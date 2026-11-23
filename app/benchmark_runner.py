"""Pilote du benchmark a la demande de la limite de puissance (SPEC 10.4).

Un seul benchmark a la fois (409 cote API). Le mineur du token par defaut est
arrete sur la machine, le profil de l'algo est lance, la limite de puissance
descend palier par palier, puis le token par defaut revient **verifie**.
Le journal ``benchmark_events`` porte chaque etape ; il est relu tel quel par
l'API, sans recomputation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid as uuid_lib
from datetime import datetime
from typing import Any

from app import benchmark as B
from app.models import (
    AuditLog,
    BenchmarkEvent,
    BenchmarkProfile,
    BenchmarkRun,
    BenchmarkStep,
    Device,
    Machine,
    MinerProfile,
)

logger = logging.getLogger("fossor.benchmark")

ACTIVE_STATUSES = ("queued", "running", "stopping")
TERMINAL_STATUSES = ("done", "failed", "cancelled", "restore_failed")
POLL_S = 15.0
_LOCK: asyncio.Lock | None = None
#: Tâches de fond du benchmark, par identifiant de run. Une seule boucle.
_TASKS: dict[int, asyncio.Task] = {}


def lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


def active_run(db: Any, machine_id: Any = None) -> Any:
    """Benchmark en cours, globalement ou sur une machine donnee."""
    query = db.query(BenchmarkRun).filter(BenchmarkRun.status.in_(ACTIVE_STATUSES))
    if machine_id is not None:
        query = query.filter(BenchmarkRun.machine_id == int(machine_id))
    return query.order_by(BenchmarkRun.id.asc()).first()


def machine_muted(db: Any, machine_id: Any) -> bool:
    """Le watchdog reste muet sur les devices pendant un benchmark."""
    return active_run(db, machine_id) is not None


def journal(db: Any, run: Any, kind: str, message: str, *, result: str = "ok",
            simulated: bool = False, detail: Any = None) -> None:
    db.add(
        BenchmarkEvent(
            run_id=run.id,
            ts=B.iso(B.now_utc()),
            kind=kind[:40],
            message=message[:1000],
            result=result,
            simulated=bool(simulated),
            detail=json.dumps(detail, ensure_ascii=False)[:2000] if detail is not None else "",
        )
    )
    db.commit()


def audit(db: Any, run: Any, action: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor="benchmark",
            action=action[:50],
            object_ref=f"benchmark #{run.id} {run.algo}"[:255],
            result=result[:200],
            output=output[:500],
        )
    )
    db.commit()


def notify(db: Any, run: Any, message: str) -> None:
    from app.campaign_runner import notify as enqueue

    enqueue(db, "benchmark", message)


# ── Portee ────────────────────────────────────────────────────────────
def devices_for(db: Any, run: Any) -> list[Any]:
    """GPU concernes : le device choisi, ou tous les GPU presents de la machine."""
    query = db.query(Device).filter(
        Device.machine_id == run.machine_id,
        Device.kind == "gpu",
        Device.present == True,  # noqa: E712 — colonne SQL
    )
    if run.device_uuid:
        query = query.filter(Device.uuid == run.device_uuid)
    rows = query.all()
    return sorted(rows, key=lambda row: (int(row.index or 0), row.uuid))


def resolve_profile(db: Any, machine: Any, algo: str | None = None) -> Any:
    """Profil de l'algo demande **sur cette machine**, sinon son token par defaut.

    Le profil par defaut de la machine passe avant la recherche par coin : un
    benchmark sur une machine doit tourner avec le binaire et la ligne de
    commande que cette machine utilise.  Chercher le premier profil du coin
    dans toute la base ramenait celui d'une autre machine (binaire absent) et
    le run echouait en 20 ms, sans aucun palier.
    """
    from app.campaign_runner import default_profile_for

    default = default_profile_for(db, machine)
    wanted = str(algo).strip().upper() if algo else None
    if default is not None and (not wanted or (default.coin or "").upper() == wanted):
        return default
    if wanted:
        row = (
            db.query(MinerProfile)
            .filter(MinerProfile.coin == wanted)
            .order_by(MinerProfile.id.asc())
            .first()
        )
        if row is not None:
            return row
    return default


# ── Bornes et application ─────────────────────────────────────────────
async def bounds_for(db: Any, machine: Any, device: Any, simulated: bool) -> dict[str, float]:
    """Bornes min/max/defaut de la carte, lues par nvidia-smi ou simulees."""
    if simulated:
        return dict(B.SIM_BOUNDS)
    from app import ssh_client as S

    result = await S.ssh_gpu_power_limits(machine)
    limits = (result or {}).get("limits") or {}
    bounds = limits.get(int(device.index or 0))
    if not bounds:
        raise RuntimeError(
            f"bornes de power limit inconnues sur {device.model or 'GPU'} "
            f"#{device.index} : aucune mesure nvidia-smi"
        )
    return {"min": float(bounds["min"]), "max": float(bounds["max"]),
            "default": float(bounds["default"])}


async def apply_watts(db: Any, run: Any, machine: Any, device: Any, watts: int,
                      simulated: bool) -> dict[str, Any]:
    """Applique la limite a un GPU — ou l'ecrit comme proposition HiveOS."""
    from app import hiveos as HV

    index = int(device.index or 0)
    if HV.is_hiveos(machine):
        run.hiveos_proposal = B.hiveos_hint(index, watts)[:500]
        journal(db, run, "hiveos-proposal", run.hiveos_proposal, simulated=simulated,
                detail={"gpu": index, "watts": int(watts)})
        db.commit()
        return {"ok": True, "applied": False, "hiveos": True, "watts": int(watts)}
    if simulated:
        journal(db, run, "power-limit", f"GPU {index} : {int(watts)} W (simule)",
                simulated=True, detail={"gpu": index, "watts": int(watts)})
        return {"ok": True, "applied": False, "simulated": True, "watts": int(watts)}
    from app import ssh_client as S

    result = await S.ssh_set_gpu_power_limit(machine, index, watts)
    ok = bool((result or {}).get("ok")) and "password is required" not in str(result.get("stdout", "")).lower()
    journal(
        db, run,
        "power-limit" if ok else "power-limit-failed",
        f"GPU {index} : nvidia-smi -pl {int(watts)} W"
        + ("" if ok else " — refus (droits sudo ?)"),
        result="ok" if ok else "fail",
        detail={"gpu": index, "watts": int(watts), "stdout": str(result.get("stdout", ""))[:400]},
    )
    return {"ok": ok, "applied": ok, "hiveos": False, "watts": int(watts),
            "stdout": (result or {}).get("stdout", "")}


async def reapply(db: Any, run: Any, *, simulated: bool | None = None,
                  backend: Any = None, reason: str = "") -> dict[str, Any]:
    """Rejoue la limite retenue, sans nouveau balayage."""
    from app import pilotage

    machine = db.get(Machine, run.machine_id)
    if machine is None:
        return {"ok": False, "reason": "machine absente"}
    if simulated is None:
        simulated = pilotage.simulated_for(db, machine)
    bests = json.loads(run.bests_json) if run.bests_json else {}
    bounds = json.loads(run.bounds_json) if run.bounds_json else dict(B.SIM_BOUNDS)
    try:
        watts = B.claimed_watts(run.apply_mode or "none", bests, minimum=bounds.get("min"),
                                maximum=bounds.get("max"), default=bounds.get("default"))
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    results = []
    for device in devices_for(db, run):
        results.append(await apply_watts(db, run, machine, device, watts, simulated))
    from app import hiveos as HV

    run.applied_watts = int(watts)
    if not HV.is_hiveos(machine):
        run.applied_at = B.iso(B.now_utc())
        run.applied_uptime_s = machine_uptime(db, machine)
    db.commit()
    suffix = f" — {reason}" if reason else ""
    journal(db, run, "reapplied-after-reboot" if reason == "redemarrage" else "reapplied",
            f"limite {watts} W rejouee (mode {run.apply_mode}){suffix}",
            simulated=simulated, detail=results)
    return {"ok": all(bool(item.get("ok")) for item in results), "watts": int(watts),
            "applied": results}


def machine_uptime(db: Any, machine: Any) -> int:
    """Uptime releve par la derniere collecte (``machine_status``)."""
    from app.models import MachineStatus

    if machine is None or not getattr(machine, "uuid", None):
        return 0
    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    )
    if status is None or status.uptime_s is None:
        return 0
    return int(status.uptime_s)


def reapply_after_reboot(db: Any, machine_id: Any = None) -> list[dict[str, Any]]:
    """Benchmarks dont la limite a ete perdue par un redemarrage."""
    from app.models import MachineStatus

    query = db.query(BenchmarkRun).filter(BenchmarkRun.applied_at != None)  # noqa: E711
    if machine_id is not None:
        query = query.filter(BenchmarkRun.machine_id == int(machine_id))
    pending: list[dict[str, Any]] = []
    for run in query.all():
        machine = db.get(Machine, run.machine_id)
        if machine is None or not getattr(machine, "uuid", None):
            continue
        status = (
            db.query(MachineStatus)
            .filter(MachineStatus.machine_uuid == machine.uuid)
            .first()
        )
        if status is None or not status.online or status.uptime_s is None:
            continue
        if B.needs_reapply(run.applied_uptime_s, status.uptime_s):
            pending.append({"run_id": run.id, "machine_id": run.machine_id,
                            "applied_uptime_s": run.applied_uptime_s,
                            "uptime_s": int(status.uptime_s)})
    return pending


async def apply_pending_reboots(db: Any, machine_id: Any = None) -> list[dict[str, Any]]:
    """Reapplique la limite retenue apres un redemarrage, et le journalise."""
    results: list[dict[str, Any]] = []
    for item in reapply_after_reboot(db, machine_id=machine_id):
        run = db.get(BenchmarkRun, item["run_id"])
        if run is None or run.status in ACTIVE_STATUSES:
            continue
        result = await reapply(db, run, reason="redemarrage")
        results.append({"run_id": run.id, "machine_id": run.machine_id,
                        "watts": result.get("watts"), "ok": bool(result.get("ok")),
                        "uptime_s": item["uptime_s"]})
    return results


# ── Mesure ────────────────────────────────────────────────────────────
def simulated_sample(device: Any, watts: int, percent: int) -> dict[str, Any]:
    """Courbe fictive deterministe : rendement maximal en bas de plage."""
    hashrate = 1000.0 * B.SIM_HASHRATE_PER_WATT * (float(watts) ** 0.7)
    power = round(float(watts) * 0.96, 1)
    accepted = 120
    rejected = 0 if percent >= 70 else 2
    return {
        "hashrate_hs": round(hashrate, 1),
        "power_w": power,
        "temperature_c": round(40.0 + 30.0 * percent / 100.0, 1),
        "accepted": accepted,
        "rejected": rejected,
    }


async def measure(db: Any, run: Any, machine: Any, device: Any, profile_dict: dict[str, Any],
                  backend: Any, simulated: bool, step: Any) -> dict[str, Any]:
    """Mesure stable sur N minutes : hashrate (API mineur), watts, temperature."""
    from app import ssh_client as S

    samples = max(1, int(run.measure_min or 1))
    last: dict[str, Any] = {}
    for position in range(samples):
        if simulated:
            last = simulated_sample(device, int(step.watts), int(step.percent))
        else:
            if position:
                await asyncio.sleep(60)
            hashrate = await backend.hashrate(machine, profile_dict)
            probe = await S.ssh_benchmark_probe(machine)
            gpu = (probe.get("gpus") or {}).get(int(device.index or 0), {})
            last = {
                "hashrate_hs": hashrate,
                "power_w": gpu.get("power_w"),
                "temperature_c": gpu.get("temperature_c"),
                "accepted": int(gpu.get("accepted") or 0),
                "rejected": int(gpu.get("rejected") or 0),
            }
    sample = {
        "hashrate_hs": last.get("hashrate_hs"),
        "power_w": last.get("power_w"),
        "temperature_c": last.get("temperature_c"),
        "accepted": int(last.get("accepted") or 0),
        "rejected": int(last.get("rejected") or 0),
    }
    sample["reject_pct"] = B.reject_pct(sample["rejected"], sample["accepted"])
    return sample


# ── Balayage ──────────────────────────────────────────────────────────
async def sweep_device(db: Any, run: Any, machine: Any, device: Any,
                       profile_dict: dict[str, Any], backend: Any, simulated: bool,
                       rows: list[dict[str, Any]]) -> None:
    from app.campaign_runner import Target, render_argv

    target = Target(machine=machine, device_uuids=[device.uuid], gpus=[int(device.index or 0)])
    argv = render_argv(profile_dict, machine, target)
    bounds = await bounds_for(db, machine, device, simulated)
    run.bounds_json = json.dumps(bounds)
    levels = B.levels(bounds["min"], bounds["max"], bounds["default"])
    run.levels_json = json.dumps(levels)
    db.commit()
    journal(db, run, "sweep-start",
            f"{device.model or 'GPU'} #{device.index} : {len(levels)} paliers de "
            f"{levels[0]['watts']} W a {levels[-1]['watts']} W", simulated=simulated,
            detail={"bounds": bounds})
    await backend.start(machine, profile_dict, argv, simulated)
    first_hashrate: float | None = None
    try:
        for level in levels:
            if run.stop_requested:
                break
            step = BenchmarkStep(
                run_id=run.id, device_uuid=device.uuid, gpu_index=int(device.index or 0),
                percent=level["percent"], watts=level["watts"], phase="measure",
                started_at=B.iso(B.now_utc()), result="running",
            )
            db.add(step)
            db.commit()
            await apply_watts(db, run, machine, device, int(level["watts"]), simulated)
            if not simulated and int(run.warmup_s or 0):
                await asyncio.sleep(int(run.warmup_s))
            sample = await measure(db, run, machine, device, profile_dict, backend, simulated, step)
            step.hashrate_hs = sample["hashrate_hs"]
            step.power_w = sample["power_w"]
            step.temperature_c = sample["temperature_c"]
            step.accepted = sample["accepted"]
            step.rejected = sample["rejected"]
            step.reject_pct = sample["reject_pct"]
            step.ended_at = B.iso(B.now_utc())
            reason = B.stop_reason(sample, first_hashrate=first_hashrate,
                                   temp_limit_c=float(run.temp_limit_c or B.DEFAULT_TEMP_LIMIT_C))
            if reason:
                step.result = "stopped"
                step.stop_reason = reason[:300]
                db.commit()
                journal(db, run, "level-stopped", f"{level['watts']} W : {reason}",
                        result="fail", simulated=simulated)
                rows.append(step_row(step))
                break
            step.result = "ok"
            db.commit()
            if first_hashrate is None and sample["hashrate_hs"]:
                first_hashrate = float(sample["hashrate_hs"])
            rows.append(step_row(step))
    finally:
        try:
            await backend.stop(machine, profile_dict, simulated)
        except Exception as exc:  # arreter le mineur ne doit jamais masquer l'erreur
            journal(db, run, "miner-stop-failed", str(exc)[:400], result="fail",
                    simulated=simulated)


def step_row(step: Any) -> dict[str, Any]:
    return {
        "device_uuid": step.device_uuid,
        "gpu_index": step.gpu_index,
        "percent": step.percent,
        "watts": step.watts,
        "hashrate_hs": step.hashrate_hs,
        "power_w": step.power_w,
        "temperature_c": step.temperature_c,
        "accepted": step.accepted,
        "rejected": step.rejected,
        "reject_pct": step.reject_pct,
        "result": step.result,
        "stop_reason": step.stop_reason,
    }


# ── Retour au token par defaut, verifie ───────────────────────────────
async def restore_default(db: Any, run: Any, machine: Any, backend: Any,
                          simulated: bool, reason: str = "") -> dict[str, Any]:
    from app import hiveos as HV
    from app.campaign_runner import (
        Target, _profile_dict, default_profile_for, hiveos_ack, hiveos_probe_data,
        render_argv, restore_timeout,
    )

    profile = default_profile_for(db, machine)
    if profile is None:
        run.restore_status = "unverifiable"
        journal(db, run, "default-none",
                "aucun token par defaut : retour non verifiable", simulated=simulated,
                detail={"reason": reason})
        return {"ok": False, "reason": "aucun token par defaut"}
    profile_dict = _profile_dict(profile)
    devices = devices_for(db, run)
    timeout = restore_timeout(db)
    run.restore_deadline = B.iso(B.now_utc())
    verified, source = False, ""
    try:
        if HV.is_hiveos(machine):
            from app import hiveos_remote as HR

            probe = await hiveos_probe_data(db, machine, [int(d.index or 0) for d in devices], simulated)
            resumed = await HR.resume(machine, probe, ack_watchdog=hiveos_ack(db), simulated=simulated)
            verified = bool(resumed.get("verified") and resumed.get("hashrate_hs"))
            source = resumed.get("source") or "last_stat.json"
        else:
            target = Target(machine=machine, gpus=[int(d.index or 0) for d in devices])
            await backend.start(machine, profile_dict, render_argv(profile_dict, machine, target), simulated)
            if simulated:
                verified, source = True, "simulation"
            else:
                verified, source = await verify_default(backend, machine, profile_dict, timeout)
    except Exception as exc:
        journal(db, run, "restore-error", str(exc)[:500], result="fail", simulated=simulated)
    run.verified = bool(verified)
    run.verify_source = source[:60]
    run.restore_status = "ok" if verified else "failed"
    db.commit()
    journal(db, run, "restore-verified" if verified else "restore-failed",
            f"token par defaut {profile.coin} "
            + ("verifie" if verified else "NON verifie") + (f" ({source})" if source else ""),
            result="ok" if verified else "fail", simulated=simulated)
    notify(db, run, f"Benchmark #{run.id} : retour au token par defaut {profile.coin} "
                    + ("verifie" if verified else "EN ECHEC"))
    return {"ok": verified, "source": source}


async def verify_default(backend: Any, machine: Any, profile_dict: dict[str, Any],
                         timeout: int) -> tuple[bool, str]:
    """Le mineur par defaut produit-il du hashrate avant l'echeance ?"""
    deadline = B.now_utc().timestamp() + max(30, int(timeout or 300))
    while B.now_utc().timestamp() < deadline:
        try:
            hashrate = await backend.hashrate(machine, profile_dict)
        except Exception:
            hashrate = None
        if hashrate and float(hashrate) > 0:
            return True, "api-mineur"
        await asyncio.sleep(POLL_S)
    return False, "echeance"


# ── Resultats reutilisables ───────────────────────────────────────────
def store_profiles(db: Any, run: Any, machine: Any, bests: dict[str, Any]) -> None:
    """Resultats par modele de carte et par algo, reutilisables ailleurs."""
    from app.campaign_runner import default_profile_for

    profile = db.get(MinerProfile, run.profile_id) if run.profile_id else default_profile_for(db, machine)
    algo = (run.algo or (getattr(profile, "coin", "") or "")).upper()
    if not algo:
        return
    for device in devices_for(db, run):
        model = (device.model or "").strip()
        if not model:
            continue
        for mode in ("performance", "efficiency"):
            chosen = (bests or {}).get(mode) or {}
            if chosen.get("watts") is None:
                continue
            row = (
                db.query(BenchmarkProfile)
                .filter(BenchmarkProfile.gpu_model == model,
                        BenchmarkProfile.algo == algo,
                        BenchmarkProfile.mode == mode)
                .first()
            )
            if row is None:
                row = BenchmarkProfile(gpu_model=model[:120], algo=algo[:20], mode=mode)
                db.add(row)
            row.watts = int(chosen["watts"])
            row.hashrate_hs = chosen.get("hashrate_hs")
            row.power_w = chosen.get("power_w")
            row.efficiency = chosen.get("efficiency")
            row.run_id = run.id
            row.machine_id = run.machine_id
            row.updated_at = B.iso(B.now_utc())
    db.commit()


# ── Tâche de fond ────────────────────────────────────────────────────
def start_run(db: Any, run: Any) -> bool:
    """Marque le run en file et le deroule dans une tache de fond."""
    existing = _TASKS.get(run.id)
    if existing is not None and not existing.done():
        return False
    if run.status in ACTIVE_STATUSES:
        return False
    run.status = "queued"
    db.commit()
    task = asyncio.create_task(_execute_task(run.id))
    _TASKS[run.id] = task
    task.add_done_callback(lambda _task, rid=run.id: _TASKS.pop(rid, None))
    return True


async def _execute_task(run_id: int) -> None:
    """Execute un benchmark hors de la requete HTTP, sur sa propre session."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        run = db.get(BenchmarkRun, int(run_id))
        if run is None:
            return
        await execute(db, run)
    except Exception as exc:  # une tache de fond ne tue jamais la boucle
        logger.exception("benchmark #%s en echec", run_id)
        try:
            db.rollback()
        except Exception:
            pass
        run = db.get(BenchmarkRun, int(run_id))
        if run is not None:
            journal(db, run, "background-error", str(exc)[:800], result="fail",
                    simulated=bool(run.simulated))
            finish(db, run, "failed", simulated=bool(run.simulated))
    finally:
        db.close()


# ── Cycle complet ─────────────────────────────────────────────────────
def finish(db: Any, run: Any, status: str, *, simulated: bool) -> dict[str, Any]:
    run.status = status
    run.finished_at = B.iso(B.now_utc())
    db.commit()
    journal(db, run, f"finished:{status}", f"benchmark #{run.id} : {status}", simulated=simulated)
    notify(db, run, f"Benchmark #{run.id} termine ({status})")
    audit(db, run, "benchmark-finish", status)
    return {"run_id": run.id, "status": status}


async def execute(db: Any, run: Any, *, backend: Any = None,
                  simulated: bool | None = None) -> dict[str, Any]:
    """Déroule le benchmark : un device a la fois, paliers bornes, retour verifie."""
    from app import pilotage
    from app.campaign_runner import MinerBackend, _profile_dict

    backend = backend or MinerBackend()
    machine = db.get(Machine, run.machine_id)
    if machine is None:
        return finish(db, run, "failed", simulated=bool(run.simulated))
    if simulated is None:
        simulated = pilotage.simulated_for(db, machine)
    run.simulated = bool(simulated)
    if not run.levels_json:
        run.levels_json = json.dumps(
            B.levels(B.SIM_BOUNDS["min"], B.SIM_BOUNDS["max"], B.SIM_BOUNDS["default"])
            if simulated else [])
    run.status = "running"
    run.started_at = B.iso(B.now_utc())
    db.commit()
    notify(db, run, f"Benchmark #{run.id} demarre sur {machine.name} ({run.algo})"
                    + (" [simule]" if simulated else ""))
    journal(db, run, "started", f"benchmark {run.algo} sur {machine.name}",
            simulated=simulated, detail={"run": run.id})

    profile = db.get(MinerProfile, run.profile_id) if run.profile_id else None
    if profile is None:
        journal(db, run, "no-profile", "aucun profil pour l'algo demande",
                result="fail", simulated=simulated)
        return finish(db, run, "failed", simulated=simulated)
    profile_dict = _profile_dict(profile)
    devices = devices_for(db, run)
    if not devices:
        journal(db, run, "no-device", "aucun GPU a tester", result="fail", simulated=simulated)
        return finish(db, run, "failed", simulated=simulated)

    rows: list[dict[str, Any]] = []
    try:
        for device in devices:
            if run.stop_requested:
                break
            await sweep_device(db, run, machine, device, profile_dict, backend, simulated, rows)
    except Exception as exc:
        journal(db, run, "error", str(exc)[:800], result="fail", simulated=simulated)
        await restore_default(db, run, machine, backend, simulated, reason="erreur pendant le balayage")
        return finish(db, run, "failed", simulated=simulated)

    bests = B.pick_bests(rows)
    run.bests_json = json.dumps(bests, ensure_ascii=False)
    db.commit()
    store_profiles(db, run, machine, bests)
    await restore_default(db, run, machine, backend, simulated,
                          reason="annulation" if run.stop_requested else "fin de benchmark")

    # Limite finale : le defaut, ou le mode deja choisi par l'utilisateur.
    try:
        from app import hiveos as HV

        bounds = json.loads(run.bounds_json) if run.bounds_json else dict(B.SIM_BOUNDS)
        watts = B.claimed_watts(run.apply_mode or "none", bests, minimum=bounds.get("min"),
                                maximum=bounds.get("max"), default=bounds.get("default"))
        for device in devices_for(db, run):
            await apply_watts(db, run, machine, device, watts, simulated)
        if not HV.is_hiveos(machine):
            run.applied_watts = int(watts)
            run.applied_at = B.iso(B.now_utc())
            run.applied_uptime_s = machine_uptime(db, machine)
        db.commit()
    except Exception as exc:
        journal(db, run, "apply-failed", str(exc)[:400], result="fail", simulated=simulated)

    status = "cancelled" if run.stop_requested else ("done" if run.restore_status == "ok" else "restore_failed")
    return finish(db, run, status, simulated=simulated)
