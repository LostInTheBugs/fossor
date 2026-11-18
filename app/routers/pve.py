"""API du pilote Proxmox (§4.6) — modèle de VM et inspection en lecture seule.

Le modèle Proxmox est un réglage distinct de ``spec_vm_model`` (libvirt) : les
deux peuvent coexister, et seul l'hôte ``vm_host`` de type Proxmox les utilise.
L'inspection ne lit que ``pvesm``/``pvesh``/``qm list`` : aucune écriture, aucune
création d'utilisateur, de jeton ou de permission sur l'hyperviseur.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import pve_remote as PR
from app import pve_vm as P
from app.ai_config import get_setting, set_setting
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog
from app.routers.miners import simulation_enabled
from app.routers.vms import _host

router = APIRouter(prefix="/api", tags=["pve"])

PVE_MODEL_KEY = "spec_pve_model"


def stored_pve_model(db: Session) -> dict | None:
    import json

    raw = get_setting(db, PVE_MODEL_KEY, None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            return None
    return None


def _audit(db: Session, actor: str, action: str, ref: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            ts=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            action=action,
            object_ref=ref[:255],
            result=result,
            output=(output or "")[:500],
        )
    )
    db.commit()


def pve_defaults() -> dict:
    return {
        "node": "",
        "disk_storage": P.DEFAULT_DISK_STORAGE,
        "ci_storage": P.DEFAULT_CI_STORAGE,
        "bridge": P.DEFAULT_BRIDGE,
        "vmid_min": P.DEFAULT_VMID_MIN,
        "vmid_max": P.DEFAULT_VMID_MAX,
        "vcpus": 1,
        "memory_mb": 1024,
        "disk_gb": 8,
        "checksum_algo": "sha256",
    }


def _inspection_model(model: dict | None) -> dict:
    """Modèle minimal pour l'inspection : plage et taille de disque suffisent."""
    base = dict(pve_defaults())
    base.update({key: value for key, value in (model or {}).items() if value not in (None, "")})
    return base


@router.get("/spec/pve/settings")
def read_pve_settings(db: Session = Depends(get_db), _user=Depends(require_login)):
    from app.models import Machine

    hosts = (
        db.query(Machine)
        .filter(Machine.vm_host.is_(True))
        .order_by(Machine.name.asc())
        .all()
    )
    return {
        "pve_model": stored_pve_model(db) or {},
        "defaults": pve_defaults(),
        "hosts": [
            {"id": m.id, "name": m.name, "host": m.host, "os_kind": m.os_kind,
             "ssh_user": m.ssh_user}
            for m in hosts
        ],
        "checksum_algos": list(P.CHECKSUM_ALGOS),
        "simulation": simulation_enabled(db),
    }


@router.put("/spec/pve/settings")
def write_pve_settings(data: dict, db: Session = Depends(get_db), user=Depends(require_login)):
    payload = data.get("pve_model") if "pve_model" in data else data
    try:
        model = P.validate_pve_model(payload or {})
    except P.VmError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    host = _host(db, model["host_id"])
    if (host.os_kind or "") not in ("proxmox", "other") and not host.vm_host:
        raise HTTPException(status_code=400, detail="Machine hôte inutilisable pour Proxmox")
    set_setting(db, PVE_MODEL_KEY, model)
    db.commit()
    _audit(db, user, "spec-pve-settings", f"machine {model['host_id']}", "ok",
           f"{model['node']} {model['vmid_min']}-{model['vmid_max']}")
    return read_pve_settings(db, _user=user)


@router.post("/spec/pve/inspect")
async def inspect_pve(data: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    """Lecture seule : stockages ``images``, VMID pris, premier VMID libre."""
    machine = _host(db, int(data.get("machine_id") or 0))
    model = _inspection_model(stored_pve_model(db))
    if data.get("model"):
        model.update({key: value for key, value in data["model"].items() if value is not None})
    try:
        return await PR.inspect(machine, simulated=simulation_enabled(db), model=model)
    except PR.PveRemoteError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/spec/pve/detect")
async def detect_pve(data: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    """``pveversion`` **et** ``/dev/kvm`` : confirme (ou non) ``vm_host``."""
    machine = _host(db, int(data.get("machine_id") or 0))
    try:
        return await PR.detect(machine, simulated=simulation_enabled(db))
    except PR.PveRemoteError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
