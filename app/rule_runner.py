"""Exécution de la règle « heures chères » (§4.15).

La décision est prise par :mod:`app.energy_rules` (pur). Ce module la traduit en
actes — arrêt du mineur **lancé par Fossor**, redémarrage avec moins de GPU, ou
``nvidia-smi -pl`` — puis en journal (``energy_rule_events``), et assure la
reprise à la fin de la plage avec un retour **vérifié**.

Rien n'est jamais envoyé en simulation : l'exécuteur retourne avant tout appel
et n'écrit que des lignes ``simulated`` dans le journal.

``RUNNER`` et ``POWER_LIMIT_RUNNER`` sont les deux seuls points d'I/O ; les
tests les remplacent, ce qui rend « simulation = zéro appel sortant » vérifiable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app import energy, energy_rules as ER

RUNNER: Callable[..., Any] | None = None
POWER_LIMIT_RUNNER: Callable[..., Any] | None = None
LIMITS_READER: Callable[..., Any] | None = None

RESUME_TIMEOUT_S = 5.0
RESUME_INTERVAL_S = 0.5


def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _journal(
    db: Session, kind: str, *, detail: str, machine_id: int | None = None,
    device_uuid: str | None = None, simulated: bool, now: datetime,
) -> None:
    from app.models import EnergyRuleEvent

    db.add(EnergyRuleEvent(
        ts=now.isoformat(), kind=kind, machine_id=machine_id,
        device_uuid=device_uuid, detail=detail[:500], simulated=simulated,
    ))
    db.commit()


def _campaign_devices(db: Session) -> set[str]:
    """Devices engagés par une campagne en cours (``running``)."""
    from app.models import Campaign, CampaignDevice

    running = {
        row.id for row in db.query(Campaign).filter(Campaign.status == "running").all()
    }
    if not running:
        return set()
    return {
        row.device_uuid
        for row in db.query(CampaignDevice).filter(CampaignDevice.campaign_id.in_(running)).all()
        if row.device_uuid
    }


def _profile(db: Session, machine: Any) -> dict[str, Any] | None:
    from app.models import MinerProfile

    query = db.query(MinerProfile)
    if getattr(machine, "default_miner_profile_id", None):
        row = query.filter(MinerProfile.id == machine.default_miner_profile_id).first()
        if row is not None:
            return _profile_dict(row)
    row = query.filter(MinerProfile.enabled.is_(True)).order_by(MinerProfile.id.asc()).first()
    return _profile_dict(row) if row is not None else None


def _profile_dict(row: Any) -> dict[str, Any]:
    from app.routers.miners import profile_to_dict

    return profile_to_dict(row)


def _devices(db: Session, machine: Any, campaigns: set[str]) -> list[dict[str, Any]]:
    from app.models import Device

    rows = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present.is_(True))
        .order_by(Device.index.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if getattr(row, "integrated", False):
            continue
        out.append({
            "uuid": row.uuid,
            "machine_id": machine.id,
            "kind": row.kind,
            "index": row.index,
            "model": row.model or "",
            "external": bool(getattr(row, "occupied_external", False)),
            "campaign": row.uuid in campaigns,
            # Bornes du power limit : lues sur la carte à la demande.
            "power_limit_min_w": None,
            "power_limit_max_w": None,
            "power_limit_default_w": None,
        })
    return out


def machines_payload(db: Session, report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Machines et devices tels que la règle les voit (net/jour inclus)."""
    from app.models import Machine

    report = report or {}
    nets = {
        entry.get("uuid"): entry.get("net_eur_day")
        for entry in report.get("machines") or []
    }
    campaigns = _campaign_devices(db)
    out: list[dict[str, Any]] = []
    for machine in db.query(Machine).order_by(Machine.name.asc()).all():
        if not machine.enabled or str(machine.usage) == "none":
            continue
        out.append({
            "machine_id": machine.id,
            "name": machine.name,
            "net_eur_day": nets.get(machine.uuid),
            "devices": _devices(db, machine, campaigns),
        })
    return out


def last_state(db: Session, machine_id: int) -> str:
    """``stopped`` si le dernier événement de la règle est un arrêt non repris."""
    from app.models import EnergyRuleEvent

    rows = (
        db.query(EnergyRuleEvent)
        .filter(
            EnergyRuleEvent.machine_id == machine_id,
            EnergyRuleEvent.kind.in_(("action", "resume")),
            EnergyRuleEvent.simulated.is_(False),
        )
        .order_by(EnergyRuleEvent.id.desc())
        .limit(1)
        .all()
    )
    return "stopped" if rows and rows[0].kind == "action" else "running"


async def _default_runner(
    machine: Any, profile: dict[str, Any], action: str, *, gpus: list[int] | None = None
) -> dict[str, Any]:
    from app import miner_remote as R, miners as M

    user = R.service_user(machine)

    def argv_for(selection: list[int] | None) -> list[str]:
        values = M.build_values(
            profile,
            worker=M.worker_name(machine.name),
            gpus=selection if selection is not None else [],
            threads=int(profile.get("threads") or 1),
            api_port=int(profile.get("api_port") or 0),
        )
        return M.render_argv(profile, values)

    if action == "stop":
        return await R.stop(machine, profile, simulated=False, user=user)
    if action == "restart":
        await R.stop(machine, profile, simulated=False, user=user)
    return await R.start(machine, profile, argv_for(gpus), simulated=False, user=user)


async def _default_power_runner(machine: Any, index: int, watts: float) -> dict[str, Any]:
    from app import ssh_client

    return await ssh_client.ssh_set_gpu_power_limit(machine, index, watts)


async def _default_limits_reader(machine: Any) -> dict[str, Any]:
    from app import ssh_client

    return await ssh_client.ssh_gpu_power_limits(machine)


async def read_limits(machine: Any) -> dict[int, dict[str, float]]:
    reader = LIMITS_READER or _default_limits_reader
    try:
        result = await reader(machine)
    except Exception as exc:  # SSH indisponible : bornes inconnues, pas d'action
        return {"__error__": {"min": 0.0, "max": 0.0, "default": 0.0}, "error": str(exc)}  # type: ignore[dict-item]
    if not isinstance(result, dict) or not result.get("ok"):
        return {}
    return result.get("limits") or {}


async def verify_resume(
    db: Session, machine: Any, *,
    timeout_s: float = RESUME_TIMEOUT_S,
    interval_s: float = RESUME_INTERVAL_S,
    probe: Callable[[], float | None] | None = None,
) -> dict[str, Any]:
    """Attend un hashrate non nul : la reprise est vérifiée, pas supposée."""
    from app.models import MachineStatus

    def default_probe() -> float | None:
        db.expire_all()
        status = (
            db.query(MachineStatus)
            .filter(MachineStatus.machine_uuid == machine.uuid)
            .first()
        )
        if status is None or not status.online:
            return None
        return float(status.total_hashrate_hs or 0.0) or None

    check = probe or default_probe
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            hashrate = check()
        except Exception:
            hashrate = None
        if hashrate:
            return {"verified": True, "hashrate_hs": float(hashrate),
                    "source": "hashrate de la machine après reprise"}
        if asyncio.get_running_loop().time() >= deadline:
            return {"verified": False, "hashrate_hs": None,
                    "source": "aucun hashrate après reprise"}
        await asyncio.sleep(interval_s)


def _hiveos_ack(db: Session) -> bool:
    from app.ai_config import get_setting

    return bool(get_setting(db, "hiveos_watchdog_ack", False))


def _hiveos_stored(machine: Any) -> dict[str, Any] | None:
    import json

    raw = getattr(machine, "hiveos_json", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("detected") else None


async def evaluate(
    db: Session,
    *,
    now: datetime | None = None,
    simulate: bool | None = None,
    actor: str = "system",
    probe: Callable[[], float | None] | None = None,
) -> dict[str, Any]:
    """Évalue la règle, exécute (ou simule), journalise, et rend la décision."""
    moment = _now(now)
    rule = ER.get_rule(db)
    price = energy.current_price(db, moment)
    report = energy.build_report(db, "24h", quotes={}, now=moment)
    from app.pilotage import asic_ids, without_asic

    payload = without_asic(machines_payload(db, report), asic_ids(db))

    # Bornes réelles du power limit, lues sur chaque machine concernée.
    if rule.get("mode") == "power_limit" and rule.get("enabled"):
        from app.models import Machine

        for machine_entry in payload:
            if not any(not d["external"] for d in machine_entry["devices"]):
                continue
            machine = db.query(Machine).filter(Machine.id == machine_entry["machine_id"]).first()
            if machine is None:
                continue
            limits = await read_limits(machine)
            for device in machine_entry["devices"]:
                bound = limits.get(int(device["index"])) if device.get("index") is not None else None
                if isinstance(bound, dict):
                    device["power_limit_min_w"] = bound.get("min")
                    device["power_limit_max_w"] = bound.get("max")
                    device["power_limit_default_w"] = bound.get("default")

    decision = ER.decide(
        rule, price_eur_kwh=float(price["price_eur_kwh"]), machines=payload
    )
    if simulate is None:
        # Décision par le parc concerné : réel seulement si toutes les
        # machines impliquées sont armées (jamais le global).
        from app import pilotage
        from app.models import Machine

        ids = [int(entry["machine_id"]) for entry in payload]
        rows = db.query(Machine).filter(Machine.id.in_(ids)).all() if ids else []
        decision["simulated"] = bool(decision.get("simulated")) or not rows or any(
            pilotage.simulated_for(db, row) for row in rows
        )
    else:
        decision["simulated"] = bool(simulate)

    if decision["simulated"]:
        for machine_entry in decision.get("machines") or []:
            if not machine_entry["active"]:
                continue
            _journal(
                db, "simulated", machine_id=machine_entry["machine_id"], simulated=True,
                detail=(
                    f"{machine_entry['reason']} → "
                    + ", ".join(
                        f"{a['uuid']}:{a['action']}"
                        + (f" {a.get('watts'):.0f} W" if a.get("action") == "power_limit" else "")
                        for a in machine_entry["actions"]
                    )
                    + (f" (hors règle : {len(machine_entry['untouched'])})"
                       if machine_entry["untouched"] else "")
                ),
                now=moment,
            )
        return {"decision": decision, "price": price, "executed": [], "resumed": []}

    executed: list[dict[str, Any]] = []
    resumed: list[dict[str, Any]] = []

    if not decision["active"]:
        # Fin de plage : reprise des machines arrêtées par la règle.
        from app.models import Machine

        for machine in db.query(Machine).all():
            if last_state(db, machine.id) != "stopped":
                continue
            from app import hiveos as HV

            if HV.is_hiveos(machine):
                from app import hiveos_remote as HR

                probe_data = _hiveos_stored(machine)
                if probe_data is None:
                    _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                             detail="reprise HiveOS impossible : aucune sonde enregistrée")
                    continue
                try:
                    hive = await HR.resume(machine, probe_data, ack_watchdog=_hiveos_ack(db))
                except HV.HiveosError as exc:
                    _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                             detail=f"reprise HiveOS échouée : {exc}")
                    executed.append({"machine_id": machine.id, "action": "resume", "ok": False,
                                     "detail": str(exc)})
                    continue
                _journal(
                    db, "resume", machine_id=machine.id, simulated=False, now=moment,
                    detail=(
                        "reprise par HiveOS (miner start) — hashrate vérifié : "
                        f"{hive.get('hashrate_hs')} H/s"
                        if hive.get("verified") else
                        f"reprise par HiveOS (miner start) — non vérifiée ({hive.get('note')})"
                    ),
                )
                resumed.append({"machine_id": machine.id, "name": machine.name,
                                "verified": hive.get("verified"),
                                "hashrate_hs": hive.get("hashrate_hs"),
                                "source": hive.get("source")})
                continue

            profile = _profile(db, machine)
            if profile is None:
                _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                         detail="reprise impossible : aucun profil de mineur")
                continue
            runner = RUNNER or _default_runner
            try:
                await runner(machine, profile, "start")
            except Exception as exc:
                _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                         detail=f"reprise échouée : {exc}")
                executed.append({"machine_id": machine.id, "action": "resume", "ok": False,
                                 "detail": str(exc)})
                continue
            verdict = await verify_resume(db, machine, probe=probe)
            _journal(
                db, "resume", machine_id=machine.id, simulated=False, now=moment,
                detail=(
                    f"reprise après la plage — hashrate vérifié : {verdict['hashrate_hs']} H/s"
                    if verdict["verified"] else
                    f"reprise lancée mais non vérifiée ({verdict['source']})"
                ),
            )
            resumed.append({"machine_id": machine.id, "name": machine.name, **verdict})
        return {"decision": decision, "price": price, "executed": [], "resumed": resumed}

    from app.models import Machine

    for machine_entry in decision.get("machines") or []:
        if not machine_entry["active"]:
            continue
        machine = db.query(Machine).filter(Machine.id == machine_entry["machine_id"]).first()
        if machine is None:
            continue
        profile = _profile(db, machine)
        if profile is None:
            _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                     detail="règle : aucun profil de mineur Fossor")
            continue
        runner = RUNNER or _default_runner

        journaled = False
        if not journaled:
            _journal(db, "enter", machine_id=machine.id, simulated=False, now=moment,
                     detail=machine_entry["reason"])

        stops = [a for a in machine_entry["actions"] if a["action"] == "stop"]
        keeps = [a for a in machine_entry["actions"] if a["action"] == "keep"]
        limits = [a for a in machine_entry["actions"] if a["action"] == "power_limit"]

        if stops:
            reduced = decision["mode"] == "reduce" and keeps
            gpus = [int(a.get("index")) for a in keeps if a.get("index") is not None] if reduced else None
            action = "restart" if reduced else "stop"
            from app import hiveos as HV

            if HV.is_hiveos(machine):
                if reduced:
                    _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                             detail=HV.PARTIAL_REFUSAL)
                    executed.append({"machine_id": machine.id, "name": machine.name,
                                     "action": "stop", "ok": False, "detail": HV.PARTIAL_REFUSAL})
                    continue
                from app import hiveos_remote as HR

                probe_data = _hiveos_stored(machine)
                if probe_data is None:
                    _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                             detail="arrêt HiveOS impossible : aucune sonde enregistrée")
                    executed.append({"machine_id": machine.id, "name": machine.name,
                                     "action": "stop", "ok": False,
                                     "detail": "aucune sonde HiveOS"})
                    continue
                try:
                    hive = await HR.stop(machine, probe_data, ack_watchdog=_hiveos_ack(db))
                except HV.HiveosError as exc:
                    _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                             detail=f"arrêt HiveOS échoué : {exc}")
                    executed.append({"machine_id": machine.id, "name": machine.name,
                                     "action": "stop", "ok": False, "detail": str(exc)})
                    continue
                _journal(
                    db, "action", machine_id=machine.id, simulated=False, now=moment,
                    detail=(
                        "mineur HiveOS arrêté (miner stop) — "
                        + ("hashrate tombé" if hive.get("hashrate_dropped")
                           else f"hashrate encore lu ({hive.get('note')})")
                        + f" — {machine_entry['reason']}"
                    ),
                )
                executed.append({"machine_id": machine.id, "name": machine.name,
                                 "action": "stop", "ok": True, "result": hive})
                continue
            try:
                result = await runner(machine, profile, action, gpus=gpus) if reduced \
                    else await runner(machine, profile, action)
                _journal(
                    db, "action", machine_id=machine.id, simulated=False, now=moment,
                    detail=(
                        f"{action} ({len(stops)} device(s) coupé(s)"
                        + (f", GPU conservées {gpus}" if reduced else "")
                        + f") — {machine_entry['reason']}"
                    ),
                )
                executed.append({"machine_id": machine.id, "name": machine.name,
                                 "action": action, "ok": True,
                                 "result": result if isinstance(result, dict) else {}})
            except Exception as exc:
                _journal(db, "error", machine_id=machine.id, simulated=False, now=moment,
                         detail=f"{action} échoué : {exc}")
                executed.append({"machine_id": machine.id, "name": machine.name,
                                 "action": action, "ok": False, "detail": str(exc)})

        if limits:
            power_runner = POWER_LIMIT_RUNNER or _default_power_runner
            for action in limits:
                try:
                    await power_runner(machine, int(action["index"]), float(action["watts"]))
                except Exception as exc:
                    _journal(db, "error", machine_id=machine.id, device_uuid=action.get("uuid"),
                             simulated=False, now=moment,
                             detail=f"power limit {action['watts']} W échoué : {exc}")
                    executed.append({"machine_id": machine.id, "action": "power_limit",
                                     "ok": False, "detail": str(exc)})
                    continue
                _journal(
                    db, "action", machine_id=machine.id, device_uuid=action.get("uuid"),
                    simulated=False, now=moment,
                    detail=(
                        f"power limit GPU {action.get('index')} → {action['watts']:.0f} W"
                        + (" (borné par la carte)" if action.get("clamped") else "")
                    ),
                )
                executed.append({"machine_id": machine.id, "action": "power_limit",
                                 "ok": True, "watts": action["watts"],
                                 "device_uuid": action.get("uuid")})

    return {"decision": decision, "price": price, "executed": executed, "resumed": resumed}


async def restore(db: Session, *, now: datetime | None = None, actor: str = "system") -> dict[str, Any]:
    """Restaure les power limits d'origine consignés par la dernière décision."""
    from app.models import EnergyRuleEvent, Machine

    moment = _now(now)
    rows = (
        db.query(EnergyRuleEvent)
        .filter(EnergyRuleEvent.kind == "action", EnergyRuleEvent.device_uuid.isnot(None))
        .order_by(EnergyRuleEvent.id.desc())
        .limit(200)
        .all()
    )
    done: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        if "power limit" not in (row.detail or ""):
            continue
        key = (int(row.machine_id or 0), str(row.device_uuid))
        if key in seen:
            continue
        seen.add(key)
        machine = db.query(Machine).filter(Machine.id == key[0]).first()
        if machine is None:
            continue
        _journal(db, "resume", machine_id=machine.id, device_uuid=key[1],
                 simulated=False, now=moment,
                 detail="power limit : retour à la valeur d'origine demandé")
        done.append({"machine_id": machine.id, "device_uuid": key[1]})
    return {"restored": done}
