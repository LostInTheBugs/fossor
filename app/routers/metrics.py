"""Metrics API — live snapshot and history (§6 écran 1, §7)."""

from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_login
from app.models import Device, Machine, MachineStatus, Metric
from app.devices import occupancy
from app.stats_parser import format_hashrate

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _external_miners(db, machine) -> list[dict]:
    """Last external miners seen on *machine* (JSON column, masked)."""
    import json

    from app.devices import filter_external_miners

    raw = getattr(machine, "external_miners", None)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return filter_external_miners(db, machine, value if isinstance(value, list) else [])


def _coin_groups(miners: list[dict], coin: str, algo: str,
                 total_hs: float | None) -> list[dict[str, Any]]:
    """One group per coin mined by *machine* (2026.11.003-c4).

    A machine may run several miners on different coins (PRL + WAM + KSH on the
    rig): the dashboard needs one card per coin, never one card per machine.
    """
    groups: dict[str, dict[str, Any]] = {}
    for miner in miners:
        ticker = str(miner.get("coin") or "").strip().upper()
        key = ticker or "?"
        group = groups.setdefault(key, {
            "coin": ticker,
            "algo": "",
            "hashrate_hs": None,
            "miners": 0,
            "workers": 0,
        })
        group["miners"] += 1
        try:
            group["workers"] += int(miner.get("workers") or miner.get("count") or 1)
        except (TypeError, ValueError):
            group["workers"] += 1
        if not group["algo"] and miner.get("algorithm"):
            group["algo"] = str(miner["algorithm"])
        hashrate = miner.get("hashrate_hs")
        if isinstance(hashrate, (int, float)):
            group["hashrate_hs"] = (group["hashrate_hs"] or 0) + float(hashrate)
    if not groups and (coin or total_hs):
        groups["?"] = {
            "coin": str(coin or "").upper(),
            "algo": str(algo or ""),
            "hashrate_hs": total_hs,
            "miners": 0,
            "workers": 0,
        }
    return list(groups.values())


def _age_seconds(ts: str | None, now: datetime) -> int | None:
    if not ts:
        return None
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((now - moment).total_seconds()))


def _latest_metrics(db: Session, machine_uuid: str) -> dict[str, Any]:
    """Most recent metric row per device_uuid for one machine."""
    sub = (
        db.query(Metric.device_uuid.label("device_uuid"), func.max(Metric.ts).label("ts"))
        .filter(Metric.machine_uuid == machine_uuid)
        .group_by(Metric.device_uuid)
        .subquery()
    )
    rows = (
        db.query(Metric)
        .join(
            sub,
            and_(
                Metric.device_uuid == sub.c.device_uuid,
                Metric.ts == sub.c.ts,
                Metric.machine_uuid == machine_uuid,
            ),
        )
        .all()
    )
    return {row.device_uuid: row for row in rows}


def _metric_dict(row: Metric | None) -> dict[str, Any]:
    if row is None:
        return {
            "hashrate_hs": None,
            "hashrate_human": "—",
            "temp_c": None,
            "fan_pct": None,
            "power_w": None,
            "load_pct": None,
        }
    return {
        "hashrate_hs": row.hashrate_hs,
        "hashrate_human": format_hashrate(row.hashrate_hs),
        "temp_c": row.temp_c,
        "fan_pct": row.fan_pct,
        "power_w": row.power_w,
        "load_pct": row.load_pct,
    }


@router.get("/live")
def metrics_live(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Last known state of every machine and its devices."""
    now = datetime.now(timezone.utc)
    statuses = {
        status.machine_uuid: status for status in db.query(MachineStatus).all()
    }
    machines = db.query(Machine).order_by(Machine.name.asc()).all()

    payload: list[dict[str, Any]] = []
    for machine in machines:
        status = statuses.get(machine.uuid)
        collected = bool(machine.enabled) and str(machine.usage) != "none"
        latest = _latest_metrics(db, machine.uuid) if machine.uuid else {}

        devices = (
            db.query(Device)
            .filter(Device.machine_id == machine.id, Device.present == True)  # noqa: E712
            .all()
        )
        devices.sort(key=lambda d: (0 if d.kind == "cpu" else 1, d.index if d.index is not None else 0))

        cpu_state = _cpu_state(status)
        cpu_extra = ({
            "hashrate_by_algo": cpu_state.get("hashrate_by_algo") or [],
            "hashrate_label": cpu_state.get("hashrate_label") or "",
        } if cpu_state else {})
        device_rows = []
        for device in devices:
            metric = _metric_dict(latest.get(device.uuid))
            device_rows.append({
                "uuid": device.uuid,
                "kind": device.kind,
                "index": device.index,
                "vendor": device.vendor,
                "model": device.model,
                "pci_bus": device.pci_bus,
                "integrated": device.integrated,
                "enabled_for_mining": device.enabled_for_mining,
                "occupied_external": bool(getattr(device, "occupied_external", False)),
                # 025 : même occupation que Campagnes (`occupied_by`).
                **occupancy(device),
                **(cpu_extra if device.kind == "cpu" else {}),
                **metric,
            })

        total_hs = status.total_hashrate_hs if status else None
        if total_hs is None:
            values = [d["hashrate_hs"] for d in device_rows if d["hashrate_hs"]]
            total_hs = sum(values) if values else None
        total_w = status.total_power_w if status else None
        if total_w is None:
            values = [d["power_w"] for d in device_rows if d["power_w"]]
            total_w = sum(values) if values else None

        miners = _external_miners(db, machine)
        # §4.20 : un ASIC n'a pas de device ; son état vit dans asic_json et
        # alimente directement les totaux par coin (BTC, KAS).
        asic_state = None
        if getattr(machine, "machine_kind", "host") == "asic" and status and status.asic_json:
            try:
                asic_state = json.loads(status.asic_json)
            except ValueError:
                asic_state = None
        groups = _coin_groups(
            miners,
            status.coin if status else "",
            status.algo if status else "",
            total_hs,
        )
        # §4.19 : l'algorithme vient de la commande du mineur s'il y est,
        # sinon du catalogue — jamais de l'ancien ``status.algo`` (WAM y était
        # annoncé yespower alors qu'il mine en RandomX).
        from app.coins import algo_for_coin

        for group in groups:
            if not (group.get("algo") or "").strip():
                group["algo"] = algo_for_coin(db, group.get("coin") or "") or (
                    status.algo if status else ""
                )
        wall_w, worst_pct = total_w, 0.0
        power_source = ""
        try:
            from app.routers.psu import evaluate_machine_power

            psu_power = evaluate_machine_power(db, machine)
            wall_w = psu_power["total_wall_w"]
            worst_pct = psu_power["worst_load_pct"]
            power_source = str(psu_power.get("power_source") or "")
        except Exception:  # pragma: no cover - la conso ne casse jamais le live
            pass
        payload.append({
            "uuid": machine.uuid,
            "name": machine.name,
            "host": machine.host,
            "kind": getattr(machine, "machine_kind", "host") or "host",
            "asic": asic_state,
            "os_kind": machine.os_kind,
            "usage": machine.usage,
            "enabled": machine.enabled,
            "collected": collected,
            "online": bool(status.online) if status else False,
            "error": status.error if status else None,
            "miner": status.miner if status else "",
            "algo": status.algo if status else "",
            "coin": status.coin if status else "",
            "coins": [group["coin"] for group in groups if group["coin"]],
            "uptime_s": status.uptime_s if status else None,
            "accepted": status.accepted if status else None,
            "rejected": status.rejected if status else None,
            "api_port": status.api_port if status else None,
            "api_error": status.api_error if status else None,
            "total_hashrate_hs": total_hs,
            "total_hashrate_human": format_hashrate(total_hs),
            "total_power_w": total_w,
            "wall_power_w": wall_w,
            "power_source": power_source,
            "worst_psu_load_pct": worst_pct,
            "last_ts": status.ts if status else None,
            "age_s": _age_seconds(status.ts if status else None, now),
            "devices": device_rows,
            "external_miners": miners,
            "coin_groups": groups,
            # 2026.11.003-c7 : état du CPU (charge, température, fréquence,
            # puissance) et hashrate de chaque mineur CPU.
            "cpu": cpu_state,
            "cpu_freq_mhz": (cpu_state or {}).get("freq_mhz"),
        })
    return payload


def _cpu_state(status) -> dict[str, Any] | None:
    """Last CPU state of one machine (JSON column written by the collector)."""
    import json

    raw = getattr(status, "cpu_json", None) if status else None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    # 2026.11.004 : les champs de fréquence existent toujours, même vides — une
    # clé absente se lisait comme « l'API ne remonte pas la fréquence ».
    value.setdefault("freq_mhz", None)
    value.setdefault("freq_source", "")
    freq = value.get("freq_mhz")
    if isinstance(freq, (int, float)) and freq > 0:
        value["freq_mhz"] = round(float(freq), 1)
        value["freq_ghz"] = round(float(freq) / 1000.0, 2)
    else:
        value["freq_mhz"] = None
        value["freq_ghz"] = None
        value["freq_source"] = value.get("freq_source") or "non mesurée"
    return value


@router.get("/miner-patterns")
def get_miner_patterns(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Configurable external-miner patterns (display name + process regex)."""
    from app.external_miners import DEFAULT_PATTERNS, get_patterns

    return {
        "patterns": get_patterns(db),
        "defaults": [dict(entry) for entry in DEFAULT_PATTERNS],
    }


@router.put("/miner-patterns")
def put_miner_patterns(
    data: dict,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Replace the pattern list (Configuration screen, no new release needed)."""
    from app.external_miners import set_patterns

    entries = data.get("patterns")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="« patterns » doit être une liste")
    saved = set_patterns(db, entries)
    db.commit()
    return {"patterns": saved}


@router.get("/history")
def metrics_history(
    machine: str = Query(..., description="machine UUID"),
    hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Metric points of one machine over the last *hours* hours."""
    target = db.query(Machine).filter(Machine.uuid == machine).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Machine not found")

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = (
        db.query(Metric)
        .filter(Metric.machine_uuid == machine, Metric.ts >= since)
        .order_by(Metric.ts.asc())
        .limit(20000)
        .all()
    )
    return {
        "machine": machine,
        "name": target.name,
        "hours": hours,
        "count": len(rows),
        "points": [
            {
                "ts": row.ts,
                "device_uuid": row.device_uuid,
                "hashrate_hs": row.hashrate_hs,
                "temp_c": row.temp_c,
                "fan_pct": row.fan_pct,
                "power_w": row.power_w,
                "load_pct": row.load_pct,
            }
            for row in rows
        ],
    }
