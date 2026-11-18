"""Device API — per-card mining switch (§4.2)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_login
from app.devices import device_to_dict, recompute_usage
from app.models import ActorEnum, AuditLog, Device, Machine

router = APIRouter(prefix="/api/devices", tags=["devices"])

#: §4.19 — rôles d'un device.
DEVICE_ROLES = ("default_only", "default_and_spec")


@router.get("/{device_id}")
def get_device(device_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device_to_dict(device)


def _apply_role(db: Session, device: Device, data: dict) -> None:
    """§4.19 : rôle du device, pool de boost et token par défaut propre au device."""
    from app.models import MinerProfile

    if "role" in data:
        role = str(data.get("role") or "").strip()
        if role not in DEVICE_ROLES:
            raise HTTPException(
                status_code=422,
                detail=f"Rôle inconnu : {role!r} (attendu {' ou '.join(DEVICE_ROLES)})",
            )
        device.role = role
    if "boost_pool" in data:
        device.boost_pool = bool(data.get("boost_pool"))
    if "default_token_id" in data:
        raw = data.get("default_token_id")
        if raw in (None, ""):
            device.default_token_id = None
        else:
            try:
                profile_id = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="default_token_id invalide")
            if db.get(MinerProfile, profile_id) is None:
                raise HTTPException(status_code=422, detail="Profil par défaut introuvable")
            device.default_token_id = profile_id


@router.patch("/{device_id}")
def update_device(
    device_id: int,
    data: dict,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Enable/disable a device, and set its role (§4.19)."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    writable = {"enabled_for_mining", "role", "boost_pool", "default_token_id"}
    if not writable & set(data):
        raise HTTPException(
            status_code=400,
            detail="enabled_for_mining, role, boost_pool ou default_token_id attendu",
        )
    if data.get("enabled_for_mining") and device.integrated:
        from app.devices import integrated_hidden

        if integrated_hidden(db):
            raise HTTPException(
                status_code=400,
                detail="GPU intégré masqué — réglez « Masquer les GPU intégrés du minage » dans Configuration",
            )

    if "enabled_for_mining" in data:
        device.enabled_for_mining = bool(data["enabled_for_mining"])
    _apply_role(db, device, data)
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
