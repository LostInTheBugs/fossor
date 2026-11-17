from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.dependencies import require_login
from app.models import Machine, Device, AuditLog
from app.os_presets import apply_preset, compare_os, os_label, preset_fields, public_presets
from app.validation import validate_machine_payload

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
        # §4.1 ter : type choisi vs OS réellement détecté + bannière d'écart.
        "os_kind_detected": machine.os_kind_detected,
        "host_key": machine.host_key,
        "usage": machine.usage,
        "vm_host": machine.vm_host,
        "enabled": machine.enabled,
        "cpu_power_w": machine.cpu_power_w,
        "platform_power_w": machine.platform_power_w,
        "default_miner_profile_id": machine.default_miner_profile_id,
        "created_at": machine.created_at,
        "updated_at": machine.updated_at,
        "external_miners": _external_miners(machine),
    }
    comparison = compare_os(machine.os_kind, machine.os_kind_detected)
    d["os_label"] = os_label(machine.os_kind)
    d["os_detected_label"] = comparison["detected_label"]
    d["os_mismatch"] = comparison["mismatch"]
    d["os_banner"] = comparison["banner"]
    d["os_preset_label"] = comparison["preset_label"]
    return d


def _external_miners(machine: Machine) -> list[dict]:
    """Last external miners seen (JSON column), never raises on bad data."""
    import json

    raw = getattr(machine, "external_miners", None)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _audit(db: Session, machine: Machine | None, action: str, result: str = "ok", output: str = "", actor: str = "fred"):
    entry = AuditLog(
        action=action,
        actor=actor,
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
    current_user=Depends(require_login),
):
    from app.secrets import encrypt_secret

    payload = validate_machine_payload(data)

    # Check for duplicate name
    existing = db.query(Machine).filter(Machine.name == payload["name"]).first()
    if existing:
        raise HTTPException(status_code=409, detail="Une machine porte déjà ce nom.")

    machine = Machine(
        name=payload["name"],
        host=payload["host"],
        ssh_port=payload["ssh_port"],
        ssh_user=payload["ssh_user"],
        auth_type=payload.get("auth_type", "provision_pending"),
        sudo=payload.get("sudo", "none"),
        os_kind=data.get("os_kind", "other"),
        host_key=data.get("host_key"),
        usage=data.get("usage", "both"),
        vm_host=data.get("vm_host", False),
        enabled=data.get("enabled", True),
        cpu_power_w=data.get("cpu_power_w"),
        platform_power_w=data.get("platform_power_w", 50),
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
    _audit(db, machine, "create", actor=current_user if current_user else "system")
    return _mask_secret(machine)


@router.get("/os-presets")
def get_os_presets(_user=Depends(require_login)):
    """§4.1 ter : préréglages du formulaire d'ajout (aucun secret)."""
    return public_presets()


@router.post("/{machine_id}/os-preset")
def post_os_preset(
    machine_id: int,
    data: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_login),
):
    """Applique le préréglage du type d'OS (bouton de la bannière d'écart)."""
    from datetime import datetime, timezone

    from app.os_presets import OS_KINDS

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    kind = str((data or {}).get("os_kind") or "").strip().lower()
    if kind not in OS_KINDS:
        raise HTTPException(status_code=400, detail="Type d'OS inconnu.")
    changed = apply_preset(machine, kind)
    machine.updated_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    db.refresh(machine)
    _audit(db, machine, "os_preset", actor=current_user if current_user else "system")
    result = _mask_secret(machine)
    result["changed"] = changed
    result["preset"] = preset_fields(kind)
    return result


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
    current_user=Depends(require_login),
):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    payload = validate_machine_payload(data, partial=True)
    for key in ["name", "host", "ssh_port", "ssh_user", "auth_type", "sudo", "usage"]:
        if key in payload:
            setattr(machine, key, payload[key])
    for key in ["os_kind", "host_key", "vm_host", "enabled", "cpu_power_w",
                "platform_power_w", "default_miner_profile_id"]:
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
    _audit(db, machine, "update", actor=current_user if current_user else "system")
    return _mask_secret(machine)


@router.delete("/{machine_id}")
def delete_machine(machine_id: int, request: Request, db: Session = Depends(get_db), current_user=Depends(require_login)):
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    _audit(db, machine, "delete", actor=current_user if current_user else "system")
    db.delete(machine)
    db.commit()
    return {"ok": True}


@router.post("/{machine_id}/test")
async def test_machine_connection(
    machine_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_login),
):
    """Test SSH connection to a machine. Returns connection result."""
    import asyncio
    from app.ssh_client import ssh_test_machine

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    if machine.auth_type == "provision_pending":
        return {"ok": False, "error": "La machine n'a pas encore été préparée. Cliquez sur « Réessayer la préparation ». (provision_pending)"}

    try:
        result = await asyncio.wait_for(ssh_test_machine(machine, actor=current_user if current_user else "system"), timeout=30)
    except asyncio.TimeoutError:
        result = {"ok": False, "error": "Le test SSH a expiré (30 s)", "os_kind": "other"}

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
    current_user=Depends(require_login),
):
    """Detect CPU/GPU/RAM/virtualisation over SSH and upsert the devices."""
    import asyncio
    from app.devices import apply_detection, device_to_dict
    from app.ssh_client import ssh_detect_hw

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    if machine.auth_type == "provision_pending":
        return {"ok": False, "error": "La machine n'a pas encore été préparée. Cliquez sur « Réessayer la préparation ». (provision_pending)"}

    try:
        result = await asyncio.wait_for(ssh_detect_hw(machine, actor=current_user if current_user else "system"), timeout=40)
    except asyncio.TimeoutError:
        result = {"ok": False, "error": "Détection matérielle expirée (40 s)"}

    if not result.get("ok"):
        error = result.get("error", "Détection impossible")
        _audit(db, machine, "detect", "fail", error, actor=current_user if current_user else "system")
        return {
            "ok": False,
            "error": error,
            "host_key_fingerprint": result.get("host_key_fingerprint", ""),
        }

    external_miners = result.get("gpu_compute", [])
    stats = apply_detection(db, machine, result, gpu_compute=external_miners)
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
    _audit(db, machine, "detect", "ok", summary, actor=current_user if current_user else "system")

    miner_alerts = [
        f"{m['miner_name']}"
        + (f" {m['version']}" if m.get("version") else "")
        + (f" — {m['algorithm']}" if m.get("algorithm") else "")
        + (f" — {m['pool']}" if m.get("pool") else "")
        for m in external_miners
    ]

    return {
        "ok": True,
        "latency_ms": result.get("latency_ms"),
        "host_key_fingerprint": result.get("host_key_fingerprint", ""),
        "vm_host": machine.vm_host,
        "os_kind": machine.os_kind,
        "virt": result.get("virt", {}),
        "stats": stats,
        "devices": [device_to_dict(d) for d in devices],
        "external_miners": [
            {
                "name": m["miner_name"],
                "version": m.get("version", ""),
                "algorithm": m.get("algorithm", ""),
                "pool": m.get("pool", ""),
                "pid": m["pid"],
                "used_memory_mb": m["used_memory_mb"],
                "process_name": m["process_name"],
            }
            for m in external_miners
        ],
        "miner_alerts": miner_alerts,
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
    current_user=Depends(require_login),
):
    """Reset the pinned host key for a machine (admin action)."""
    import asyncio
    from app.ssh_client import reset_machine_host_key as _reset

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    result = await asyncio.wait_for(_reset(machine_id, actor=current_user if current_user else "system"), timeout=5)
    _audit(db, machine, "host-key-reset", result.get("ok") and "ok" or "fail", actor=current_user if current_user else "system")
    return result


# ── §4.7 bis: last raw collection (diagnostic, admin only) ────────────
@router.get("/{machine_id}/raw-collect")
def last_raw_collection(
    machine_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_login),
):
    """Raw ``collect_stats`` output of a machine, already masked.

    Admin only: the screen exists to fix a parser without an SSH session.
    Wallets are truncated and passwords are ``•••`` (``mask_raw_collect``).
    """
    from app.models import RawCollection, User

    user = db.query(User).filter(User.username == current_user).first()
    if user is None or not user.is_admin:
        raise HTTPException(status_code=403, detail="Réservé à l'administrateur")
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    row = (
        db.query(RawCollection)
        .filter(RawCollection.machine_uuid == machine.uuid)
        .first()
    )
    if row is None:
        return {"ts": None, "stdout": "", "machine": machine.name}
    return {"ts": row.ts, "stdout": row.stdout, "machine": machine.name}


# ── §4.1 bis mode 3: automatic preparation through temporary access ───
@router.post("/{machine_id}/provision")
async def provision_machine_account(
    machine_id: int,
    data: dict,
    db: Session = Depends(get_db),
    current_user=Depends(require_login),
):
    """Prepare the dedicated account with a temporary login/password.

    The temporary password is only kept in memory for the duration of the
    call: it is never stored, logged or audited (see ``app/provision.py``).

    On success, sets auth_type=fossor_key so subsequent SSH works with
    the dedicated fossor account instead of the temporary one.
    """
    from app.provision import ProvisionError, provision_machine

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        result = await provision_machine(
            machine,
            temp_user=data.get("temp_user") or "",
            temp_password=data.get("temp_password") or "",
            account=data.get("account") or "fossor",
            actor=current_user if current_user else "system",
        )
    except ProvisionError as exc:
        # app.provision already journalised the failure (single audit row).
        raise HTTPException(status_code=400, detail=str(exc))

    if result.get("ok"):
        db.expire_all()
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        if machine is not None:
            machine.auth_type = "fossor_key"
            db.commit()
            result["machine"] = _mask_secret(machine)
        # app.provision wrote the single success row with the logged-in actor.
    return result


@router.post("/{machine_id}/deprovision")
async def deprovision_machine_account(
    machine_id: int,
    data: dict | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_login),
):
    """Remove Fossor's access (sudoers drop-in, key line, optionally the account)."""
    from app.provision import ProvisionError, deprovision_machine

    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")

    try:
        result = await deprovision_machine(machine, delete_account=bool((data or {}).get("delete_account")))
    except ProvisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, machine, "deprovision", result.get("ok") and "ok" or "fail", actor=current_user if current_user else "system")
    return result
