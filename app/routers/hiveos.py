"""Routes « Pilotage HiveOS » (§4.1 ter, §4.6, §4.7) — fiche machine et sonde."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import hiveos as H
from app import hiveos_remote as HR
from app.ai_config import get_setting, set_setting
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Device, Machine
from app import pilotage

router = APIRouter(prefix="/api/machines", tags=["hiveos"])


def _audit(db: Session, machine: Machine, action: str, result: str, output: str = "",
           actor: str = "system") -> None:
    db.add(AuditLog(actor=actor, action=action, machine_id=machine.id, result=result,
                    output=output[:500]))
    db.commit()


def stored_probe(machine: Machine) -> dict | None:
    """Dernière sonde enregistrée (masquée), ou ``None``."""
    raw = getattr(machine, "hiveos_json", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("detected") else None


def _devices(db: Session, machine: Machine) -> list[Device]:
    return db.query(Device).filter(Device.machine_id == machine.id).all()


def _view(db: Session, machine: Machine) -> dict:
    ack = bool(get_setting(db, "hiveos_watchdog_ack", False))
    probe = stored_probe(machine)
    view = H.pilotage(probe, _devices(db, machine), ack=ack)
    view["machine_id"] = machine.id
    view["machine"] = machine.name
    view["os_kind"] = machine.os_kind
    view["os_kind_detected"] = machine.os_kind_detected
    view["watchdog_ack"] = ack
    view["simulation"] = pilotage.simulated_for(db, machine)
    return view


@router.get("/{machine_id}/hiveos")
def read_hiveos(machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    """Vue « Pilotage HiveOS » : zéro commande, uniquement la sonde enregistrée."""
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine introuvable")
    return _view(db, machine)


@router.post("/{machine_id}/hiveos/probe")
async def probe_hiveos(machine_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    """Sonde HiveOS **en lecture seule** ; en simulation, rien n'est exécuté."""
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine introuvable")
    if not H.is_hiveos(machine):
        raise HTTPException(
            status_code=400,
            detail=(
                "Cette machine n'est pas déclarée HiveOS : choisissez le type d'OS « HiveOS » "
                "(§4.1 ter) avant de lancer la sonde."
            ),
        )
    simulated = pilotage.simulated_for(db, machine)
    try:
        data = await HR.probe(machine, simulated=simulated)
    except HR.HiveosError as exc:
        _audit(db, machine, "hiveos-probe", "fail", str(exc), actor=user)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if simulated:
        return {"simulated": True, "probe": data, "pilotage": _view(db, machine)}
    machine.hiveos_json = json.dumps(H.mask_probe(data), ensure_ascii=False)
    db.commit()
    _audit(db, machine, "hiveos-probe", "ok", f"mineur {data.get('miner') or '?'}", actor=user)
    return {"simulated": False, "probe": H.mask_probe(data), "pilotage": _view(db, machine)}


@router.get("/{machine_id}/hiveos/simulation")
def simulated_plan(machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    """Plan complet (arrêt + reprise) tel qu'il serait exécuté — aucun appel SSH."""
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine introuvable")
    probe = stored_probe(machine)
    if probe is None:
        return {"simulated": True, "commands": [], "reason": "aucune sonde HiveOS enregistrée"}
    ack = bool(get_setting(db, "hiveos_watchdog_ack", False))
    refusal = ""
    try:
        stop = [step.command() for step in H.plan_stop(probe, ack)]
        start = [step.command() for step in H.plan_start(probe, ack)]
    except H.HiveosError as exc:
        refusal, stop, start = str(exc), [], []
    return {
        "simulated": True,
        "stop": stop,
        "start": start,
        "refusal": refusal,
        "watchdog": H.watchdog_verdict(probe, ack),
        "partial_refusal": H.partial_refusal(
            H.gpu_indices(_devices(db, machine)), int(probe.get("gpu_total") or 0)
        ) or "",
    }


router_settings = APIRouter(prefix="/api/hiveos", tags=["hiveos"])


@router_settings.get("/watchdog-ack")
def read_watchdog_ack(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Option explicite « arrêter le watchdog HiveOS pendant une campagne »."""
    return {
        "ack": bool(get_setting(db, "hiveos_watchdog_ack", False)),
        "parade": H.WATCHDOG_PARADE,
        "reason": H.WATCHDOG_REFUSAL,
    }


@router_settings.put("/watchdog-ack")
def set_watchdog_ack(data: dict, db: Session = Depends(get_db), user=Depends(require_login)):
    """Active/désactive l'option ; l'activation exige le mot ``VALIDER``.

    Fossor ne modifie **jamais** ``rig.conf`` : l'option ne fait qu'autoriser
    ``wd stop`` / ``wd start`` autour d'une campagne.
    """
    ack = bool(data.get("ack"))
    if ack and str(data.get("confirm") or "").strip().upper() != "VALIDER":
        raise HTTPException(
            status_code=400,
            detail=(
                "Arrêter le watchdog HiveOS pendant une campagne est une décision explicite : "
                'envoyez {"ack": true, "confirm": "VALIDER"}. rig.conf ne sera pas modifié.'
            ),
        )
    set_setting(db, "hiveos_watchdog_ack", ack)
    db.commit()
    db.add(AuditLog(actor=user, action="hiveos-watchdog-ack", result="ok",
                    output=f"watchdog HiveOS arrêté pendant les campagnes : {ack}"))
    db.commit()
    return {"ack": ack}
