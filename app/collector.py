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
import logging
import os
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any

from app.hw_parser import norm_bus
from app.stats_parser import merge_srbminer, parse_collect_stats, parse_srbminer_api

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
        raw = await ssh_detect_hw(machine)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("détection automatique de %s en échec: %s", machine.name, exc)
        return False

    if not raw.get("ok"):
        logger.info("détection automatique de %s impossible: %s", machine.name, raw.get("error"))
        db.add(AuditLog(
            actor="scheduler",
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

    summary = apply_detection(db, machine, detected)
    db.add(AuditLog(
        actor="scheduler",
        action="detect",
        machine_id=machine.id,
        object_ref=machine.name,
        result="ok",
        output=("détection automatique (1re collecte) " + str(summary))[:500],
    ))
    db.commit()
    return True


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
                  "api_port", "total_hashrate_hs", "total_power_w"):
        setattr(status, field, None if field not in ("miner", "algo", "coin") else "")
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

        raw = await ssh_collect_stats(machine)
        ts = _now()
        if not raw.get("ok"):
            error = raw.get("error", "collecte impossible")
            _store_offline(db, machine, error, ts)
            logger.info("machine %s hors ligne: %s", machine.name, error)
            return {"machine_id": machine_id, "name": machine.name, "online": False, "error": error}

        stats = parse_collect_stats(raw.get("stdout", ""))
        # §4.2: first collection of a machine without devices → detect once,
        # so this round's GPU metrics attach by PCI bus instead of NULL.
        await _ensure_devices(db, machine)
        if stats.get("api_port"):
            api = await fetch_srbminer(machine.host, int(stats["api_port"]))
            if api:
                stats = merge_srbminer(stats, api)

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
