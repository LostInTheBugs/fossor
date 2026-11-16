"""Device API — per-card mining switch (§4.2)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_login
from app.devices import device_to_dict, recompute_usage
from app.models import ActorEnum, AuditLog, Device, Machine

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("/{device_id}")
def get_device(device_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device_to_dict(device)


@router.patch("/{device_id}")
def update_device(
    device_id: int,
    data: dict,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Enable/disable a device for mining. Only ``enabled_for_mining`` is writable."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if "enabled_for_mining" not in data:
        raise HTTPException(status_code=400, detail="enabled_for_mining is required")

    device.enabled_for_mining = bool(data["enabled_for_mining"])
    device.updated_at = datetime.now(timezone.utc).isoformat()

    # §4.2: the machine usage follows its devices on every PATCH.
    machine = db.query(Machine).filter(Machine.id == device.machine_id).first()
    usage = recompute_usage(db, machine) if machine is not None else None

    entry = AuditLog(
        actor=ActorEnum.fred,
        action="device-update",
        machine_id=device.machine_id,
        object_ref=f"device:{device.uuid}",
        result="ok",
        output=(
            f"{device.kind} {device.model or ''} "
            f"enabled_for_mining={device.enabled_for_mining} usage={usage}"
        )[:500],
    )
    db.add(entry)
    db.commit()
    db.refresh(device)
    return device_to_dict(device)
