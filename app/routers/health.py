"""API de l'état de santé (§4.19) : une ligne par machine, seuils réglables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app import health
from app.database import get_db
from app.dependencies import require_login
from app.models import Metric

router = APIRouter(prefix="/api/health", tags=["health"])


def _averages(db: Session) -> dict[str, dict[str, float]]:
    """Hashrate moyen sur 1 h, par machine et par device (clé ``__machine__``)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = (
        db.query(Metric.machine_uuid, Metric.ts, Metric.device_uuid, Metric.hashrate_hs)
        .filter(Metric.ts >= since, Metric.hashrate_hs.isnot(None))
        .all()
    )
    per_device: dict[str, dict[str, list[float]]] = {}
    per_machine: dict[str, dict[str, float]] = {}
    for machine_uuid, ts, device_uuid, hashrate in rows:
        bucket = per_device.setdefault(machine_uuid, {}).setdefault(device_uuid or "", [])
        bucket.append(float(hashrate))
        per_machine.setdefault(machine_uuid, {})[ts] = (
            per_machine.setdefault(machine_uuid, {}).get(ts, 0.0) + float(hashrate)
        )
    averages: dict[str, dict[str, float]] = {}
    for machine_uuid, devices in per_device.items():
        values = {key: sum(items) / len(items) for key, items in devices.items() if items}
        totals = list(per_machine.get(machine_uuid, {}).values())
        if totals:
            values["__machine__"] = sum(totals) / len(totals)
        averages[machine_uuid] = values
    return averages


@router.get("")
def health_status(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Pastille de chaque machine + totaux, pour le tableau de bord."""
    from app.routers.metrics import metrics_live

    machines = metrics_live(db=db, _user=True)
    limits = health.thresholds(db)
    averages = _averages(db)
    rows = []
    for machine in machines:
        state = health.evaluate(machine, averages.get(machine.get("uuid"), {}), limits)
        rows.append(
            {
                **state,
                "uuid": machine.get("uuid"),
                "name": machine.get("name"),
                "host": machine.get("host"),
                "online": bool(machine.get("online")),
                "collected": bool(machine.get("collected")),
                "total_hashrate_hs": machine.get("total_hashrate_hs"),
                "wall_power_w": machine.get("wall_power_w"),
                "devices": len(machine.get("devices") or []),
            }
        )
    totals = {
        "machines": len(rows),
        "online": sum(1 for row in rows if row["online"]),
        "green": sum(1 for row in rows if row["status"] == "green"),
        "orange": sum(1 for row in rows if row["status"] == "orange"),
        "red": sum(1 for row in rows if row["status"] == "red"),
    }
    return {"machines": rows, "totals": totals, "thresholds": limits}


@router.get("/thresholds")
def get_thresholds(db: Session = Depends(get_db), _user=Depends(require_login)):
    return {"thresholds": health.thresholds(db), "defaults": health.DEFAULTS}


@router.put("/thresholds")
def put_thresholds(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    values = payload.get("thresholds", payload)
    try:
        limits = health.save_thresholds(db, values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "thresholds": limits}
