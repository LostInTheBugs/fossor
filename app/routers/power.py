"""Contrôleur d'alimentation d'une machine (§4.17) — configuration et actions.

Les garde-fous et la construction des requêtes vivent dans
:mod:`app.power_control` ; ce routeur ne fait que lire la base, appeler et
journaliser. Une action refusée ne laisse aucun appel sortant.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app import pilotage
from app import power_control as PC
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Machine, MachineStatus, PowerAction, PowerController

router = APIRouter(prefix="/api", tags=["power"])


def _machine(db: Session, machine_id: int) -> Machine:
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine introuvable")
    return machine


def _controller(db: Session, machine_id: int) -> PowerController | None:
    return (
        db.query(PowerController)
        .filter(PowerController.machine_id == machine_id)
        .first()
    )


def _online(db: Session, machine: Machine) -> bool:
    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    )
    return bool(status and status.online)


def _audit(db: Session, actor: str, action: str, ref: str, result: str, detail: str) -> None:
    db.add(AuditLog(
        actor=actor, action=action, object_ref=ref[:255], result=result,
        output=detail[:500],
    ))
    db.commit()


@router.get("/machines/{machine_id}/power-controller")
def read_controller(
    machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    machine = _machine(db, machine_id)
    row = _controller(db, machine_id)
    return {
        "machine_id": machine.id,
        "machine": machine.name,
        "fossor_host": bool(machine.fossor_host),
        "kinds": [
            {"value": kind, "label": PC.KIND_LABELS[kind], "supported": kind in PC.SUPPORTED}
            for kind in PC.KINDS
        ],
        "controller": PC.controller_dict(row) if row is not None else None,
        "max_per_day": PC.DEFAULT_MAX_PER_DAY,
        "min_delay_s": PC.DEFAULT_MIN_DELAY_S,
        "counted_today": PC.counted_actions(db, machine_id, datetime.now(timezone.utc)),
    }


@router.put("/machines/{machine_id}/power-controller")
def put_controller(
    machine_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    machine = _machine(db, machine_id)
    if "fossor_host" in payload:
        machine.fossor_host = bool(payload["fossor_host"])
        db.commit()
        _audit(db, str(user), "power-fossor-host", machine.name, "ok",
               f"fossor_host={machine.fossor_host}")
    if not payload.get("kind"):
        raise HTTPException(status_code=400, detail="type de contrôleur attendu")
    try:
        row = PC.save_controller(db, machine_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, str(user), "power-controller", machine.name, "ok",
           f"{row.kind} {row.url}")
    return {
        "ok": True,
        "controller": PC.controller_dict(row),
        "fossor_host": bool(machine.fossor_host),
    }


@router.delete("/machines/{machine_id}/power-controller")
def delete_controller(
    machine_id: int, db: Session = Depends(get_db), user=Depends(require_login)
):
    machine = _machine(db, machine_id)
    row = _controller(db, machine_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Aucun contrôleur configuré")
    db.delete(row)
    db.commit()
    _audit(db, str(user), "power-controller", machine.name, "ok", "supprimé")
    return {"ok": True}


@router.post("/machines/{machine_id}/power/read")
async def read_power(
    machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    machine = _machine(db, machine_id)
    row = _controller(db, machine_id)
    if row is None:
        raise HTTPException(status_code=400, detail="Aucun contrôleur configuré")
    try:
        state = await PC.read_state(row)
    except PC.PowerError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # réseau
        raise HTTPException(status_code=502, detail=f"contrôleur injoignable : {exc}")
    if state.get("power_w") is not None:
        PC.store_sample(
            db, machine.id, float(state["power_w"]), state["source"], row.kind
        )
    return {"ok": True, **state}


@router.get("/machines/{machine_id}/power/history")
def power_history(
    machine_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    _machine(db, machine_id)
    rows = (
        db.query(PowerAction)
        .filter(PowerAction.machine_id == machine_id)
        .order_by(PowerAction.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [
        {
            "id": row.id, "action": row.action, "result": row.result,
            "detail": row.detail, "simulated": bool(row.simulated),
            "actor": row.actor, "at": row.at,
        }
        for row in rows
    ]


@router.post("/machines/{machine_id}/power/{action}")
async def power_action(
    machine_id: int,
    action: str,
    payload: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Éteindre, allumer ou faire un power cycle — avec confirmation et quota."""
    payload = payload or {}
    machine = _machine(db, machine_id)
    row = _controller(db, machine_id)
    simulated = pilotage.simulated_for(db, machine)
    online = _online(db, machine)

    try:
        PC.check_guards(
            db, machine, row, action,
            confirm=bool(payload.get("confirm")),
            online=online,
        )
    except PC.PowerError as exc:
        PC.journal(
            db, machine.id, action, result="refused", detail=str(exc),
            simulated=simulated, actor=str(user),
        )
        raise HTTPException(status_code=409, detail=str(exc))

    if simulated:
        commands = PC.build_requests(row, action)
        detail = json.dumps(
            [f"{c['method']} {c['path']}" for c in commands], ensure_ascii=False
        )
        PC.journal(
            db, machine.id, action, result="simulated", detail=detail,
            simulated=True, actor=str(user),
        )
        return {"ok": True, "simulated": True, "action": action, "commands": commands}

    try:
        result = await PC.perform(
            row, action,
            cycle_wait_s=int(payload.get("cycle_wait_s") or PC.DEFAULT_CYCLE_WAIT_S),
        )
    except PC.PowerError as exc:
        PC.journal(db, machine.id, action, result="fail", detail=str(exc),
                   simulated=False, actor=str(user))
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        PC.journal(db, machine.id, action, result="fail", detail=str(exc),
                   simulated=False, actor=str(user))
        raise HTTPException(status_code=502, detail=f"contrôleur injoignable : {exc}")

    PC.journal(
        db, machine.id, action, result="ok",
        detail=json.dumps(result.get("sent", []), ensure_ascii=False),
        simulated=False, actor=str(user),
    )
    return {"ok": True, "simulated": False, "action": action, **result}
