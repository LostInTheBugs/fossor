"""Configuration et journal du watchdog (§10.1).

La décision et les garde-fous vivent dans :mod:`app.watchdog` ; ce routeur ne
fait que lire la base, enregistrer une configuration et déclencher une
évaluation à la demande (utile après un changement de seuils).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import watchdog
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Machine, WatchdogEvent

router = APIRouter(prefix="/api/watchdog", tags=["watchdog"])


def _machine(db: Session, machine_id: int) -> Machine:
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine introuvable")
    return machine


def _audit(db: Session, actor: str, action: str, detail: str) -> None:
    db.add(AuditLog(actor=actor, action=action, result="ok", output=detail[:1000]))
    db.commit()


def _machine_rows(db: Session, config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for machine in db.query(Machine).order_by(Machine.name).all():
        override = config.get("machines", {}).get(str(machine.id))
        rows.append({
            "id": machine.id,
            "name": machine.name,
            "machine_kind": machine.machine_kind,
            "os_kind": machine.os_kind,
            "simulated": watchdog._is_simulated(db, machine),
            "asic": bool(machine.machine_kind == "asic"),
            "reboot_ok": bool(machine.watchdog_reboot_ok),
            "power_ok": bool(machine.watchdog_power_ok),
            "override": override or None,
        })
    return rows


@router.get("")
def get_watchdog(db: Session = Depends(get_db), _: str = Depends(require_login)) -> dict[str, Any]:
    config = watchdog.get_config(db)
    return {"config": config, "machines": _machine_rows(db, config),
            "rules": list(watchdog.RULE_NAMES), "actions": list(watchdog.ACTION_NAMES)}


@router.put("/config")
def put_config(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db),
               actor: str = Depends(require_login)) -> dict[str, Any]:
    config = watchdog.save_config(db, payload)
    _audit(db, actor, "watchdog-config", "Configuration du watchdog enregistrée")
    return {"ok": True, "config": config, "machines": _machine_rows(db, config)}


@router.put("/machines/{machine_id}")
def put_machine(machine_id: int, payload: dict[str, Any] = Body(...),
                db: Session = Depends(get_db), actor: str = Depends(require_login)) -> dict[str, Any]:
    machine = _machine(db, machine_id)
    config = watchdog.save_machine_override(db, machine_id, payload.get("override", payload))
    _audit(db, actor, "watchdog-machine", f"Surcharge watchdog de {machine.name} enregistrée")
    return {"ok": True, "config": config, "machines": _machine_rows(db, config)}


@router.post("/machines/{machine_id}/authorize")
def authorize(machine_id: int, payload: dict[str, Any] = Body(...),
              db: Session = Depends(get_db), actor: str = Depends(require_login)) -> dict[str, Any]:
    """Active reboot ou power cycle pour **cette** machine, avec « VALIDER »."""
    machine = _machine(db, machine_id)
    action = str(payload.get("action", "")).strip()
    if action not in ("reboot", "power_cycle"):
        raise HTTPException(status_code=400, detail="action inconnue : reboot ou power_cycle attendu")
    if str(payload.get("confirm", "")).strip() != watchdog.CONFIRM_WORD:
        raise HTTPException(
            status_code=400,
            detail=f"Activation refusée : saisissez « {watchdog.CONFIRM_WORD} » pour confirmer",
        )
    enabled = bool(payload.get("enabled", True))
    if action == "reboot":
        machine.watchdog_reboot_ok = enabled
    else:
        machine.watchdog_power_ok = enabled
    db.commit()
    _audit(db, actor, "watchdog-authorize",
           f"{action} {'activé' if enabled else 'désactivé'} sur {machine.name} (mot VALIDER)")
    config = watchdog.get_config(db)
    return {"ok": True, "machine": machine_id, "action": action, "enabled": enabled,
            "machines": _machine_rows(db, config)}


@router.get("/events")
def events(machine_id: int | None = Query(None), limit: int = Query(100, ge=1, le=500),
           db: Session = Depends(get_db), _: str = Depends(require_login)) -> dict[str, Any]:
    query = db.query(WatchdogEvent)
    if machine_id is not None:
        query = query.filter(WatchdogEvent.machine_id == machine_id)
    rows = query.order_by(WatchdogEvent.id.desc()).limit(limit).all()
    return {"events": [
        {
            "id": row.id, "ts": row.ts, "machine_id": row.machine_id,
            "machine_name": row.machine_name, "device_id": row.device_id,
            "device_label": row.device_label, "rule": row.rule, "measure": row.measure,
            "action": row.action, "result": row.result,
            "simulated": bool(row.simulated), "detail": row.detail,
        }
        for row in rows
    ]}


@router.post("/run/{machine_id}")
async def run_now(machine_id: int, db: Session = Depends(get_db),
                  _: str = Depends(require_login)) -> dict[str, Any]:
    """Évalue une machine tout de suite, sur les dernières mesures en base."""
    machine = _machine(db, machine_id)
    events = await watchdog.evaluate_machine(db, machine)
    return {"ok": True, "machine_id": machine_id, "events": events,
            "now": datetime.now(timezone.utc).isoformat()}
