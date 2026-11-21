"""Planificateur du mode boost (§10.3).

Le boost est une **campagne courte et bornée** sur le pool de boost : entrée
(`wd stop` → `miner stop` → mineur de campagne), sortie (`miner start` →
`wd start` → token par défaut vérifié).  La séquence HiveOS est celle de
:mod:`app.hiveos_remote` — elle n'est pas réécrite ici.

Comme pour les campagnes, le journal ``boost_events`` **est** l'idempotence :
une étape écrite en ``result="ok"`` n'est jamais rejouée.
"""

from __future__ import annotations

import asyncio
import json
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any

from app import boost as B
from app import campaigns as C
from app.campaign_runner import (
    MinerBackend,
    Target,
    default_profile_for,
    hiveos_probe_data,
    machines_online,
    measure_hashrate,
    notify,
    psu_worst_pct,
    restore_timeout,
)
from app.models import (
    AuditLog,
    Boost,
    BoostDevice,
    BoostEvent,
    Campaign,
    Coin,
    Device,
    Machine,
    MinerProfile,
)

_LOCK: asyncio.Lock | None = None
MAX_RESTORE_ATTEMPTS = 2
BOOST_SWITCHING = ("approved", "stopping")


class BoostRunError(RuntimeError):
    """Boost impossible à exécuter — message affichable."""


# ── Journal / audit ───────────────────────────────────────────────────
def log_event(
    db: Any,
    boost: Any,
    kind: str,
    message: str,
    *,
    result: str = "ok",
    simulated: bool = False,
    detail: Any = None,
) -> None:
    db.add(
        BoostEvent(
            boost_id=boost.id,
            ts=C.iso(C.now_utc()),
            kind=kind[:40],
            message=message[:1000],
            result=result,
            simulated=bool(simulated),
            detail=json.dumps(detail, ensure_ascii=False)[:2000] if detail is not None else "",
        )
    )


def audit(db: Any, boost: Any, action: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor="boost",
            action=action[:50],
            object_ref=f"boost #{boost.id} {boost.coin}"[:255],
            result=result[:200],
            output=output[:500],
        )
    )


# ── Pool et portée ────────────────────────────────────────────────────
def default_coin_map(db: Any) -> dict[str, str]:
    """Token par défaut de chaque device (device → machine → rien)."""
    coins = {row.id: row.ticker for row in db.query(Coin).all()}
    profiles = {row.id: row.coin for row in db.query(MinerProfile).all()}
    out: dict[str, str] = {}
    for device in db.query(Device).all():
        if getattr(device, "default_token_id", None) in coins:
            out[device.uuid] = coins[device.default_token_id]
            continue
        machine = db.get(Machine, device.machine_id)
        profile_id = getattr(machine, "default_miner_profile_id", None) if machine else None
        if profile_id in profiles:
            out[device.uuid] = str(profiles[profile_id]).upper()
    return out


def _machine_rows(db: Any) -> dict[int, Any]:
    return {machine.id: machine for machine in db.query(Machine).all()}


def _label(machine: Any, device: Any) -> str:
    name = getattr(machine, "name", "") or f"machine {getattr(device, 'machine_id', '?')}"
    if device.kind == "gpu":
        return f"{name} — GPU {device.index if device.index is not None else '?'}"
    return f"{name} — CPU {device.model or ''}".strip()


def pool_payload(db: Any) -> dict[str, Any]:
    """Vue « Campagnes → Boost » : pool, machines, ce qui est exclu et pourquoi."""
    from app import hiveos as HV

    machines = _machine_rows(db)
    coins = default_coin_map(db)
    devices = db.query(Device).order_by(Device.machine_id, Device.kind, Device.index).all()
    rows = B.pool_rows(devices, coins)
    by_uuid = {device.uuid: device for device in devices}
    for row in rows:
        device = by_uuid[row["uuid"]]
        machine = machines.get(device.machine_id)
        row["machine"] = getattr(machine, "name", "") if machine else ""
        row["label"] = _label(machine, device) if machine else row["model"]
        row["hiveos"] = bool(machine is not None and HV.is_hiveos(machine))
        row["default_coin"] = coins.get(device.uuid, "")
    return {
        "devices": rows,
        "in_pool": sum(1 for row in rows if row["in_pool"]),
        "excluded": sum(1 for row in rows if not row["in_pool"]),
        "hiveos_machines": sorted({row["machine"] for row in rows if row["hiveos"]}),
    }


def resolve_selection(db: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Applique portée, proportion et règle « rig HiveOS en entier ».

    Lève :class:`boost.BoostError` (message affichable) ; le routeur la traduit
    en 409 quand la sélection est refusée par la règle HiveOS, 422 sinon.
    """
    from app import hiveos as HV

    scope = str(payload.get("scope") or "pool")
    proportion = payload.get("proportion", 100)
    machine_ids = payload.get("machine_ids") or []
    hours = B.validate_duration(payload.get("duration_h"))
    view = pool_payload(db)
    rows = B.select_rows(view["devices"], scope=scope, proportion=proportion, machine_ids=machine_ids)

    machines = _machine_rows(db)
    per_machine: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        per_machine.setdefault(int(row["machine_id"]), []).append(row)
    for machine_id, chosen in per_machine.items():
        machine = machines.get(machine_id)
        if machine is None:
            continue
        present_gpus = [
            d
            for d in db.query(Device).filter(Device.machine_id == machine_id, Device.kind == "gpu").all()
            if d.present and not d.integrated and d.enabled_for_mining
        ]
        indices = [row["index"] for row in chosen if row["kind"] == "gpu" and row["index"] is not None]
        refusal = B.hiveos_refusal(
            machine_name=machine.name,
            hiveos=HV.is_hiveos(machine),
            gpu_total=len(present_gpus),
            selected_gpu_indices=indices,
        )
        if refusal:
            raise BoostSelectionError(refusal)
    if not rows:
        raise B.BoostError("aucun device sélectionné : le pool de boost est vide")
    for row in rows:
        device = db.query(Device).filter(Device.uuid == row["uuid"]).first()
        if device is not None and getattr(device, "occupied_external", False):
            raise B.BoostError(f"{row['label']} : occupé par un mineur externe")
    return {"rows": rows, "hours": hours, "scope": scope, "proportion": proportion}


class BoostSelectionError(B.BoostError):
    """Sélection partielle d'un rig HiveOS — refus 409."""


def cpu_thread_pct(db: Any) -> int:
    """Part des threads CPU mobilisée par un boost (réglage, 100 % par défaut)."""
    from app.ai_config import get_setting

    try:
        pct = int(float(get_setting(db, "boost_cpu_thread_pct", 100)))
    except (TypeError, ValueError):
        pct = 100
    return max(1, min(100, pct))


def targets_from_rows(db: Any, rows: list[dict[str, Any]]) -> list[Target]:
    machines = _machine_rows(db)
    by_machine: dict[int, Target] = {}
    for row in rows:
        machine = machines.get(int(row["machine_id"]))
        if machine is None:
            continue
        target = by_machine.get(machine.id)
        if target is None:
            target = Target(machine=machine)
            by_machine[machine.id] = target
        target.device_uuids.append(row["uuid"])
        target.labels.append(row.get("label") or "")
        if row["kind"] == "gpu" and row["index"] is not None:
            target.gpus.append(int(row["index"]))
        elif row["kind"] == "cpu":
            device = db.query(Device).filter(Device.uuid == row["uuid"]).first()
            total = max(1, int(getattr(device, "threads", None) or 1))
            target.threads = max(1, round(total * cpu_thread_pct(db) / 100))
    return list(by_machine.values())


# ── Écran de validation ───────────────────────────────────────────────
def _boost_targets(db: Any, boost: Any) -> list[Target]:
    return targets_from_rows(db, boost_rows(db, boost))


def boost_rows(db: Any, boost: Any) -> list[dict[str, Any]]:
    rows = []
    for item in db.query(BoostDevice).filter(BoostDevice.boost_id == boost.id).order_by(BoostDevice.id).all():
        device = db.query(Device).filter(Device.uuid == item.device_uuid).first()
        rows.append(
            {
                "uuid": item.device_uuid,
                "machine_id": item.machine_id,
                "kind": item.kind,
                # L'index vit sur le device : sans lui, `{gpus}` est vide et le
                # gabarit refuse de démarrer (constaté par l'e2e).
                "index": getattr(device, "index", None),
                "label": item.label,
                "hiveos": bool(item.hiveos),
            }
        )
    return rows


def lost_hashrate(db: Any, rows: list[dict[str, Any]]) -> float | None:
    """Hashrate Pearl perdu : dernière mesure connue des devices retirés."""
    from app.models import Metric

    total = 0.0
    seen = False
    for row in rows:
        metric = (
            db.query(Metric)
            .filter(Metric.device_uuid == row["uuid"], Metric.hashrate_hs.isnot(None))
            .order_by(Metric.ts.desc())
            .first()
        )
        if metric is not None and metric.hashrate_hs is not None:
            total += float(metric.hashrate_hs)
            seen = True
    return round(total, 2) if seen else None


def boost_revenue(db: Any, coin: str, hashrate_hs: float | None, hours: float) -> float | None:
    """Revenu estimé du coin boosté **s'il est connu** — sinon ``None``.

    Aucune table de rentabilité par coin n'existe encore : on n'invente pas de
    chiffre, l'écran affiche « revenu inconnu » et c'est dit dans le rapport.
    """
    return None


def preview(db: Any, payload: dict[str, Any]) -> dict[str, Any]:
    selection = resolve_selection(db, payload)
    rows = selection["rows"]
    hours = selection["hours"]
    targets = targets_from_rows(db, rows)
    worst = psu_worst_pct(db, targets)
    from app import energy

    price = energy.price_at(C.now_utc(), energy.get_tariff(db))
    power = None
    from app.routers.psu import evaluate_machine_power

    total_w = 0.0
    seen = False
    for target in targets:
        try:
            value = float(evaluate_machine_power(db, target.machine).get("wall_power_w") or 0.0)
            total_w += value
            seen = True
        except Exception:
            continue
    power = round(total_w, 2) if seen else None
    lost = lost_hashrate(db, rows)
    coin = str(payload.get("coin") or "").upper()
    revenue = boost_revenue(db, coin, lost, hours)
    screen = B.screen(
        rows=[{**row} for row in rows],
        hours=hours,
        wall_power_w=power,
        eur_per_kwh=price,
        lost_hashrate_hs=lost,
        boost_price_eur=None,
        boost_network_hashrate=None,
    )
    screen["ends_at"] = C.iso(B.deadline(C.now_utc(), hours))
    screen["revenue_eur"] = revenue
    screen["revenue_known"] = revenue is not None
    warning = B.psu_warning(worst, bool(payload.get("confirm_overload")))
    if warning:
        (screen["warnings"] if warning["code"].endswith("confirmed") else screen["blocking"]).append(warning)
    screen["ok"] = not screen["blocking"]
    screen["psu_worst_pct"] = worst
    screen["coin"] = coin
    screen["selection"] = {
        "scope": selection["scope"],
        "proportion": selection["proportion"],
        "machine_ids": [target.machine.id for target in targets],
    }
    return screen


# ── Création ──────────────────────────────────────────────────────────
def create(db: Any, payload: dict[str, Any], *, origin: str = "manual", proposed: bool = False) -> Any:
    selection = resolve_selection(db, payload)
    campaign = None
    campaign_id = payload.get("campaign_id")
    if campaign_id not in (None, ""):
        campaign = db.get(Campaign, int(campaign_id))
        if campaign is None:
            raise B.BoostError(f"campagne #{campaign_id} introuvable")
        allowed = ("running", "approved") + (("proposed",) if proposed else ())
        if campaign.status not in allowed:
            raise B.BoostError(
                f"campagne {campaign.status} : un boost ne part que d'une campagne running ou approved"
            )
    coin = str(payload.get("coin") or (campaign.coin if campaign else "")).upper()
    if not coin:
        raise B.BoostError("coin du boost manquant")
    share_max = payload.get("max_share_pct", B.DEFAULT_MAX_SHARE_PCT)
    total = len([row for row in pool_payload(db)["devices"] if row["in_pool"]])
    refusal = B.share_refusal(len(selection["rows"]), total, share_max)
    if refusal:
        raise B.BoostError(refusal)

    busy = busy_devices(db)
    clash = [row["label"] for row in selection["rows"] if row["uuid"] in busy]
    if clash:
        raise B.BoostError("device(s) déjà engagé(s) dans un boost : " + ", ".join(sorted(clash)))

    now = C.now_utc()
    boost = Boost(
        uuid=str(uuid_lib.uuid4()),
        campaign_id=campaign.id if campaign else None,
        coin=coin,
        profile_id=campaign.profile_id if campaign else payload.get("profile_id"),
        status="proposed" if proposed else "approved",
        origin=origin,
        scope=selection["scope"],
        proportion=int(selection["proportion"] or 100),
        machine_ids=json.dumps([target.machine.id for target in targets_from_rows(db, selection["rows"])]),
        duration_h=selection["hours"],
        confirm_overload=bool(payload.get("confirm_overload")),
        created_at=C.iso(now),
        updated_at=C.iso(now),
    )
    db.add(boost)
    db.flush()
    machines = _machine_rows(db)
    for row in selection["rows"]:
        machine = machines.get(int(row["machine_id"]))
        from app import hiveos as HV

        db.add(
            BoostDevice(
                boost_id=boost.id,
                machine_id=int(row["machine_id"]),
                device_uuid=row["uuid"],
                kind=row["kind"],
                label=row["label"],
                hiveos=bool(machine is not None and HV.is_hiveos(machine)),
            )
        )
    db.commit()
    log_event(db, boost, "created", f"boost {coin} créé ({len(selection['rows'])} device(s), {selection['hours']:g} h)")
    db.commit()
    return boost


def busy_devices(db: Any) -> set[str]:
    """Un seul boost à la fois par device (§10.3)."""
    rows = (
        db.query(BoostDevice.device_uuid)
        .join(Boost, Boost.id == BoostDevice.boost_id)
        .filter(Boost.status.in_(("proposed", "approved", "running", "stopping")))
        .all()
    )
    return {str(row[0]) for row in rows if row[0]}


# ── Entrée / sortie ───────────────────────────────────────────────────
def entry_sequence(hiveos: bool) -> list[str]:
    """Séquence d'entrée, dans l'ordre imposé par Fred (§4.1 ter)."""
    if hiveos:
        return ["wd stop", "miner stop", "profil de campagne démarré"]
    return ["token par défaut arrêté", "profil de campagne démarré"]


def exit_sequence(hiveos: bool) -> list[str]:
    """Séquence de sortie : `miner start` puis `wd start`, jamais l'inverse."""
    if hiveos:
        return ["profil de campagne arrêté", "miner start", "wd start", "token par défaut vérifié"]
    return ["profil de campagne arrêté", "token par défaut redémarré", "token par défaut vérifié"]


def _profile(db: Any, boost: Any) -> dict[str, Any] | None:
    if not boost.profile_id:
        return None
    from app.campaign_runner import _profile_dict

    row = db.get(MinerProfile, boost.profile_id)
    return _profile_dict(row) if row is not None else None


async def start_boost(db: Any, boost: Any, *, backend: Any = None, simulated: bool = False) -> dict[str, Any]:
    from app import hiveos_remote as HR

    backend = backend or MinerBackend()
    targets = _boost_targets(db, boost)
    if not targets:
        boost.status = "failed"
        boost.note = "aucun device dans le boost"
        db.commit()
        return {"boost": boost.id, "action": "failed"}
    if not machines_online(db, targets):
        boost.status = "failed"
        boost.note = "machine injoignable — boost non démarré"
        log_event(db, boost, "refused", boost.note, result="fail")
        db.commit()
        notify(db, "boost_failed", f"{B.summary_line(boost)} : {boost.note}")
        return {"boost": boost.id, "action": "refused"}

    profile = _profile(db, boost)
    if profile is None:
        boost.status = "failed"
        boost.note = "profil du boost introuvable"
        log_event(db, boost, "refused", boost.note, result="fail")
        db.commit()
        return {"boost": boost.id, "action": "failed"}

    moment = C.now_utc()
    # 1. Entrée : HiveOS d'abord (wd stop → miner stop), puis défauts Fossor.
    for target in targets:
        from app import hiveos as HV

        if HV.is_hiveos(target.machine):
            probe_data = await hiveos_probe_data(db, target.machine, target.gpus, simulated)
            try:
                result = await HR.stop(
                    target.machine, probe_data, ack_watchdog=True, simulated=simulated
                )
            except HV.HiveosError as exc:
                log_event(db, boost, "hiveos-refused", str(exc)[:600], result="fail", simulated=simulated)
                boost.status = "failed"
                boost.note = str(exc)[:1000]
                db.commit()
                notify(db, "boost_failed", f"{B.summary_line(boost)} : {boost.note}")
                return {"boost": boost.id, "action": "failed"}
            log_event(
                db,
                boost,
                "hiveos-stopped",
                f"{target.machine.name} : wd stop puis miner stop"
                + (" (simulé)" if simulated else ""),
                simulated=simulated,
                detail={"commands": result.get("commands") or []},
            )
            db.commit()
        else:
            default_row = default_profile_for(db, target.machine)
            if default_row is not None:
                from app.campaign_runner import _profile_dict

                await backend.stop(target.machine, _profile_dict(default_row), simulated)
            log_event(db, boost, "default-stopped", f"{target.machine.name} : token par défaut arrêté", simulated=simulated)
            db.commit()

    # 2. Démarrage du profil du coin boosté sur **tout** le pool.
    commands: list[str] = []
    try:
        for target in targets:
            from app.campaign_runner import render_argv

            argv = render_argv(profile, target.machine, target)
            result = await backend.start(target.machine, profile, argv, simulated)
            commands.extend(result.get("commands") or [])
    except Exception as exc:
        log_event(db, boost, "start-failed", str(exc)[:600], result="fail", simulated=simulated)
        boost.status = "failed"
        boost.note = f"démarrage impossible : {exc}"[:1000]
        db.commit()
        notify(db, "boost_failed", f"{B.summary_line(boost)} : {boost.note}")
        return {"boost": boost.id, "action": "failed"}

    boost.status = "running"
    boost.started_at = C.iso(moment)
    boost.ends_at = C.iso(B.deadline(moment, boost.duration_h))
    boost.updated_at = C.iso(moment)
    log_event(db, boost, "running", f"boost en cours sur {len(targets)} machine(s)", simulated=simulated, detail={"argv": commands})
    db.commit()
    audit(db, boost, "boost-start", "simulated" if simulated else "ok")
    notify(db, "boost_started", f"{B.summary_line(boost)} — jusqu'à {boost.ends_at}")
    return {"boost": boost.id, "action": "started", "ends_at": boost.ends_at}


async def stop_boost(db: Any, boost: Any, *, backend: Any = None, simulated: bool = False) -> dict[str, Any]:
    from app import hiveos_remote as HR

    backend = backend or MinerBackend()
    targets = _boost_targets(db, boost)
    moment = C.now_utc()
    if boost.status != "stopping":
        B.apply_transition(boost, "stopping")
    boost.stop_requested = True
    boost.updated_at = C.iso(moment)
    db.commit()

    profile = _profile(db, boost)
    if profile is not None:
        for target in targets:
            try:
                await backend.stop(target.machine, profile, simulated)
            except Exception as exc:
                log_event(db, boost, "stop-failed", str(exc)[:400], result="fail", simulated=simulated)
        log_event(db, boost, "campaign-stopped", "mineur du coin boosté arrêté", simulated=simulated)
        db.commit()

    # Sortie : `miner start` **puis** `wd start` ; en cas d'échec au milieu, le
    # watchdog est quand même remis en route dans un `finally`.
    for target in targets:
        from app import hiveos as HV

        if not HV.is_hiveos(target.machine):
            continue
        probe_data = await hiveos_probe_data(db, target.machine, target.gpus, simulated)
        try:
            result = await HR.resume(
                target.machine, probe_data, ack_watchdog=True, simulated=simulated
            )
            log_event(
                db,
                boost,
                "hiveos-resumed",
                f"{target.machine.name} : miner start puis wd start",
                simulated=simulated,
                detail={"commands": result.get("commands") or []},
            )
        except Exception as exc:  # toujours wd start
            log_event(db, boost, "hiveos-resume-failed", str(exc)[:600], result="fail", simulated=simulated)
            try:
                from app.hiveos import Step

                await HR._execute(  # noqa: SLF001 - parade imposée
                    target.machine,
                    [Step("watchdog HiveOS → remise en route (parade)", ("sudo", "-n", "/hive/bin/wd", "start"), allow_fail=True)],
                    simulated=simulated,
                )
            except Exception:
                pass
        db.commit()

    # Retour au token par défaut sur les machines non HiveOS.
    started: list[str] = []
    for target in targets:
        from app import hiveos as HV

        if HV.is_hiveos(target.machine):
            continue
        default_row = default_profile_for(db, target.machine)
        if default_row is None:
            continue
        from app.campaign_runner import _profile_dict, render_argv

        default = _profile_dict(default_row)
        try:
            await backend.start(target.machine, default, render_argv(default, target.machine, target), simulated)
            started.append(target.machine.name)
        except Exception as exc:
            log_event(db, boost, "restore-failed", f"{target.machine.name} : {str(exc)[:400]}", result="fail", simulated=simulated)
    log_event(db, boost, "default-started", "token par défaut redémarré" if started else "aucun token par défaut à redémarrer", simulated=simulated, detail={"machines": started})

    timeout = restore_timeout(db)
    boost.restore_deadline = C.iso(moment + timedelta(seconds=int(timeout)))
    boost.attempts = 0
    boost.updated_at = C.iso(moment)
    db.commit()
    return await _verify_restore(db, boost, moment, backend, simulated)


async def _verify_restore(db: Any, boost: Any, moment: datetime, backend: Any, simulated: bool) -> dict[str, Any]:
    targets = _boost_targets(db, boost)
    default_rows = [default_profile_for(db, target.machine) for target in targets]
    hasher, source = await measure_hashrate(
        db, targets, next((row for row in default_rows if row is not None), None), backend
    )
    verified = bool(simulated) or (hasher is not None and float(hasher) > 0)
    if verified:
        for row in db.query(BoostDevice).filter(BoostDevice.boost_id == boost.id).all():
            row.verified = True
            row.restored_at = C.iso(moment)
        boost.status = "done"
        boost.ended_at = C.iso(moment)
        boost.updated_at = C.iso(moment)
        log_event(
            db,
            boost,
            "restore-verified",
            f"token par défaut revenu ({hasher if hasher is not None else 'simulé'}, source {source})",
            simulated=simulated,
            detail={"hashrate_hs": hasher, "source": source},
        )
        db.commit()
        audit(db, boost, "boost-done", "ok")
        notify(db, "boost_done", f"{B.summary_line(boost)} — retour vérifié ({source})")
        return {"boost": boost.id, "action": "done", "verified": True, "source": source}

    deadline = C.parse_iso(boost.restore_deadline)
    attempt = int(boost.attempts or 0)
    if deadline is not None and moment < deadline and attempt == 0:
        log_event(db, boost, "restore-pending", "hashrate du token par défaut pas encore revenu", detail={"source": source})
        db.commit()
        return {"boost": boost.id, "action": "restore-pending", "verified": False}
    if attempt < MAX_RESTORE_ATTEMPTS:
        boost.attempts = attempt + 1
        db.commit()
        log_event(db, boost, "restore-retry", f"nouvel essai {attempt + 1}/{MAX_RESTORE_ATTEMPTS}", result="fail")
        # Nouvel essai : arrêt puis redémarrage du token par défaut.
        for target, default_row in zip(targets, default_rows):
            if default_row is None:
                continue
            from app.campaign_runner import _profile_dict, render_argv

            default = _profile_dict(default_row)
            try:
                await backend.stop(target.machine, default, simulated)
                await backend.start(target.machine, default, render_argv(default, target.machine, target), simulated)
            except Exception as exc:
                log_event(db, boost, "restore-failed", str(exc)[:400], result="fail", simulated=simulated)
        boost.restore_deadline = C.iso(C.now_utc() + timedelta(seconds=int(restore_timeout(db))))
        db.commit()
        return {"boost": boost.id, "action": "restore-retry", "verified": False}

    boost.status = "failed"
    boost.note = "le token par défaut n'est pas revenu après deux essais"
    boost.ended_at = C.iso(moment)
    db.commit()
    log_event(db, boost, "restore-failed-final", boost.note, result="fail")
    db.commit()
    audit(db, boost, "boost-restore", "fail", boost.note)
    notify(db, "boost_restore_failed", f"{B.summary_line(boost)} : {boost.note}")
    return {"boost": boost.id, "action": "failed", "verified": False}


# ── Boucle ────────────────────────────────────────────────────────────
def due_boosts(db: Any, moment: datetime) -> list[Any]:
    rows = (
        db.query(Boost)
        .filter(Boost.status.in_(("approved", "running", "stopping")))
        .order_by(Boost.id.asc())
        .all()
    )
    due = []
    for boost in rows:
        if boost.status == "approved":
            due.append(boost)
        elif boost.status == "running":
            if B.is_due(boost, moment):
                due.append(boost)
        elif boost.status == "stopping":
            due.append(boost)
    return due


def simulation_for(db: Any, boost: Any) -> bool:
    from app import pilotage

    targets = _boost_targets(db, boost)
    return any(pilotage.simulated_for(db, target.machine) for target in targets)


async def tick(
    db: Any,
    *,
    now: datetime | None = None,
    backend: Any = None,
    simulated: bool | None = None,
) -> list[dict[str, Any]]:
    global _LOCK

    moment = now or C.now_utc()
    backend = backend or MinerBackend()
    results: list[dict[str, Any]] = []
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    if _LOCK.locked():
        return results
    async with _LOCK:
        for boost in due_boosts(db, moment):
            sim = simulation_for(db, boost) if simulated is None else simulated
            if boost.status == "approved":
                results.append(await start_boost(db, boost, backend=backend, simulated=sim))
            elif boost.status == "running":
                results.append(await stop_boost(db, boost, backend=backend, simulated=sim))
            elif boost.status == "stopping":
                results.append(await _verify_restore(db, boost, moment, backend, sim))
            db.expire_all()
    return results


def switching(db: Any, machine: Any) -> str:
    """Bascule en cours : le watchdog ne touche pas aux devices de cette machine."""
    row = (
        db.query(Boost)
        .join(BoostDevice, BoostDevice.boost_id == Boost.id)
        .filter(BoostDevice.machine_id == machine.id, Boost.status.in_(BOOST_SWITCHING))
        .order_by(Boost.id.desc())
        .first()
    )
    if row is None:
        return ""
    phase = "entrée" if row.status == "approved" else "sortie"
    return f"boost #{row.id} en {phase} : watchdog muet sur cette machine"


def active_boost(db: Any, device_uuid: str) -> Any:
    """Boost en cours qui couvre ce device, s'il existe."""
    if not device_uuid:
        return None
    return (
        db.query(Boost)
        .join(BoostDevice, BoostDevice.boost_id == Boost.id)
        .filter(BoostDevice.device_uuid == device_uuid, Boost.status == "running")
        .order_by(Boost.id.desc())
        .first()
    )


def reference_hashrate(db: Any, device_uuid: str, now: datetime, *, minimum: int = 6) -> float | None:
    """Référence du watchdog pendant un boost : le **coin boosté**, pas Pearl."""
    boost = active_boost(db, device_uuid)
    start = C.parse_iso(boost.started_at) if boost is not None else None
    if start is None:
        return None
    from app.models import Metric

    rows = (
        db.query(Metric.hashrate_hs)
        .filter(Metric.device_uuid == device_uuid, Metric.ts >= C.iso(start), Metric.hashrate_hs.isnot(None))
        .order_by(Metric.ts.asc())
        .limit(500)
        .all()
    )
    values = sorted(float(row[0]) for row in rows if row[0])
    if len(values) < minimum:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0


def start() -> None:
    """Boucle du planificateur (même processus, une seule boucle)."""
    import threading

    if getattr(start, "_thread", None) is not None:  # type: ignore[attr-defined]
        return

    def runner() -> None:
        from app.database import SessionLocal

        while True:
            try:
                db = SessionLocal()
                try:
                    asyncio.run(tick(db))
                finally:
                    db.close()
            except Exception:  # pragma: no cover - boucle de fond
                import logging

                logging.getLogger("fossor.boost").warning("tick boost", exc_info=True)
            threading.Event().wait(10)

    thread = threading.Thread(target=runner, name="fossor-boost", daemon=True)
    thread.start()
    start._thread = thread  # type: ignore[attr-defined]
