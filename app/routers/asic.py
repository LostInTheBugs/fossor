"""ASIC (§4.20) — ajout par IP, sonde en lecture seule, **aucun pilotage**.

Un ASIC est une ``Machine`` de type ``asic`` : pas de SSH, ``usage=none`` pour
que la boucle SSH l'ignore, et son état vit dans ``machine_status.asic_json``.
Les routes n'exposent que des lectures (état, santé) ; aucun redémarrage, aucun
changement de pool.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import asic, asic_client
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Machine, MachineStatus, UsageEnum

router = APIRouter(prefix="/api/asics", tags=["asics"])


class AsicIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1, max_length=255)
    driver: str = "auto"
    model: str = ""
    expected_hashrate_hs: float | None = None
    power_w: float | None = None
    port: int | None = None


def _status_row(db: Session, machine: Machine) -> MachineStatus | None:
    return db.query(MachineStatus).filter(MachineStatus.machine_uuid == machine.uuid).first()


def state_of(db: Session, machine: Machine) -> dict | None:
    row = _status_row(db, machine)
    if not row or not row.asic_json:
        return None
    try:
        return json.loads(row.asic_json)
    except ValueError:
        return None


def public(machine: Machine, state: dict | None, thresholds: dict | None = None) -> dict:
    return {
        "id": machine.id,
        "uuid": machine.uuid,
        "name": machine.name,
        "host": machine.host,
        "driver": machine.asic_driver or "",
        "model": machine.asic_model or "",
        "expected_hashrate_hs": machine.asic_expected_hashrate_hs,
        "power_w": machine.asic_power_w,
        "read_only": True,
        "asic": state,
        "health": asic.health(state, thresholds),
    }


def _store_state(db: Session, machine: Machine, state: dict) -> None:
    row = _status_row(db, machine)
    stamp = datetime.now(timezone.utc).isoformat()
    if row is None:
        row = MachineStatus(machine_uuid=machine.uuid, ts=stamp)
        db.add(row)
    row.ts = stamp
    row.online = bool(state.get("online"))
    row.error = state.get("error")
    row.miner = str(state.get("model") or "")
    row.algo = str(state.get("algorithm") or "")
    row.coin = str(state.get("coin") or "")
    row.total_hashrate_hs = state.get("hashrate_hs")
    power = state.get("power_w")
    row.total_power_w = power if power is not None else machine.asic_power_w
    uptime = state.get("uptime_s")
    row.uptime_s = int(uptime) if isinstance(uptime, (int, float)) else None
    accepted = state.get("shares_accepted")
    row.accepted = int(accepted) if isinstance(accepted, (int, float)) else None
    rejected = state.get("shares_rejected")
    row.rejected = int(rejected) if isinstance(rejected, (int, float)) else None
    row.asic_json = json.dumps(state, ensure_ascii=False)


async def probe_machine(machine: Machine, db: Session) -> dict:
    """Interroge l'ASIC et met à jour son état (lecture seule)."""
    driver = machine.asic_driver or "axeos"
    try:
        state = await asic_client.poll(driver, machine.host)
    except asic_client.AsicError as exc:
        row = _status_row(db, machine)
        stamp = datetime.now(timezone.utc).isoformat()
        if row is None:
            row = MachineStatus(machine_uuid=machine.uuid, ts=stamp)
            db.add(row)
        row.ts = stamp
        row.online = False
        row.error = str(exc)
        return {"online": False, "driver": driver, "error": str(exc)}
    _store_state(db, machine, state)
    return state


@router.get("")
def list_asics(db: Session = Depends(get_db), _user=Depends(require_login)):
    machines = (
        db.query(Machine)
        .filter(Machine.machine_kind == "asic")
        .order_by(Machine.name.asc())
        .all()
    )
    return {"asics": [public(machine, state_of(db, machine)) for machine in machines]}


@router.post("", status_code=201)
async def add_asic(item: AsicIn, db: Session = Depends(get_db), user=Depends(require_login)):
    """Ajout par IP : sonde en lecture seule puis enregistrement (sans SSH)."""
    try:
        host = asic_client.clean_host(item.host)
    except asic_client.AsicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    driver = (item.driver or "auto").strip().lower()
    if driver not in ("auto", *asic.DRIVERS):
        raise HTTPException(status_code=400, detail=f"Pilote inconnu : {driver!r}")
    try:
        if driver == "auto":
            found = await asic_client.detect(host)
            driver = found["driver"]
            state = found["state"]
        else:
            state = await asic_client.poll(driver, host)
    except asic_client.AsicError as exc:
        raise HTTPException(status_code=400, detail=f"sonde en lecture seule : {exc}")
    machine = Machine(
        name=item.name.strip(),
        host=host,
        machine_kind="asic",
        ssh_port=0,
        ssh_user="",
        usage=UsageEnum.none,
        enabled=True,
        asic_driver=driver,
        asic_model=(item.model or state.get("model") or "")[:80],
        asic_expected_hashrate_hs=item.expected_hashrate_hs or state.get("expected_hashrate_hs"),
        asic_power_w=item.power_w,
    )
    db.add(machine)
    db.flush()
    _store_state(db, machine, state)
    db.add(
        AuditLog(
            actor=user,
            action="asic-add",
            object_ref=f"asic:{host}"[:255],
            result="ok",
            output=json.dumps({"name": machine.name, "host": host, "driver": driver}, ensure_ascii=False)[:500],
        )
    )
    db.commit()
    db.refresh(machine)
    return public(machine, state_of(db, machine))


@router.post("/{machine_id}/probe")
async def probe(machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    machine = db.query(Machine).filter(Machine.id == machine_id, Machine.machine_kind == "asic").first()
    if not machine:
        raise HTTPException(status_code=404, detail="ASIC inconnu")
    state = await probe_machine(machine, db)
    db.commit()
    db.refresh(machine)
    return public(machine, state_of(db, machine))


@router.delete("/{machine_id}")
def delete_asic(machine_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    machine = db.query(Machine).filter(Machine.id == machine_id, Machine.machine_kind == "asic").first()
    if not machine:
        raise HTTPException(status_code=404, detail="ASIC inconnu")
    row = _status_row(db, machine)
    if row:
        db.delete(row)
    db.delete(machine)
    db.add(AuditLog(actor=user, action="asic-delete", object_ref=f"asic:{machine.name}"[:255], result="ok"))
    db.commit()
    return {"ok": True}
