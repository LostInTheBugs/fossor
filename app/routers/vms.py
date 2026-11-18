"""API des VM jetables du spec mining (§4.6, étape 3c).

Rien n'est exécuté en simulation : les routes renvoient les commandes qui
seraient lancées. La destruction exige **deux** conditions : la VM appartient à
Fossor (préfixe + métadonnée, relus sur l'hôte) et sa sauvegarde de wallet a été
vérifiée (§10.11) — le contenu du fichier n'est jamais lu.
"""

from __future__ import annotations

import json
import time
import uuid as uuid_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import spec_vm as V
from app import vm_remote as VR
from app.ai_config import get_setting, set_setting
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Campaign, Machine, SpecVm
from app.routers.miners import simulation_enabled

router = APIRouter(prefix="/api", tags=["vms"])

VM_MODEL_KEY = "spec_vm_model"
SPEC_MODE_KEY = "spec_default_mode"
BACKUP_AGE_KEY = "vm_backup_max_age_days"
DESTROY_WORD = "DETRUIRE"


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


def spec_mode(db: Session) -> str:
    return V.spec_mode(get_setting(db, SPEC_MODE_KEY, V.DEFAULT_SPEC_MODE))


def backup_max_age_days(db: Session) -> int:
    value = get_setting(db, BACKUP_AGE_KEY, V.DEFAULT_BACKUP_MAX_AGE_DAYS)
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = V.DEFAULT_BACKUP_MAX_AGE_DAYS
    return days if 1 <= days <= 365 else V.DEFAULT_BACKUP_MAX_AGE_DAYS


def stored_model(db: Session) -> dict | None:
    raw = get_setting(db, VM_MODEL_KEY, None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            return None
    return None


def _host(db: Session, machine_id: int) -> Machine:
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=400, detail="Machine hôte introuvable")
    if not machine.vm_host:
        raise HTTPException(
            status_code=400,
            detail="Cette machine n'est pas marquée comme hôte de VM (vm_host)",
        )
    return machine


def _vm(db: Session, vm_id: int) -> SpecVm:
    row = db.query(SpecVm).filter(SpecVm.id == vm_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="VM introuvable")
    return row


def vm_to_dict(db: Session, row: SpecVm) -> dict:
    campaign = None
    if row.campaign_id:
        found = db.query(Campaign).filter(Campaign.id == row.campaign_id).first()
        campaign = {"id": found.id, "coin": found.coin, "status": found.status} if found else None
    backup = None
    if row.backup_state:
        try:
            backup = json.loads(row.backup_state)
        except (TypeError, ValueError):
            backup = None
    machine = db.query(Machine).filter(Machine.id == row.host_machine_id).first()
    return {
        "id": row.id,
        "uuid": row.uuid,
        "name": row.name,
        "campaign_id": row.campaign_id,
        "campaign": campaign,
        "host_machine_id": row.host_machine_id,
        "host_name": machine.name if machine else "",
        "mode": row.mode,
        "mode_label": V.MODE_LABELS.get(row.mode, row.mode),
        "state": row.state,
        "ip": row.ip,
        "vcpus": row.vcpus,
        "memory_mb": row.memory_mb,
        "disk_gb": row.disk_gb,
        "network": row.network,
        "node_port": row.node_port,
        "node_address": row.ip and f"{row.ip}:{row.node_port}" or "",
        "wallet_address": row.wallet_address,
        "backup_path": row.backup_path,
        "backup": backup,
        "backup_checked_at": row.backup_checked_at,
        "destroyable": bool(backup and backup.get("verified")),
        "note": row.note,
        "created_at": row.created_at,
        "destroyed_at": row.destroyed_at,
    }


# ── Réglages (Configuration → Spec mining) ───────────────────────────
@router.get("/spec/settings")
def read_spec_settings(db: Session = Depends(get_db), _user=Depends(require_login)):
    model = stored_model(db) or {}
    hosts = (
        db.query(Machine)
        .filter(Machine.vm_host.is_(True))
        .order_by(Machine.name.asc())
        .all()
    )
    return {
        "default_mode": spec_mode(db),
        "modes": [{"value": m, "label": V.MODE_LABELS[m]} for m in V.SPEC_MODES],
        "vm_model": model,
        "backup_max_age_days": backup_max_age_days(db),
        "hosts": [
            {"id": m.id, "name": m.name, "host": m.host, "os_kind": m.os_kind}
            for m in hosts
        ],
        "defaults": {
            "vcpus": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "network": "default",
            "node_port": V.DEFAULT_NODE_PORT,
            "wallet_backup_path": f"{V.DATA_DIR}/wallet.dat",
        },
        "simulation": simulation_enabled(db),
    }


@router.put("/spec/settings")
def write_spec_settings(data: dict, db: Session = Depends(get_db), user=Depends(require_login)):
    if "default_mode" in data:
        try:
            mode = V.normalize_mode(data.get("default_mode"), V.DEFAULT_SPEC_MODE)
        except V.VmError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        set_setting(db, SPEC_MODE_KEY, mode)
    if "backup_max_age_days" in data:
        try:
            days = int(data.get("backup_max_age_days"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="ancienneté maximale : nombre de jours")
        if not 1 <= days <= 365:
            raise HTTPException(status_code=400, detail="ancienneté maximale : 1 à 365 jours")
        set_setting(db, BACKUP_AGE_KEY, days)
    if "vm_model" in data:
        try:
            model = V.validate_vm_model(data.get("vm_model") or {})
        except V.VmError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _host(db, model["host_id"])
        set_setting(db, VM_MODEL_KEY, model)
    db.commit()
    _audit(db, user, "spec-settings", "spec mining", "ok")
    return read_spec_settings(db, _user=user)


@router.post("/spec/host-check")
async def host_check(data: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    """Détection seulement : libvirt utilisable, ou « Proxmox : pilote à venir »."""
    machine = _host(db, int(data.get("machine_id") or 0))
    simulated = simulation_enabled(db)
    result = await VR.hypervisor(machine, simulated=simulated)
    if result.get("driver") == "proxmox":
        # Détection uniquement : aucun pilote Proxmox n'est branché à cette étape.
        result["message"] = "Proxmox : pilote à venir."
    return result


# ── VM ───────────────────────────────────────────────────────────────
@router.get("/vms")
def list_vms(
    campaign_id: int | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    query = db.query(SpecVm)
    if campaign_id:
        query = query.filter(SpecVm.campaign_id == campaign_id)
    rows = query.order_by(SpecVm.id.desc()).limit(200).all()
    return [vm_to_dict(db, row) for row in rows]


@router.get("/vms/{vm_id}")
def read_vm(vm_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    return vm_to_dict(db, _vm(db, vm_id))


@router.get("/vms/{vm_id}/collect")
async def collect_vm(vm_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    row = _vm(db, vm_id)
    machine = db.query(Machine).filter(Machine.id == row.host_machine_id).first()
    if machine is None:
        raise HTTPException(status_code=400, detail="Machine hôte introuvable")
    simulated = simulation_enabled(db)
    from app.routers.miners import profile_to_dict
    from app.models import MinerProfile

    profile = None
    if row.campaign_id:
        campaign = db.query(Campaign).filter(Campaign.id == row.campaign_id).first()
        if campaign is not None:
            found = db.query(MinerProfile).filter(MinerProfile.id == campaign.profile_id).first()
            profile = profile_to_dict(found) if found is not None else None
    result = await VR.collect(machine, row.name, row.ip, profile, simulated=simulated)
    if not simulated and result.get("state"):
        row.state = "running" if result["state"] == "running" else result["state"]
        row.updated_at = datetime.now(timezone.utc).isoformat()
        db.commit()
    return result


@router.post("/vms/{vm_id}/backup-check")
async def check_backup(vm_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    """§10.11 : le fichier existe, n'est pas vide et est récent — jamais lu."""
    row = _vm(db, vm_id)
    machine = db.query(Machine).filter(Machine.id == row.host_machine_id).first()
    if machine is None:
        raise HTTPException(status_code=400, detail="Machine hôte introuvable")
    if not row.backup_path:
        raise HTTPException(status_code=400, detail="Aucun chemin de sauvegarde enregistré")
    simulated = simulation_enabled(db)
    verdict = await VR.check_backup(
        machine,
        row.ip,
        row.backup_path,
        now_epoch=int(time.time()),
        max_age_days=backup_max_age_days(db),
        simulated=simulated,
    )
    if not simulated:
        row.backup_state = json.dumps(verdict)
        row.backup_checked_at = datetime.now(timezone.utc).isoformat()
        db.commit()
        _audit(
            db,
            user,
            "vm-backup-check",
            row.name,
            "ok" if verdict.get("verified") else "fail",
            verdict.get("reason", ""),
        )
    return verdict


@router.post("/vms/{vm_id}/stop")
async def stop_vm(vm_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    """Éteint la VM **en la conservant** : le wallet y est."""
    row = _vm(db, vm_id)
    machine = db.query(Machine).filter(Machine.id == row.host_machine_id).first()
    if machine is None:
        raise HTTPException(status_code=400, detail="Machine hôte introuvable")
    simulated = simulation_enabled(db)
    if not simulated:
        live = await VR.list_vms(machine, simulated=False)
        found = next((item for item in live["vms"] if item["name"] == row.name), None)
        if found is None:
            raise HTTPException(status_code=404, detail="VM absente de l'hôte")
        try:
            V.ensure_owned(found["name"], found["description"])
        except V.VmError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    result = await VR.shutdown(machine, row.name, simulated=simulated)
    if not simulated:
        row.state = "shut off"
        db.commit()
        _audit(db, user, "vm-stop", row.name, "ok", "VM éteinte, conservée")
    return result


@router.post("/vms/{vm_id}/destroy")
async def destroy_vm(vm_id: int, data: dict, db: Session = Depends(get_db), user=Depends(require_login)):
    """Action séparée et explicite : confirmation + sauvegarde vérifiée (§10.11)."""
    row = _vm(db, vm_id)
    if str(data.get("confirm") or "").strip().upper() != DESTROY_WORD:
        raise HTTPException(
            status_code=400,
            detail=f"Destruction refusée : saisissez « {DESTROY_WORD} » pour confirmer",
        )
    machine = db.query(Machine).filter(Machine.id == row.host_machine_id).first()
    if machine is None:
        raise HTTPException(status_code=400, detail="Machine hôte introuvable")
    backup = None
    if row.backup_state:
        try:
            backup = json.loads(row.backup_state)
        except (TypeError, ValueError):
            backup = None
    simulated = simulation_enabled(db)
    description = V.OWNER_MARKER
    if not simulated:
        live = await VR.list_vms(machine, simulated=False)
        found = next((item for item in live["vms"] if item["name"] == row.name), None)
        if found is None:
            raise HTTPException(status_code=404, detail="VM absente de l'hôte")
        description = found["description"]
    try:
        result = await VR.destroy(
            machine,
            row.name,
            description,
            backup=backup,
            now_epoch=int(time.time()),
            max_age_days=backup_max_age_days(db),
            simulated=simulated,
        )
    except V.VmError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not simulated:
        row.state = "destroyed"
        row.destroyed_at = datetime.now(timezone.utc).isoformat()
        db.commit()
    _audit(db, user, "vm-destroy", row.name, "simulated" if simulated else "ok",
           "destruction après sauvegarde vérifiée")
    return result


def record_vm(
    db: Session,
    *,
    campaign_id: int | None,
    host_machine_id: int,
    name: str,
    mode: str,
    model: dict,
    wallet_address: str = "",
) -> SpecVm:
    """Enregistre la VM dès sa création (le Suivi la rattache à sa campagne)."""
    row = SpecVm(
        uuid=str(uuid_lib.uuid4()),
        campaign_id=campaign_id,
        host_machine_id=host_machine_id,
        name=name,
        mode=mode,
        base_image=model.get("base_image_name") or "",
        overlay_path=V.overlay_path(name),
        seed_path=V.seed_path(name),
        vcpus=int(model.get("vcpus") or 2),
        memory_mb=int(model.get("memory_mb") or 2048),
        disk_gb=int(model.get("disk_gb") or 20),
        network=str(model.get("network") or "default"),
        node_port=int(model.get("node_port") or V.DEFAULT_NODE_PORT),
        state="creating",
        wallet_address=wallet_address,
        backup_path=str(model.get("wallet_backup_path") or ""),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
