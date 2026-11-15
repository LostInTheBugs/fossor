from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.dependencies import require_login
from app.models import Machine, AuditLog, ActorEnum

router = APIRouter(prefix="/api/machines", tags=["machines"])


def _mask_secret(machine: Machine) -> dict:
    """Convert a Machine to a dict, masking secrets."""
    d = {
        "id": machine.id,
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
