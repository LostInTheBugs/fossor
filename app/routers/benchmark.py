"""API du benchmark a la demande (§10.4) : options, validation, lancement, mode."""

from __future__ import annotations

import json
import uuid as uuid_lib

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app import benchmark as B
from app import benchmark_runner as RUN
from app import pilotage
from app.database import get_db
from app.dependencies import require_login
from app.models import BenchmarkEvent, BenchmarkProfile, BenchmarkRun, BenchmarkStep, Device, Machine, MinerProfile

router = APIRouter(prefix="/api", tags=["benchmark"])
SUDOERS_RULE = (
    "fossor ALL=(root) NOPASSWD: /usr/bin/nvidia-smi -i [0-9]* -pl [0-9]*"
)


def run_to_dict(db: Session, run: BenchmarkRun, *, steps: bool = True) -> dict:
    payload = {
        "id": run.id,
        "uuid": run.uuid,
        "machine_id": run.machine_id,
        "device_uuid": run.device_uuid,
        "algo": run.algo,
        "profile_id": run.profile_id,
        "status": run.status,
        "scope": run.scope,
        "measure_min": run.measure_min,
        "warmup_s": run.warmup_s,
        "temp_limit_c": run.temp_limit_c,
        "simulated": bool(run.simulated),
        "stop_requested": bool(run.stop_requested),
        "apply_mode": run.apply_mode,
        "apply_label": B.MODE_LABELS.get(run.apply_mode, run.apply_mode),
        "applied_watts": run.applied_watts,
        "applied_at": run.applied_at,
        "hiveos_proposal": run.hiveos_proposal,
        "restore_status": run.restore_status,
        "verified": bool(run.verified),
        "verify_source": run.verify_source,
        "estimated_s": run.estimated_s,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "note": run.note,
        "created_at": run.created_at,
        "levels": json.loads(run.levels_json) if run.levels_json else [],
        "bounds": json.loads(run.bounds_json) if run.bounds_json else {},
        "bests": json.loads(run.bests_json) if run.bests_json else {},
    }
    if steps:
        rows = (
            db.query(BenchmarkStep)
            .filter(BenchmarkStep.run_id == run.id)
            .order_by(BenchmarkStep.id.asc())
            .all()
        )
        payload["steps"] = [RUN.step_row(row) for row in rows]
        payload["curve"] = B.shape_curve(payload["steps"])
        events = (
            db.query(BenchmarkEvent)
            .filter(BenchmarkEvent.run_id == run.id)
            .order_by(BenchmarkEvent.id.asc())
            .all()
        )
        payload["events"] = [
            {"ts": row.ts, "kind": row.kind, "message": row.message,
             "result": row.result, "simulated": bool(row.simulated)}
            for row in events
        ]
    return payload


def _machine(db: Session, machine_id: int) -> Machine:
    machine = db.get(Machine, int(machine_id))
    if machine is None:
        raise HTTPException(status_code=404, detail="machine inconnue")
    return machine


def _devices(db: Session, machine_id: int, device_uuid: str | None) -> list[Device]:
    query = db.query(Device).filter(Device.machine_id == int(machine_id), Device.kind == "gpu",
                                    Device.present == True)  # noqa: E712
    if device_uuid:
        query = query.filter(Device.uuid == device_uuid)
    return sorted(query.all(), key=lambda row: int(row.index or 0))


@router.get("/benchmark/options")
def options(db: Session = Depends(get_db), user=Depends(require_login)) -> dict:
    machines = []
    for machine in db.query(Machine).order_by(Machine.name.asc()).all():
        devices = _devices(db, machine.id, None)
        if not devices:
            continue
        from app import hiveos as HV
        from app.campaign_runner import default_profile_for

        profile = default_profile_for(db, machine)
        machines.append({
            "id": machine.id,
            "name": machine.name,
            "hiveos": HV.is_hiveos(machine),
            "simulated": pilotage.simulated_for(db, machine),
            "default_algo": getattr(profile, "coin", "") or "",
            "default_profile_id": getattr(profile, "id", None),
            "devices": [
                {"uuid": row.uuid, "index": row.index, "model": row.model or "",
                 "label": f"GPU {row.index} — {row.model or 'inconnu'}"}
                for row in devices
            ],
        })
    profiles = [
        {"id": row.id, "coin": row.coin, "name": row.name,
         "algo": getattr(row, "software", "")}
        for row in db.query(MinerProfile).order_by(MinerProfile.coin.asc()).all()
    ]
    active = RUN.active_run(db)
    return {
        "machines": machines,
        "profiles": profiles,
        "modes": [{"value": key, "label": B.MODE_LABELS[key]} for key in B.MODES],
        "defaults": {
            "measure_min": B.DEFAULT_MEASURE_MIN,
            "warmup_s": B.DEFAULT_WARMUP_S,
            "temp_limit_c": B.DEFAULT_TEMP_LIMIT_C,
            "step_pcts": list(B.STEP_PCTS),
        },
        "active_run": {"id": active.id, "machine_id": active.machine_id, "status": active.status}
        if active else None,
        "sudoers_rule": SUDOERS_RULE,
    }


@router.post("/benchmark/preview")
async def preview(payload: dict = Body(...), db: Session = Depends(get_db),
                  user=Depends(require_login)) -> dict:
    machine = _machine(db, payload.get("machine_id"))
    devices = _devices(db, machine.id, payload.get("device_uuid") or None)
    if not devices:
        raise HTTPException(status_code=400, detail="aucun GPU present sur cette machine")
    profile = RUN.resolve_profile(db, machine, payload.get("algo"))
    if profile is None:
        raise HTTPException(status_code=400, detail="aucun profil pour cet algo")
    simulated = pilotage.simulated_for(db, machine)
    measure_min = int(payload.get("measure_min") or B.DEFAULT_MEASURE_MIN)
    measure_min = min(max(measure_min, B.MIN_MEASURE_MIN), B.MAX_MEASURE_MIN)
    bounds = dict(B.SIM_BOUNDS)
    if not simulated:
        try:
            bounds = await RUN.bounds_for(db, machine, devices[0], simulated)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"bornes de power limit illisibles : {exc}")
    levels = B.levels(bounds["min"], bounds["max"], bounds["default"])
    power = sum(float(getattr(row, "power_w", 0) or 0) for row in devices) or None
    hashrate = sum(float(getattr(row, "hashrate_hs", 0) or 0) for row in devices) or None
    from app import hiveos as HV

    return {
        "machine": {"id": machine.id, "name": machine.name},
        "hiveos": HV.is_hiveos(machine),
        "simulated": simulated,
        "algo": profile.coin,
        "profile_id": profile.id,
        "devices": [{"uuid": row.uuid, "index": row.index, "model": row.model or ""} for row in devices],
        "levels": levels,
        "bounds": bounds,
        "bounds_source": "simulees" if simulated else "nvidia-smi",
        "duration_s": B.estimate(level_count=len(levels) * len(devices),
                                 warmup_s=int(payload.get("warmup_s") or B.DEFAULT_WARMUP_S),
                                 measure_min=measure_min),
        "hashrate_lost_hs": hashrate,
        "power_w": power,
        "measure_min": measure_min,
        "warmup_s": int(payload.get("warmup_s") or B.DEFAULT_WARMUP_S),
        "active_run": RUN.active_run(db).id if RUN.active_run(db) else None,
        "note": (
            "Rig HiveOS : le benchmark est possible, mais HiveOS ecrase nvidia-smi -pl. "
            "Fossor affichera la valeur a saisir dans l'OC, il ne l'appliquera pas."
            if HV.is_hiveos(machine) else ""
        ),
    }


@router.post("/benchmark")
async def create(payload: dict = Body(...), db: Session = Depends(get_db),
                 user=Depends(require_login)) -> dict:
    active = RUN.active_run(db)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"benchmark #{active.id} deja en cours : une seule machine a la fois",
        )
    machine = _machine(db, payload.get("machine_id"))
    devices = _devices(db, machine.id, payload.get("device_uuid") or None)
    if not devices:
        raise HTTPException(status_code=400, detail="aucun GPU present sur cette machine")
    profile = RUN.resolve_profile(db, machine, payload.get("algo"))
    if profile is None:
        raise HTTPException(status_code=400, detail="aucun profil pour cet algo")
    measure_min = int(payload.get("measure_min") or B.DEFAULT_MEASURE_MIN)
    measure_min = min(max(measure_min, B.MIN_MEASURE_MIN), B.MAX_MEASURE_MIN)
    run = BenchmarkRun(
        uuid=str(uuid_lib.uuid4()),
        machine_id=machine.id,
        device_uuid=payload.get("device_uuid") or None,
        algo=profile.coin,
        profile_id=profile.id,
        status="proposed",
        scope="device" if payload.get("device_uuid") else "all",
        measure_min=measure_min,
        warmup_s=int(payload.get("warmup_s") or B.DEFAULT_WARMUP_S),
        temp_limit_c=float(payload.get("temp_limit_c") or B.DEFAULT_TEMP_LIMIT_C),
        apply_mode=B.normalize_mode(payload.get("apply_mode")),
        created_at=B.iso(B.now_utc()),
    )
    db.add(run)
    db.commit()
    return run_to_dict(db, run, steps=False)


@router.get("/benchmark")
def index(db: Session = Depends(get_db), user=Depends(require_login)) -> dict:
    rows = db.query(BenchmarkRun).order_by(BenchmarkRun.id.desc()).limit(50).all()
    return {"runs": [run_to_dict(db, row, steps=False) for row in rows],
            "active": RUN.active_run(db).id if RUN.active_run(db) else None}


@router.get("/benchmark/profiles")
def profiles(gpu_model: str | None = None, algo: str | None = None,
             db: Session = Depends(get_db), user=Depends(require_login)) -> dict:
    query = db.query(BenchmarkProfile)
    if gpu_model:
        query = query.filter(BenchmarkProfile.gpu_model == gpu_model)
    if algo:
        query = query.filter(BenchmarkProfile.algo == algo.upper())
    return {"profiles": [
        {"gpu_model": row.gpu_model, "algo": row.algo, "mode": row.mode,
         "mode_label": B.MODE_LABELS.get(row.mode, row.mode), "watts": row.watts,
         "hashrate_hs": row.hashrate_hs, "power_w": row.power_w,
         "efficiency": row.efficiency, "updated_at": row.updated_at}
        for row in query.order_by(BenchmarkProfile.gpu_model.asc()).all()
    ]}


@router.get("/benchmark/{run_id}")
def detail(run_id: int, db: Session = Depends(get_db), user=Depends(require_login)) -> dict:
    run = db.get(BenchmarkRun, int(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="benchmark inconnu")
    return run_to_dict(db, run)


@router.post("/benchmark/{run_id}/execute")
async def execute(run_id: int, db: Session = Depends(get_db), user=Depends(require_login)) -> dict:
    run = db.get(BenchmarkRun, int(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="benchmark inconnu")
    if run.status in RUN.ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="benchmark deja en cours")
    result = await RUN.execute(db, run)
    return {"result": result, "run": run_to_dict(db, run)}


@router.post("/benchmark/{run_id}/cancel")
def cancel(run_id: int, db: Session = Depends(get_db), user=Depends(require_login)) -> dict:
    run = db.get(BenchmarkRun, int(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="benchmark inconnu")
    run.stop_requested = True
    if run.status in RUN.ACTIVE_STATUSES:
        run.status = "stopping"
    db.commit()
    RUN.journal(db, run, "cancel-requested", "annulation demandee", simulated=bool(run.simulated))
    return run_to_dict(db, run, steps=False)


@router.post("/benchmark/{run_id}/apply")
async def apply(run_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
                user=Depends(require_login)) -> dict:
    run = db.get(BenchmarkRun, int(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="benchmark inconnu")
    try:
        run.apply_mode = B.normalize_mode(payload.get("mode"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not run.bests_json and run.apply_mode != "none":
        raise HTTPException(status_code=409, detail="aucun palier mesure pour ce mode")
    db.commit()
    result = await RUN.reapply(db, run)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason") or "application impossible")
    return {"result": result, "run": run_to_dict(db, run)}


@router.post("/benchmark/{run_id}/reapply")
async def reapply(run_id: int, db: Session = Depends(get_db), user=Depends(require_login)) -> dict:
    run = db.get(BenchmarkRun, int(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="benchmark inconnu")
    result = await RUN.reapply(db, run)
    return {"result": result, "run": run_to_dict(db, run, steps=False)}
