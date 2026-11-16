from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.dependencies import require_login
from app.models import Machine, Device, AuditLog, ActorEnum

router = APIRouter(prefix="/api/machines", tags=["machines"])


def _mask_secret(machine: Machine) -> dict:
    """Convert a Machine to a dict, masking secrets."""
    d = {
        "id": machine.id,
        "uuid": machine.uuid,
        "name": machine.name,
        "host": machine.host,
        "ssh_port": machine.ssh_port,
        "ssh_user": machine.ssh_user,
        "auth_type": machine.auth_type,
        "has_secret": machine.secret_ref is not None,
        "sudo": machine.sudo,
        "os_kind": machine.os_kind,
        "host_key": machine.host_key,
        "usage": machine.usage,
        "vm_host": machine.vm_host,
        "enabled": machine.enabled,
        "created_at": machine.created_at,
        "updated_at": machine.updated_at,
    }
    return d


def _audit(db: Session, machine: Machine | None, action: str, result: str = "ok", output: str = ""):
    entry = AuditLog(
        action=action,
        actor=ActorEnum.fred,
        machine_id=machine.id if machine else None,
        object_ref=machine.name if machine else None,
        result=result,
        output=output[:500],  # truncate
    )
    db.add(entry)
    db.commit()


@router.get("")
def list_machines(db: Session = Depends(get_db), _user=Depends(require_login)):
    machines = db.query(Machine).filter(Machine.enabled != False).all()
    return [_mask_secret(m) for m in machines]


@router.post("")
def create_machine(
    data: dict,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    from app.secrets import encrypt_secret

    # Check for duplicate name
    existing = db.query(Machine).filter(Machine.name == data["name"]).first()
    if existing:
        raise HTTPException(status_code=400, detail="Machine name already exists")

    machine = Machine(
        name=data["name"],
        host=data["host"],
        ssh_port=data.get("ssh_port", 22),
        ssh_user=data.get("ssh_user", "root"),
        auth_type=data.get("auth_type", "key"),
        sudo=data.get("sudo", "none"),
        os_kind=data.get("os_kind", "other"),
        host_key=data.get("host_key"),
        usage=data.get("usage", "both"),
        vm_host=data.get("vm_host", False),
        enabled=data.get("enabled", True),
    )

    # Handle secrets
    if data.get("secret"):
        try:
            encrypted = encrypt_secret(data["secret"])
            machine.secret_ref = encrypted
        except Exception:
            raise HTTPException(status_code=400, detail="Failed to encrypt secret")

    db.add(machine)
    db.commit()
    db.refresh(machine)
    _audit(db, machine, "create")
    return _mask_secret(machine)


@router.get("/{machine_id}")
def get_machine(machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    return _mask_secret(machine)


@router.patch("/{machine_id}")
def update_machine(
    machine_id: int,
    data: dict,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    for key in ["name", "host", "ssh_port", "ssh_user", "auth_type", "sudo", "os_kind", "host_key", "usage", "vm_host", "enabled"]:
        if key in data:
            setattr(machine, key, data[key])

    # Handle secret update
    if "secret" in data and data["secret"]:
        from app.secrets import encrypt_secret
        machine.secret_ref = encrypt_secret(data["secret"])
    elif "secret" in data and data["secret"] is None:
        machine.secret_ref = None

    db.commit()
    db.refresh(machine)
    _audit(db, machine, "update")
    return _mask_secret(machine)


@router.delete("/{machine_id}")
def delete_machine(machine_id: int, request: Request, db: Session = Depends(get_db), _user=Depends(require_login)):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    _audit(db, machine, "delete")
    db.delete(machine)
    db.commit()
    return {"ok": True}


@router.post("/{machine_id}/test")
async def test_machine_connection(
    machine_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Test SSH connection to a machine. Returns connection result."""
    import asyncio
    from app.ssh_client import ssh_test_machine

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        result = await asyncio.wait_for(ssh_test_machine(machine), timeout=30)
    except asyncio.TimeoutError:
        result = {"ok": False, "error": "SSH test timed out (30s)", "os_kind": "other"}

    # Build safe response — no secrets ever
    response = {
        "ok": result.get("ok", False),
        "latency_ms": result.get("latency_ms"),
        "hostname": result.get("hostname", ""),
        "user": result.get("user", ""),
        "uname": result.get("uname", ""),
        "os_kind": result.get("os_kind", "other"),
        "host_key_fingerprint": result.get("host_key_fingerprint", ""),
    }
    if "error" in result:
        response["error"] = result["error"]
    return response


@router.post("/{machine_id}/detect")
async def detect_machine_hardware(
    machine_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Detect CPU/GPU/RAM/virtualisation over SSH and upsert the devices."""
    import asyncio
    from app.devices import apply_detection, device_to_dict
    from app.ssh_client import ssh_detect_hw

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        result = await asyncio.wait_for(ssh_detect_hw(machine), timeout=40)
    except asyncio.TimeoutError:
        result = {"ok": False, "error": "Détection matérielle expirée (40 s)"}

    if not result.get("ok"):
        error = result.get("error", "Détection impossible")
        _audit(db, machine, "detect", "fail", error)
        return {
            "ok": False,
            "error": error,
            "host_key_fingerprint": result.get("host_key_fingerprint", ""),
        }

    stats = apply_detection(db, machine, result)
    db.refresh(machine)
    devices = (
        db.query(Device)
        .filter(Device.machine_id == machine.id)
        .order_by(Device.kind.asc(), Device.index.asc())
        .all()
    )
    cpu = next((d for d in devices if d.kind == "cpu" and d.present), None)
    gpus = [d for d in devices if d.kind == "gpu" and d.present]
    cpu_label = f"{cpu.model} ({cpu.cores}c/{cpu.threads}t)" if cpu else "aucun"
    summary = (
        f"cpu={cpu_label}, "
        f"gpus={len(gpus)}, vm_host={machine.vm_host}, os={machine.os_kind}, "
        f"crees={stats['created']}, disparus={stats['missing']}"
    )
    _audit(db, machine, "detect", "ok", summary)

    return {
        "ok": True,
        "latency_ms": result.get("latency_ms"),
        "host_key_fingerprint": result.get("host_key_fingerprint", ""),
        "vm_host": machine.vm_host,
        "os_kind": machine.os_kind,
        "virt": result.get("virt", {}),
        "stats": stats,
        "devices": [device_to_dict(d) for d in devices],
    }


@router.get("/{machine_id}/devices")
def list_machine_devices(
    machine_id: int,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Devices of a machine (absent ones included, flagged present=false)."""
    from app.devices import device_to_dict

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    devices = (
        db.query(Device)
        .filter(Device.machine_id == machine_id)
        .order_by(Device.kind.asc(), Device.index.asc())
        .all()
    )
    return [device_to_dict(d) for d in devices]


@router.post("/{machine_id}/reset-host-key")
async def reset_machine_host_key(
    machine_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Reset the pinned host key for a machine (admin action)."""
    import asyncio
    from app.ssh_client import reset_machine_host_key as _reset

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    result = await asyncio.wait_for(_reset(machine_id), timeout=5)
    _audit(db, machine, "host-key-reset", result.get("ok") and "ok" or "fail")
    return result
