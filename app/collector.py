"""Metrics collector — SSH snapshot every N seconds, stored for the dashboard.

Design
------
* One whitelisted command per machine (``collect_stats``), never sudo.
* Machines are collected in parallel (``asyncio``, 5 at a time) with a
  per-machine timeout; a failure marks the machine offline with its error and
  never aborts the round.
* Machines with ``usage == "none"`` (or disabled) are never contacted.
* The SRBMiner HTTP API is queried from Fossor only when ``ps`` revealed an
  ``--api-port``; a closed port is a silent no-op.
* ``metrics`` keeps one row per device per round; ``machine_status`` keeps the
  last known machine-level state.  Rows older than the retention window are
  purged once a day.
"""

from __future__ import annotations

import asyncio
import json as json_lib
import logging
import os
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any

from app.hw_parser import norm_bus
from app.stats_parser import (
    merge_srbminer,
    parse_collect_stats,
    parse_srbminer_api,
)

logger = logging.getLogger("fossor.collector")

MAX_PARALLEL = 5
MACHINE_TIMEOUT = 45  # host-key read + connect + command, with margin
SRBMINER_TIMEOUT = 5.0

_task: asyncio.Task | None = None
_last_purge_date: str | None = None


# ── Settings (read per call so tests can set them) ────────────────────
def collect_interval() -> int:
    try:
        return int(os.environ.get("FOSSOR_COLLECT_INTERVAL", "60"))
    except ValueError:
        return 60


def retention_days() -> int:
    try:
        return int(os.environ.get("FOSSOR_METRICS_RETENTION_DAYS", "7"))
    except ValueError:
        return 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SRBMiner HTTP API ─────────────────────────────────────────────────
async def fetch_srbminer(host: str, port: int) -> dict[str, Any] | None:
    """GET ``http://host:port/`` for 5 s; any failure returns ``None`` silently."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=SRBMINER_TIMEOUT) as client:
            response = await client.get(f"http://{host}:{port}/")
        if response.status_code != 200:
            return None
        return parse_srbminer_api(response.text)
    except Exception:
        return None


# ── Settings read per call (patterns, coin pool hosts) ────────────────
def _patterns(db) -> list[dict[str, str]]:
    from app.external_miners import get_patterns

    return get_patterns(db)


def _coin_hosts(db) -> dict[str, str]:
    """Pool host substring → ticker, from the coin catalogue."""
    from app.models import Coin

    hosts: dict[str, str] = {}
    for coin in db.query(Coin).all():
        for host in (coin.pool_hosts or []):
            key = str(host or "").strip().lower()
            if key:
                hosts[key] = coin.ticker
    return hosts


def _coin_catalogue(db) -> dict[str, Any]:
    """Everything coin inference needs: pool hosts, tickers, names, aliases."""
    from app.ai_config import get_setting
    from app.coins import builtin_hosts, builtin_names, builtin_tickers
    from app.models import Coin
    from app.external_miners import COIN_ALIASES

    hosts: dict[str, str] = builtin_hosts()
    names: dict[str, str] = builtin_names()
    tickers: list[str] = builtin_tickers()
    for coin in db.query(Coin).all():
        ticker = str(coin.ticker or "").strip().upper()
        if not ticker:
            continue
        tickers.append(ticker)
        names[ticker.lower()] = ticker
        if coin.name:
            names[str(coin.name).strip().lower()] = ticker
        for host in (coin.pool_hosts or []):
            key = str(host or "").strip().lower()
            if key:
                hosts[key] = ticker
    stored = get_setting(db, "coin_aliases", None)
    aliases = dict(COIN_ALIASES)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if str(key).strip() and str(value).strip():
                aliases[str(key).strip().upper()] = str(value).strip().upper()
    return {"hosts": hosts, "tickers": tickers, "names": names, "aliases": aliases}


def _store_raw_collection(db, machine, stdout: str) -> None:
    """Keep the last raw collection, masked (admin diagnostic screen)."""
    from app.external_miners import mask_raw_collect
    from app.models import RawCollection

    if not machine.uuid:
        machine.uuid = str(uuid_lib.uuid4())
    row = (
        db.query(RawCollection)
        .filter(RawCollection.machine_uuid == machine.uuid)
        .first()
    )
    if row is None:
        row = RawCollection(machine_uuid=machine.uuid)
        db.add(row)
    row.ts = _now()
    row.stdout = mask_raw_collect(stdout or "")


def _api_hashrate(api: dict[str, Any] | None) -> bool:
    """An API answer is only usable when it actually carries a hashrate."""
    return isinstance(api, dict) and api.get("total_hashrate_hs") is not None


async def _fetch_miner_api(machine, port: int) -> tuple[dict[str, Any] | None, str | None]:
    """Direct GET, then the SSH relay (SRBMiner bound to 127.0.0.1).

    A 200 answer without a hashrate (web page, foreign JSON) is **not** a
    success: the relay is still tried, and a miner that keeps a port but no
    hashrate ends up with an ``api_error`` instead of a silent « — »
    (2026.11.003-c4).
    """
    direct = await fetch_srbminer(machine.host, port)
    if _api_hashrate(direct):
        return direct, None
    direct_note = "réponse sans hashrate" if direct else "port fermé"

    try:
        from app.ssh_client import ssh_miner_api

        raw = await ssh_miner_api(machine, port)
    except Exception as exc:  # pragma: no cover - defensive
        raw = {"ok": False, "error": str(exc)}

    relay: dict[str, Any] | None = None
    if raw and raw.get("ok"):
        relay = parse_srbminer_api(raw.get("stdout") or "")
    if _api_hashrate(relay):
        return relay, None

    if (raw or {}).get("ok"):
        relay_note = "réponse sans hashrate" if relay else "réponse illisible"
        message = (
            f"API du mineur sans hashrate (direct : {direct_note} ; SSH : {relay_note})"
        )
        return (relay if relay else None), message
    relay_note = str((raw or {}).get("error") or "relais SSH indisponible")
    message = f"API du mineur injoignable (direct : {direct_note} ; SSH : {relay_note})"
    return None, message


# ── Storage ───────────────────────────────────────────────────────────
def _numeric(value: float | None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


async def _ensure_devices(db, machine) -> bool:
    """Detect a machine once when it has no device row yet (§4.2).

    A machine that was never « detected » stores its GPU metrics with
    ``device_uuid = NULL`` and nothing shows per card.  The first successful
    collection therefore runs one whitelisted ``detect_hw`` (audited, actor
    ``scheduler``); the metrics of the same round then attach by PCI bus.
    """
    from app.devices import apply_detection
    from app.models import AuditLog, Device
    from app.ssh_client import ssh_detect_hw

    present = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present == True)  # noqa: E712
        .count()
    )
    if present:
        return False

    try:
        raw = await ssh_detect_hw(machine, actor="system")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("détection automatique de %s en échec: %s", machine.name, exc)
        return False

    if not raw.get("ok"):
        logger.info("détection automatique de %s impossible: %s", machine.name, raw.get("error"))
        db.add(AuditLog(
            actor="system",
            action="detect",
            machine_id=machine.id,
            object_ref=machine.name,
            result="fail",
            output=str(raw.get("error") or "")[:500],
        ))
        db.commit()
        return False

    # ssh_detect_hw already returns the parsed detection; older shapes carry stdout.
    detected = raw
    if "cpu" not in raw and "gpus" not in raw and raw.get("stdout"):
        from app.hw_parser import parse_detect_hw

        detected = parse_detect_hw(raw.get("stdout", ""))

    external_miners = raw.get("gpu_compute", [])
    summary = apply_detection(db, machine, detected, gpu_compute=external_miners)
    db.add(AuditLog(
        actor="system",
        action="detect",
        machine_id=machine.id,
        object_ref=machine.name,
        result="ok",
        output=("détection automatique (1re collecte) " + str(summary))[:500],
    ))
    db.commit()
    return True


async def _enrich_cpu_state(db, machine, raw_stdout: str, stats: dict[str, Any]) -> str:
    """Hashrate CPU et état du CPU (2026.11.003-c7).

    One extra read-only probe per round: the miner APIs and logs found in the
    command lines, the CPU counters (``/proc/stat`` twice), the hwmon sensors
    and two RAPL readings.  Every CPU miner ends up with a hashrate **or** an
    explicit reason; the ``cpu`` device gets charge, temperature, power and the
    summed CPU hashrate.
    """
    from app.cpu_stats import (
        attach_cpu_hashrates,
        cpu_state_with,
        parse_cpu_report,
        plan_cpu_probes,
    )
    from app.ssh_client import ssh_collect_cpu
    from app.stats_parser import split_sections

    sections = split_sections(raw_stdout)
    probes = plan_cpu_probes(sections["PS_MINERS"])
    report: dict[str, Any] = {"state": {}, "logs": {}, "apis": {}, "nodes": {}}
    probe_stdout = ""
    # 2026.11.003-c8 : la sonde tourne **toujours**, même sans mineur détecté.
    # La fréquence, la charge et la puissance du CPU ne dépendent d'aucun mineur,
    # et c'est leur absence qui laissait `cpu MHz` vide sur les trois machines.
    try:
        raw = await ssh_collect_cpu(machine, probes)
    except Exception as exc:  # pragma: no cover - defensive
        logger.info("sonde CPU de %s en échec: %s", machine.name, exc)
        raw = {"ok": False, "error": str(exc)}
    if raw.get("ok"):
        probe_stdout = raw.get("stdout") or ""
        report = parse_cpu_report(probe_stdout)

    miners = stats.get("miners") or []
    attach_cpu_hashrates(miners, probes, report)
    cpu_miners = [m for m in miners if str(m.get("kind") or "") in ("cpu", "solo")]

    cputemp = _hive_cputemp(sections["HIVE_STAT"])
    cpu = cpu_state_with(report, cputemp, stats.get("cpu_package_power_w"), cpu_miners)
    stats["cpu"] = cpu

    for item in stats.get("devices") or []:
        if item.get("kind") != "cpu":
            continue
        item["load_pct"] = cpu.get("load_pct")
        item["temp_c"] = _first_number(item.get("temp_c"), cpu.get("temp_c"))
        item["power_w"] = _first_number(item.get("power_w"), cpu.get("power_w"))
        item["hashrate_hs"] = cpu.get("hashrate_hs")
    return probe_stdout


def _first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _hive_cputemp(hive_stat: str) -> float | None:
    """``cputemp`` of ``last_stat.json`` (HiveOS) — first value only."""
    import json

    try:
        data = json.loads(hive_stat or "{}")
    except (TypeError, ValueError):
        return None
    temps = data.get("temp") if isinstance(data, dict) else None
    cputemp = data.get("cputemp") if isinstance(data, dict) else None
    for value in (cputemp if isinstance(cputemp, list) else [], temps if isinstance(temps, list) else []):
        for entry in value:
            if isinstance(entry, (int, float)) and entry > 0:
                return float(entry)
    return None


def _store_metrics(db, machine, stats: dict[str, Any], ts: str) -> int:
    """Insert one metric row per parsed device, then upsert ``machine_status``."""
    from app.models import Device, MachineStatus, Metric

    if not machine.uuid:
        machine.uuid = str(uuid_lib.uuid4())

    devices = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present == True)  # noqa: E712
        .all()
    )
    cpu = next((d for d in devices if d.kind == "cpu"), None)
    gpus_by_bus = {
        norm_bus(d.pci_bus): d for d in devices if d.kind == "gpu" and d.pci_bus
    }

    rows = 0
    for item in stats.get("devices", []):
        if item.get("kind") == "cpu":
            device = cpu
        else:
            device = gpus_by_bus.get(item.get("pci_bus") or "")
        db.add(Metric(
            machine_uuid=machine.uuid,
            device_uuid=device.uuid if device else None,
            ts=ts,
            hashrate_hs=_numeric(item.get("hashrate_hs")),
            temp_c=_numeric(item.get("temp_c")),
            fan_pct=_numeric(item.get("fan_pct")),
            power_w=_numeric(item.get("power_w")),
            load_pct=_numeric(item.get("load_pct")),
        ))
        rows += 1
    if rows == 0:
        # Machine-level row only (no device matched or none reported).
        db.add(Metric(
            machine_uuid=machine.uuid,
            device_uuid=None,
            ts=ts,
            hashrate_hs=_numeric(stats.get("total_hashrate_hs")),
            power_w=_numeric(stats.get("total_power_w")),
        ))
        rows = 1

    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    )
    if status is None:
        status = MachineStatus(machine_uuid=machine.uuid)
        db.add(status)
    status.ts = ts
    status.online = True
    status.error = None
    status.miner = stats.get("miner") or ""
    status.algo = stats.get("algo") or ""
    status.coin = stats.get("coin") or ""
    status.uptime_s = stats.get("uptime_s")
    status.accepted = stats.get("accepted")
    status.rejected = stats.get("rejected")
    status.api_port = stats.get("api_port")
    status.total_hashrate_hs = _numeric(stats.get("total_hashrate_hs"))
    status.total_power_w = _numeric(stats.get("total_power_w"))
    status.api_error = stats.get("api_error")
    # 2026.11.003-c7: état du CPU + hashrate par mineur CPU.
    status.cpu_json = (
        json_lib.dumps(stats.get("cpu"), ensure_ascii=False) if stats.get("cpu") else None
    )
    try:
        from app import notifications

        notifications.machine_result(
            db, machine, online=True, hashrate_hs=status.total_hashrate_hs
        )
    except Exception:  # pragma: no cover - notifications never break collection
        logger.warning("notification « en ligne » en échec", exc_info=True)
    db.commit()
    return rows


def _store_offline(db, machine, error: str, ts: str) -> None:
    from app.models import MachineStatus

    if not machine.uuid:
        machine.uuid = str(uuid_lib.uuid4())
    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    )
    if status is None:
        status = MachineStatus(machine_uuid=machine.uuid)
        db.add(status)
    status.ts = ts
    status.online = False
    status.error = (error or "collecte impossible")[:500]
    for field in ("miner", "algo", "coin", "uptime_s", "accepted", "rejected",
                  "api_port", "api_error", "total_hashrate_hs", "total_power_w"):
        setattr(status, field, None if field not in ("miner", "algo", "coin") else "")
    try:
        from app import notifications

        notifications.machine_result(db, machine, online=False, error=status.error)
    except Exception:  # pragma: no cover - notifications never break collection
        logger.warning("notification « hors ligne » en échec", exc_info=True)
    db.commit()


def _mark_offline(machine_id: int, error: str) -> None:
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        if machine:
            _store_offline(db, machine, error, _now())
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not mark machine %s offline", machine_id)
        db.rollback()
    finally:
        db.close()


def purge_old_metrics() -> int:
    """Delete metrics older than the retention window (default 7 days)."""
    from app.database import SessionLocal
    from app.models import Metric

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days())).isoformat()
    db = SessionLocal()
    try:
        deleted = db.query(Metric).filter(Metric.ts < cutoff).delete()
        db.commit()
        return int(deleted or 0)
    finally:
        db.close()


def _purge_once_a_day() -> None:
    global _last_purge_date
    today = datetime.now(timezone.utc).date().isoformat()
    if _last_purge_date == today:
        return
    try:
        purge_old_metrics()
        _last_purge_date = today
    except Exception:  # pragma: no cover - defensive
        logger.warning("metrics purge failed", exc_info=True)


# ── One machine ───────────────────────────────────────────────────────
async def collect_machine(machine_id: int) -> dict[str, Any]:
    """Collect, parse and store one machine. Never raises."""
    from app.database import SessionLocal
    from app.models import Machine
    from app.ssh_client import ssh_collect_stats

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        if machine is None:
            return {"machine_id": machine_id, "online": False, "error": "machine inconnue"}
        if not machine.enabled or str(machine.usage) == "none":
            return {"machine_id": machine_id, "skipped": True}
        if str(machine.auth_type) == "provision_pending":
            # §2.2: never attempt a connection before the account exists.
            return {
                "machine_id": machine_id,
                "name": machine.name,
                "skipped": True,
                "error": "machine pas encore préparée (provision_pending)",
            }

        raw = await ssh_collect_stats(machine, actor="system")
        ts = _now()
        if not raw.get("ok"):
            error = raw.get("error", "collecte impossible")
            _store_offline(db, machine, error, ts)
            logger.info("machine %s hors ligne: %s", machine.name, error)
            return {"machine_id": machine_id, "name": machine.name, "online": False, "error": error}

        catalogue = _coin_catalogue(db)
        stats = parse_collect_stats(
            raw.get("stdout", ""),
            patterns=_patterns(db),
            coin_hosts=catalogue["hosts"],
            coin_tickers=catalogue["tickers"],
            coin_names=catalogue["names"],
            coin_aliases=catalogue["aliases"],
        )
        from app.devices import mark_external_devices, set_external_miners

        miners = stats.get("miners") or []
        set_external_miners(machine, miners)
        db.commit()
        # §4.2: first collection of a machine without devices → detect once,
        # so this round's GPU metrics attach by PCI bus instead of NULL.
        await _ensure_devices(db, machine)
        # Occupancy is refreshed every round, not only on the first detect.
        mark_external_devices(db, machine, miners)
        db.commit()
        if stats.get("api_port"):
            api, api_error = await _fetch_miner_api(machine, int(stats["api_port"]))
            if api:
                stats = merge_srbminer(stats, api)
            from app.external_miners import normalize_coin
            from app.srbminer_api import distinct_coins, expand_miner_algos

            aliases = catalogue["aliases"]
            stats["coin"] = normalize_coin(
                stats.get("coin", ""), aliases
            )
            # One hashrate per miner: it belongs to the process that answered.
            total = stats.get("total_hashrate_hs")
            for miner in stats.get("miners") or []:
                if miner.get("api_port") and total is not None:
                    miner["hashrate_hs"] = total
            # A miner with several algorithms is one line per algorithm, each
            # with its own coin and hashrate (SRBMiner on the rig, 003-c6).
            if stats.get("srbminer_algos"):
                stats["miners"] = expand_miner_algos(
                    stats.get("miners") or [], stats.pop("srbminer_algos"), aliases
                )
            stats["coins"] = distinct_coins(stats.get("miners") or [])
            # A port that yields no hashrate must be visible, never a silent « — ».
            if stats.get("total_hashrate_hs") is None:
                stats["api_error"] = api_error or (
                    "Hashrate absent : l'API du mineur n'a rien renvoyé"
                )
            else:
                stats["api_error"] = None

        # Miners as displayed (coin + per-miner hashrate included).
        cpu_probe_raw = await _enrich_cpu_state(db, machine, raw.get("stdout", ""), stats)
        set_external_miners(machine, stats.get("miners") or [])
        # Diagnostic 2026.11.003-c8 : la collecte brute porte aussi la sonde CPU
        # (masquée), en tête pour survivre à la troncature de mask_raw_collect.
        combined = raw.get("stdout", "")
        if cpu_probe_raw:
            combined = "### CPU_PROBE\n" + cpu_probe_raw + "\n" + combined
        _store_raw_collection(db, machine, combined)
        rows = _store_metrics(db, machine, stats, ts)
        return {
            "machine_id": machine_id,
            "name": machine.name,
            "online": True,
            "miner": stats.get("miner") or "",
            "api_port": stats.get("api_port"),
            "devices": len(stats.get("devices", [])),
            "rows": rows,
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("collecte machine %s en échec: %s", machine_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return {"machine_id": machine_id, "online": False, "error": str(exc)}
    finally:
        db.close()


async def _guarded(machine_id: int, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    async with semaphore:
        try:
            return await asyncio.wait_for(collect_machine(machine_id), timeout=MACHINE_TIMEOUT)
        except asyncio.TimeoutError:
            error = f"Collecte expirée ({MACHINE_TIMEOUT} s)"
            _mark_offline(machine_id, error)
            return {"machine_id": machine_id, "online": False, "error": error}
        except Exception as exc:  # pragma: no cover - defensive
            _mark_offline(machine_id, f"Erreur de collecte: {exc}")
            return {"machine_id": machine_id, "online": False, "error": str(exc)}


async def collect_all() -> list[dict[str, Any]]:
    """One round over every enabled machine whose usage is not ``none``."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        machine_ids = [
            row.id
            for row in db.query(Machine)
            .filter(Machine.enabled == True, Machine.usage != "none")  # noqa: E712
            .all()
        ]
    finally:
        db.close()

    _purge_once_a_day()
    semaphore = asyncio.Semaphore(MAX_PARALLEL)
    results = await asyncio.gather(
        *(_guarded(machine_id, semaphore) for machine_id in machine_ids),
        return_exceptions=True,
    )
    out: list[dict[str, Any]] = []
    for machine_id, result in zip(machine_ids, results):
        if isinstance(result, BaseException):
            out.append({"machine_id": machine_id, "online": False, "error": str(result)})
        else:
            out.append(result)
    return out


# ── Scheduler ─────────────────────────────────────────────────────────
async def _loop() -> None:
    while True:
        try:
            await collect_all()
        except Exception:  # pragma: no cover - defensive
            logger.warning("collecte globale en échec", exc_info=True)
        interval = collect_interval()
        await asyncio.sleep(interval if interval > 0 else 60)


def start() -> None:
    """Start the background loop (no-op when the interval is 0)."""
    global _task
    if collect_interval() <= 0:
        return
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.create_task(_loop())
    except RuntimeError:  # pragma: no cover - no running loop at import time
        _task = None


def stop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
