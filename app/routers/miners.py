"""Miner profiles, simulation switch and host-mode start/stop (§4.5, §4.6, §5.7)."""

from __future__ import annotations

import json
import uuid as uuid_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import miner_remote as R
from app import miners as M
from app.ai_config import get_setting, set_setting
from app.database import get_db
from app.dependencies import require_login
from app.models import ActorEnum, AuditLog, Device, Machine, MinerProfile

router = APIRouter(prefix="/api", tags=["miners"])

SIMULATION_KEY = "simulation_mode"
DEFAULT_PROFILE_KEY = "default_miner_profile_id"
CONFIRM_WORD = "PILOTER"

PROFILE_FIELDS = (
    "name",
    "coin",
    "wallet",
    "pool",
    "software",
    "binary_url",
    "sha256",
    "binary_path",
    "version",
    "cmdline",
    "api_port",
    "execution_mode",
    "threads",
    "enabled",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def simulation_enabled(db: Session) -> bool:
    """§5.7: the dry-run switch, **on** unless explicitly disabled."""
    value = get_setting(db, SIMULATION_KEY, True)
    return True if not isinstance(value, bool) else value


def default_profile_id(db: Session) -> int | None:
    value = get_setting(db, DEFAULT_PROFILE_KEY, None)
    return value if isinstance(value, int) else None


def profile_to_dict(profile: MinerProfile) -> dict:
    data = {field: getattr(profile, field) for field in PROFILE_FIELDS}
    data.update(
        {
            "id": profile.id,
            "uuid": profile.uuid,
            "enabled": bool(profile.enabled),
            "binary": M.binary_path({"software": profile.software, "version": profile.version,
                                     "binary_path": profile.binary_path}),
            "service": M.unit_name({"name": profile.name}),
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }
    )
    return data


def _audit(db: Session, machine: Machine | None, action: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor=ActorEnum.fred,
            action=action,
            machine_id=machine.id if machine else None,
            object_ref=machine.name if machine else None,
            result=result,
            output=output[:500],
        )
    )
    db.commit()


def _get_profile(db: Session, profile_id: int) -> MinerProfile:
    profile = db.query(MinerProfile).filter(MinerProfile.id == profile_id).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profil de minage introuvable")
    return profile


def _selected_profile(db: Session, machine: Machine, requested: int | None) -> MinerProfile:
    if requested:
        return _get_profile(db, requested)
    if machine.default_miner_profile_id:
        return _get_profile(db, machine.default_miner_profile_id)
    global_id = default_profile_id(db)
    if global_id:
        return _get_profile(db, global_id)
    raise HTTPException(
        status_code=400,
        detail="Aucun profil sélectionné : choisissez un profil ou définissez le token par défaut",
    )


def _gpu_indices(db: Session, machine: Machine) -> list[int]:
    rows = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.kind == "gpu")
        .order_by(Device.index.asc())
        .all()
    )
    return sorted(d.index for d in rows if d.present and d.enabled_for_mining and d.index is not None)


def _cpu_threads(db: Session, machine: Machine) -> int:
    cpu = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.kind == "cpu", Device.present == True)  # noqa: E712
        .first()
    )
    return int(cpu.threads or 1) if cpu else 1


def _require_pilotable(machine: Machine) -> None:
    if machine.auth_type != "fossor_key":
        raise HTTPException(
            status_code=400,
            detail="Le pilotage exige une machine en « clé Fossor » (compte dédié avec sudo)",
        )


# ── Settings ──────────────────────────────────────────────────────────
@router.get("/miner/settings")
def read_miner_settings(db: Session = Depends(get_db), _user=Depends(require_login)):
    profiles = db.query(MinerProfile).order_by(MinerProfile.name.asc()).all()
    return {
        "simulation": simulation_enabled(db),
        "confirm_word": CONFIRM_WORD,
        "default_profile_id": default_profile_id(db),
        "profiles": [profile_to_dict(p) for p in profiles],
    }


@router.put("/miner/settings")
def write_miner_settings(
    data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    if "simulation" in data:
        wanted = bool(data.get("simulation"))
        if not wanted:
            if str(data.get("confirm") or "").strip() != CONFIRM_WORD:
                raise HTTPException(
                    status_code=400,
                    detail=f"Désactivation refusée : saisissez « {CONFIRM_WORD} » pour confirmer",
                )
        set_setting(db, SIMULATION_KEY, wanted)
        db.commit()
        _audit(
            db,
            None,
            "simulation",
            "ok",
            "simulation désactivée (confirmation saisie)" if not wanted else "simulation activée",
        )
    if "default_profile_id" in data:
        value = data.get("default_profile_id")
        if value in (None, "", 0):
            set_setting(db, DEFAULT_PROFILE_KEY, None)
        else:
            profile = _get_profile(db, int(value))
            set_setting(db, DEFAULT_PROFILE_KEY, profile.id)
        db.commit()
        _audit(db, None, "default-profile", "ok", f"profil par défaut : {data.get('default_profile_id')}")
    return read_miner_settings(db=db, _user=None)


# ── Profiles CRUD ─────────────────────────────────────────────────────
@router.get("/miner/profiles")
def list_profiles(db: Session = Depends(get_db), _user=Depends(require_login)):
    rows = db.query(MinerProfile).order_by(MinerProfile.name.asc()).all()
    return [profile_to_dict(p) for p in rows]


@router.post("/miner/profiles", status_code=201)
def create_profile(data: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    try:
        clean = M.validate_profile(data)
    except M.ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if db.query(MinerProfile).filter(MinerProfile.name == clean["name"]).first():
        raise HTTPException(status_code=400, detail="Un profil porte déjà ce nom")
    profile = MinerProfile(uuid=str(uuid_lib.uuid4()), enabled=bool(data.get("enabled", True)), **{
        key: clean[key]
        for key in PROFILE_FIELDS
        if key in clean and key not in ("enabled",)
    })
    db.add(profile)
    db.commit()
    db.refresh(profile)
    _audit(db, None, "miner-profile-create", "ok", f"{profile.name} ({profile.coin})")
    return profile_to_dict(profile)


@router.get("/miner/profiles/{profile_id}")
def read_profile(profile_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    return profile_to_dict(_get_profile(db, profile_id))


@router.put("/miner/profiles/{profile_id}")
def update_profile(
    profile_id: int, data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    profile = _get_profile(db, profile_id)
    merged = {field: getattr(profile, field) for field in PROFILE_FIELDS}
    merged.update({k: v for k, v in data.items() if k in PROFILE_FIELDS})
    try:
        clean = M.validate_profile(merged)
    except M.ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for field in PROFILE_FIELDS:
        if field in clean:
            setattr(profile, field, clean[field])
    if "enabled" in data:
        profile.enabled = bool(data["enabled"])
    profile.updated_at = _now()
    db.commit()
    db.refresh(profile)
    _audit(db, None, "miner-profile-update", "ok", profile.name)
    return profile_to_dict(profile)


@router.delete("/miner/profiles/{profile_id}")
def delete_profile(profile_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    profile = _get_profile(db, profile_id)
    name = profile.name
    db.query(Machine).filter(Machine.default_miner_profile_id == profile.id).update(
        {Machine.default_miner_profile_id: None}
    )
    if default_profile_id(db) == profile.id:
        set_setting(db, DEFAULT_PROFILE_KEY, None)
    db.delete(profile)
    db.commit()
    _audit(db, None, "miner-profile-delete", "ok", name)
    return {"ok": True}


# ── Machine miner actions ─────────────────────────────────────────────
def _machine(db: Session, machine_id: int) -> Machine:
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine introuvable")
    return machine


@router.get("/machines/{machine_id}/miner")
def machine_miner_view(
    machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    machine = _machine(db, machine_id)
    profiles = db.query(MinerProfile).filter(MinerProfile.enabled == True).order_by(  # noqa: E712
        MinerProfile.name.asc()
    ).all()
    chosen = machine.default_miner_profile_id or default_profile_id(db)
    return {
        "simulation": simulation_enabled(db),
        "machine_id": machine.id,
        "auth_type": machine.auth_type,
        "pilotable": machine.auth_type == "fossor_key",
        "selected_profile_id": chosen,
        "gpus": _gpu_indices(db, machine),
        "threads": _cpu_threads(db, machine),
        "profiles": [profile_to_dict(p) for p in profiles],
    }


@router.post("/machines/{machine_id}/miner/start")
async def start_miner(
    machine_id: int, data: dict | None = None, db: Session = Depends(get_db), _user=Depends(require_login)
):
    data = data or {}
    machine = _machine(db, machine_id)
    _require_pilotable(machine)
    profile = _selected_profile(db, machine, data.get("profile_id"))
    if not profile.enabled:
        raise HTTPException(status_code=400, detail="Profil désactivé")
    try:
        gpus = _gpu_indices(db, machine)
        threads = int(data.get("threads") or profile.threads or _cpu_threads(db, machine))
        values = M.build_values(
            profile_to_dict(profile),
            worker=M.worker_name(machine.name),
            gpus=gpus,
            threads=threads,
            api_port=profile.api_port,
        )
        argv = M.render_argv(profile_to_dict(profile), values)
    except (M.ProfileError, ValueError) as exc:
        _audit(db, machine, "miner-start", "fail", str(exc))
        raise HTTPException(status_code=400, detail=str(exc))

    simulated = simulation_enabled(db)
    try:
        result = await R.start(machine, profile_to_dict(profile), argv, simulated=simulated, user=R.service_user(machine))
    except R.MinerError as exc:
        _audit(db, machine, "miner-start", "fail", f"{profile.name} : {exc}")
        raise HTTPException(status_code=409, detail=str(exc))

    result["profile"] = profile.name
    result["argv"] = argv
    _audit(
        db,
        machine,
        "miner-start",
        "simulated" if simulated else "ok",
        json.dumps({"profil": profile.name, "commandes": result.get("commands", [])}, ensure_ascii=False),
    )
    return result


@router.post("/machines/{machine_id}/miner/stop")
async def stop_miner(
    machine_id: int, data: dict | None = None, db: Session = Depends(get_db), _user=Depends(require_login)
):
    data = data or {}
    machine = _machine(db, machine_id)
    _require_pilotable(machine)
    profile = _selected_profile(db, machine, data.get("profile_id"))
    simulated = simulation_enabled(db)
    try:
        result = await R.stop(
            machine, profile_to_dict(profile), simulated=simulated, user=R.service_user(machine)
        )
    except R.MinerError as exc:
        _audit(db, machine, "miner-stop", "fail", f"{profile.name} : {exc}")
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(
        db,
        machine,
        "miner-stop",
        "simulated" if simulated else "ok",
        json.dumps({"profil": profile.name, "commandes": result.get("commands", [])}, ensure_ascii=False),
    )
    return result


@router.get("/machines/{machine_id}/miner/status")
async def miner_status(
    machine_id: int,
    profile_id: int | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    machine = _machine(db, machine_id)
    _require_pilotable(machine)
    profile = _selected_profile(db, machine, profile_id)
    simulated = simulation_enabled(db)
    try:
        result = await R.status(
            machine, profile_to_dict(profile), simulated=simulated, user=R.service_user(machine)
        )
    except R.MinerError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    result["profile"] = profile.name
    result["simulation"] = simulated
    _audit(
        db,
        machine,
        "miner-status",
        "simulated" if simulated else "ok",
        json.dumps({"profil": profile.name}, ensure_ascii=False),
    )
    return result
