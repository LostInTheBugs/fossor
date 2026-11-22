"""Historique §10.7 — rétention, agrégation bornée et export comptable.

Trois étages :

* ``metrics`` : données brutes par device, purge après ``raw_days`` (30 j) ;
* ``metric_aggs`` bucket ``hour`` : moyenne/max par heure, purge après
  ``hourly_days`` (365 j) ;
* ``metric_aggs`` bucket ``day`` : moyenne/max par jour, jamais purgé.

L'agrégation et la purge avancent par curseurs persistés (``AppSetting``),
en lots bornés : un passage ne peut pas monopoliser la base. Chaque passage
est journalisé (logger + ``observability.mark_routine``).

Ajouts §10.7 : puissance à la prise par machine (``plug_power_samples``) et
prix datés (``price_samples``), nécessaires pour valoriser une réception à
sa date dans l'export comptable.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RETENTION_KEY = "history_retention"
AGG_CURSOR_KEY = "history_agg_cursor"
DAY_CURSOR_KEY = "history_day_cursor"
PLUG_CURSOR_KEY = "history_plug_agg_cursor"

DEFAULT_RETENTION = {"raw_days": 30, "hourly_days": 365, "daily_days": 0}

EXPORT_NOTE = "Données fournies à titre informatif, pas de conseil fiscal"
EXPORT_HEADER = [
    "coin",
    "quantite",
    "date_reception",
    "valeur_eur",
    "prix_source",
    "source",
    "reference",
]

# Bornes dures : un passage d'agrégation / purge ne dépasse jamais ces lots.
MAX_CHUNKS = 20
CHUNK_HOURS = 24
CHUNK_DAYS = 7
MAX_ROWS_PER_CHUNK = 200_000
PURGE_BATCH = 5_000
PURGE_MAX_BATCHES = 20


# ── Réglages ──────────────────────────────────────────────────────────
def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _get_setting(db: Session, key: str) -> Any:
    from app.models import AppSetting

    row = db.get(AppSetting, key)
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return None


def _set_setting(db: Session, key: str, value: Any) -> None:
    from app.models import AppSetting

    row = db.get(AppSetting, key)
    payload = json.dumps(value, ensure_ascii=False)
    stamp = datetime.now(timezone.utc).isoformat()
    if row is None:
        db.add(AppSetting(key=key, value=payload, updated_at=stamp))
    else:
        row.value = payload
        row.updated_at = stamp
    db.commit()


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def get_retention(db: Session) -> dict[str, int]:
    """Rétention effective. ``daily_days = 0`` signifie « jamais purgé »."""
    stored = _get_setting(db, RETENTION_KEY) or {}
    if not isinstance(stored, dict):
        stored = {}
    return {
        "raw_days": _clamp_int(stored.get("raw_days"), 1, 3650, DEFAULT_RETENTION["raw_days"]),
        "hourly_days": _clamp_int(
            stored.get("hourly_days"), 1, 3650, DEFAULT_RETENTION["hourly_days"]
        ),
        # 0 = illimité (§10.7) ; une valeur > 0 est acceptée mais jamais imposée.
        "daily_days": _clamp_int(stored.get("daily_days"), 0, 3650, 0),
    }


def set_retention(db: Session, payload: dict[str, Any]) -> dict[str, int]:
    current = get_retention(db)
    merged = {
        "raw_days": _clamp_int(payload.get("raw_days"), 1, 3650, current["raw_days"]),
        "hourly_days": _clamp_int(
            payload.get("hourly_days"), 1, 3650, current["hourly_days"]
        ),
        "daily_days": _clamp_int(payload.get("daily_days"), 0, 3650, current["daily_days"]),
    }
    _set_setting(db, RETENTION_KEY, merged)
    return merged


# ── Puissance à la prise ──────────────────────────────────────────────
def _psu_dict(psu) -> dict[str, Any]:
    return {
        "id": psu.id,
        "name": psu.name,
        "rated_w": psu.rated_w,
        "certification": psu.certification,
        "amps_12v": psu.amps_12v,
        "derate_pct": psu.derate_pct,
        "measured_efficiency": psu.measured_efficiency,
    }


def _component_dict(component) -> dict[str, Any]:
    return {
        "id": component.id,
        "kind": component.kind,
        "label": component.label,
        "watts": component.watts,
        "quantity": component.quantity,
        "psu_id": component.psu_id,
    }


def plug_power(db: Session, machine, total_dc_w: float | None = None) -> dict[str, Any]:
    """Puissance à la prise d'une machine : DC / rendement quand une PSU existe.

    Sans PSU déclarée, aucune mesure à la prise n'existe : on renvoie la
    puissance DC avec ``source = "dc"`` — jamais une valeur inventée.
    """
    from app.models import Device, MachineComponent, Metric, Psu

    psus = db.query(Psu).filter(Psu.machine_id == machine.id).all()
    dc = total_dc_w
    if dc is None:
        latest = (
            db.query(func.max(Metric.ts))
            .filter(Metric.machine_uuid == machine.uuid)
            .scalar()
        )
        if latest:
            dc = (
                db.query(func.sum(Metric.power_w))
                .filter(Metric.machine_uuid == machine.uuid, Metric.ts == latest)
                .scalar()
            )
    dc = float(dc) if dc is not None else None
    if not psus:
        return {"power_w": dc, "dc_w": dc, "source": "dc"}
    from app.psu import compute_power

    components = (
        db.query(MachineComponent).filter(MachineComponent.machine_id == machine.id).all()
    )
    latest = (
        db.query(func.max(Metric.ts)).filter(Metric.machine_uuid == machine.uuid).scalar()
    )
    measured = (
        db.query(Metric).filter(Metric.machine_uuid == machine.uuid, Metric.ts == latest).all()
        if latest
        else []
    )
    devices = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present == True)  # noqa: E712
        .all()
    )
    power_by_device = {
        row.device_uuid: row.power_w for row in measured if row.device_uuid and row.power_w is not None
    }
    device_rows = [
        {
            "kind": device.kind,
            "model": device.model,
            "power_w": power_by_device.get(device.uuid),
            "psu_id": device.psu_id,
            "label": device.model or device.kind,
        }
        for device in devices
    ]
    try:
        result = compute_power(
            [_psu_dict(psu) for psu in psus],
            [_component_dict(component) for component in components],
            device_rows,
            machine_power_w=dc,
        )
    except Exception:  # pragma: no cover - une PSU incomplète ne casse pas la collecte
        logger.warning("calcul de puissance à la prise en échec", exc_info=True)
        return {"power_w": dc, "dc_w": dc, "source": "dc"}
    wall = result.get("total_wall_w")
    return {
        "power_w": round(float(wall), 1) if wall else dc,
        "dc_w": round(float(result.get("total_dc_w") or dc or 0.0), 1) if dc is not None else None,
        "source": "psu",
    }


def record_plug_sample(db: Session, machine, total_dc_w: float | None = None) -> Any | None:
    """Historise la puissance à la prise de la machine (une ligne par collecte)."""
    from app.models import PlugPowerSample

    if not machine.uuid:
        return None
    measured = plug_power(db, machine, total_dc_w)
    if measured.get("power_w") is None:
        return None
    row = PlugPowerSample(
        machine_uuid=machine.uuid,
        ts=datetime.now(timezone.utc).isoformat(),
        power_w=measured["power_w"],
        dc_w=measured.get("dc_w"),
        source=measured.get("source") or "dc",
    )
    db.add(row)
    return row


# ── Prix datés ────────────────────────────────────────────────────────
def record_prices(db: Session, coins: Iterable, quotes: dict[str, dict[str, Any]]) -> int:
    """Enregistre un point de prix par coin disposant d'un prix à cet instant."""
    from app.models import PriceSample

    stamp = datetime.now(timezone.utc).isoformat()
    rows = 0
    for coin in coins:
        cgid = getattr(coin, "coingecko_id", None)
        quote = quotes.get(cgid) if cgid else None
        if not isinstance(quote, dict):
            continue
        eur, usd = quote.get("eur"), quote.get("usd")
        if eur is None and usd is None:
            continue
        db.add(
            PriceSample(
                ticker=coin.ticker,
                ts=stamp,
                eur=eur,
                usd=usd,
                source=str(quote.get("source") or "coingecko"),
            )
        )
        rows += 1
    return rows


# ── Agrégation bornée ─────────────────────────────────────────────────
def _parse(ts: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor(value: datetime, unit: str) -> datetime:
    if unit == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    return value.replace(minute=0, second=0, microsecond=0)


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _max(values: list[float]) -> float | None:
    return round(max(values), 4) if values else None


def _collect_group(rows: list[Any]) -> dict[str, Any]:
    hashrates = [float(r.hashrate_hs) for r in rows if r.hashrate_hs is not None]
    powers = [float(r.power_w) for r in rows if r.power_w is not None]
    temps = [float(r.temp_c) for r in rows if r.temp_c is not None]
    return {
        "hashrate_avg_hs": _avg(hashrates),
        "hashrate_max_hs": _max(hashrates),
        "power_avg_w": _avg(powers),
        "power_max_w": _max(powers),
        "temp_avg_c": _avg(temps),
        "temp_max_c": _max(temps),
        "samples": len(rows),
    }


def _upsert_hour(db: Session, bucket: str, ts: str, values: dict[str, Any]) -> bool:
    from app.models import MetricAgg

    existing = (
        db.query(MetricAgg)
        .filter(
            MetricAgg.scope == values["scope"],
            MetricAgg.scope_uuid == values["scope_uuid"],
            MetricAgg.bucket == bucket,
            MetricAgg.ts == ts,
        )
        .first()
    )
    if existing is not None:
        return False
    db.add(MetricAgg(bucket=bucket, ts=ts, **values))
    return True


def _machine_hour_rows(rows: list[Any], hour: datetime) -> list[dict[str, Any]]:
    """Un bucket machine par heure : sommes par instant, puis moyenne.

    La puissance d'une machine est la somme de ses devices au même instant :
    moyenner les lignes device une à une sous-estimerait un rig multi-GPU.
    """
    by_machine: dict[str, dict[str, list[Any]]] = {}
    for row in rows:
        by_machine.setdefault(row.machine_uuid, {}).setdefault(row.ts, []).append(row)
    out: list[dict[str, Any]] = []
    for machine_uuid, by_ts in by_machine.items():
        powers, hashrates, dc, temps = [], [], [], []
        for _ts, items in by_ts.items():
            power = [float(item.power_w) for item in items if item.power_w is not None]
            hashrate = [float(item.hashrate_hs) for item in items if item.hashrate_hs is not None]
            temp = [float(item.temp_c) for item in items if item.temp_c is not None]
            if power:
                powers.append(sum(power))
            if hashrate:
                hashrates.append(sum(hashrate))
            if temp:
                temps.append(sum(temp) / len(temp))
            dc.append(_ts)
        out.append(
            {
                "scope": "machine",
                "scope_uuid": machine_uuid,
                "hashrate_avg_hs": _avg(hashrates),
                "hashrate_max_hs": _max(hashrates),
                "power_avg_w": _avg(powers),
                "power_max_w": _max(powers),
                "temp_avg_c": _avg(temps),
                "temp_max_c": _max(temps),
                "samples": len(dc),
            }
        )
    return out


def _plug_avgs(db: Session, start: datetime, end: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    """Moyenne/max de la puissance à la prise par (machine, heure)."""
    from app.models import PlugPowerSample

    rows = (
        db.query(PlugPowerSample)
        .filter(PlugPowerSample.ts >= start.isoformat(), PlugPowerSample.ts < end.isoformat())
        .all()
    )
    grouped: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        stamp = _parse(row.ts)
        if stamp is None or row.power_w is None:
            continue
        key = (row.machine_uuid, _floor(stamp, "hour").isoformat())
        grouped.setdefault(key, []).append(float(row.power_w))
    return {
        key: {
            "power_plug_avg_w": _avg(values),
            "power_plug_max_w": _max(values),
        }
        for key, values in grouped.items()
    }


def aggregate(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Agrège les données brutes en heures, puis les heures en jours.

    Borné : ``MAX_CHUNKS`` lots par passage, chaque lot limité en lignes ;
    les curseurs persistés évitent de relire le même intervalle.
    """
    from app.models import Metric, MetricAgg

    moment = _now(now)
    cutoff = (moment - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    cursor_raw = _get_setting(db, AGG_CURSOR_KEY)
    cursor = _parse(cursor_raw) if cursor_raw else None
    if cursor is None:
        oldest = db.query(func.min(Metric.ts)).scalar()
        cursor = _parse(oldest) if oldest else cutoff
        if cursor is None:
            cursor = cutoff
        cursor = _floor(cursor, "hour")
    if cursor >= cutoff:
        return {"hour_buckets": 0, "day_buckets": 0, "truncated": False}

    hour_buckets = 0
    truncated = False
    chunks = 0
    while chunks < MAX_CHUNKS and cursor < cutoff:
        chunks += 1
        end = min(cursor + timedelta(hours=CHUNK_HOURS), cutoff)
        rows = (
            db.query(Metric)
            .filter(Metric.ts >= cursor.isoformat(), Metric.ts < end.isoformat())
            .order_by(Metric.ts.asc())
            .limit(MAX_ROWS_PER_CHUNK)
            .all()
        )
        if not rows:
            cursor = end
            _set_setting(db, AGG_CURSOR_KEY, cursor.isoformat())
            continue
        last_ts = _parse(rows[-1].ts) or cursor
        # Regroupe par (heure, machine/device)
        by_hour: dict[datetime, list[Any]] = {}
        for row in rows:
            stamp = _parse(row.ts)
            if stamp is None:
                continue
            by_hour.setdefault(_floor(stamp, "hour"), []).append(row)
        plug = _plug_avgs(db, cursor, end)
        for hour, hour_rows in sorted(by_hour.items()):
            ts = hour.isoformat()
            for values in _machine_hour_rows(hour_rows, hour):
                key = (values["scope_uuid"], ts)
                values.update(plug.get(key, {}))
                if _upsert_hour(db, "hour", ts, values):
                    hour_buckets += 1
            device_groups: dict[str, list[Any]] = {}
            for row in hour_rows:
                if row.device_uuid:
                    device_groups.setdefault(row.device_uuid, []).append(row)
            for device_uuid, device_rows in device_groups.items():
                values = {"scope": "device", "scope_uuid": device_uuid, **_collect_group(device_rows)}
                if _upsert_hour(db, "hour", ts, values):
                    hour_buckets += 1
        db.commit()
        if len(rows) >= MAX_ROWS_PER_CHUNK:
            truncated = True
            cursor = _floor(last_ts, "hour")
        else:
            cursor = end
        _set_setting(db, AGG_CURSOR_KEY, cursor.isoformat())

    # ── Heures → jours ───────────────────────────────────────────────
    day_cutoff = (moment - timedelta(hours=48)).replace(minute=0, second=0, microsecond=0)
    day_cursor_raw = _get_setting(db, DAY_CURSOR_KEY)
    day_cursor = _parse(day_cursor_raw) if day_cursor_raw else None
    oldest_hour = db.query(func.min(MetricAgg.ts)).filter(MetricAgg.bucket == "hour").scalar()
    if day_cursor is None:
        parsed_oldest = _parse(oldest_hour) if oldest_hour else None
        day_cursor = _floor(parsed_oldest, "hour") if parsed_oldest else day_cutoff
    day_buckets = 0
    chunks = 0
    while day_cursor and day_cursor < day_cutoff and chunks < MAX_CHUNKS:
        chunks += 1
        end = min(day_cursor + timedelta(days=CHUNK_DAYS), day_cutoff)
        rows = (
            db.query(MetricAgg)
            .filter(
                MetricAgg.bucket == "hour",
                MetricAgg.ts >= day_cursor.isoformat(),
                MetricAgg.ts < end.isoformat(),
            )
            .order_by(MetricAgg.ts.asc())
            .limit(MAX_ROWS_PER_CHUNK)
            .all()
        )
        if not rows:
            day_cursor = end
            _set_setting(db, DAY_CURSOR_KEY, day_cursor.isoformat())
            continue
        grouped: dict[tuple[str, str, str], list[Any]] = {}
        for row in rows:
            stamp = _parse(row.ts)
            if stamp is None:
                continue
            key = (row.scope, row.scope_uuid, _floor(stamp, "day").isoformat())
            grouped.setdefault(key, []).append(row)
        for (scope, scope_uuid, day_ts), items in grouped.items():
            if _merge_day(db, scope, scope_uuid, day_ts, items):
                day_buckets += 1
        db.commit()
        last_hour = _parse(rows[-1].ts) or day_cursor
        day_cursor = end if len(rows) < MAX_ROWS_PER_CHUNK else last_hour
        _set_setting(db, DAY_CURSOR_KEY, day_cursor.isoformat())

    return {"hour_buckets": hour_buckets, "day_buckets": day_buckets, "truncated": truncated}


def _merge_day(db: Session, scope: str, scope_uuid: str, day_ts: str, hours: list[Any]) -> bool:
    """Fusionne des buckets horaires dans le bucket journalier (moyenne pondérée)."""
    from app.models import MetricAgg

    def weighted(field: str, weight_field: str = "samples") -> float | None:
        total, weight = 0.0, 0
        for item in hours:
            value = getattr(item, field, None)
            count = getattr(item, weight_field, 0) or 0
            if value is None or not count:
                continue
            total += float(value) * int(count)
            weight += int(count)
        return round(total / weight, 4) if weight else None

    def max_of(field: str) -> float | None:
        values = [float(getattr(item, field)) for item in hours if getattr(item, field) is not None]
        return round(max(values), 4) if values else None

    payload = {
        "hashrate_avg_hs": weighted("hashrate_avg_hs"),
        "hashrate_max_hs": max_of("hashrate_max_hs"),
        "power_avg_w": weighted("power_avg_w"),
        "power_max_w": max_of("power_max_w"),
        "power_plug_avg_w": weighted("power_plug_avg_w"),
        "power_plug_max_w": max_of("power_plug_max_w"),
        "temp_avg_c": weighted("temp_avg_c"),
        "temp_max_c": max_of("temp_max_c"),
        "samples": sum(int(item.samples or 0) for item in hours),
    }
    existing = (
        db.query(MetricAgg)
        .filter(
            MetricAgg.scope == scope,
            MetricAgg.scope_uuid == scope_uuid,
            MetricAgg.bucket == "day",
            MetricAgg.ts == day_ts,
        )
        .first()
    )
    if existing is None:
        db.add(MetricAgg(scope=scope, scope_uuid=scope_uuid, bucket="day", ts=day_ts, **payload))
        return True
    # Un jour peut être traité en plusieurs passages : pondère par les samples.
    old_count = int(existing.samples or 0)
    new_count = int(payload["samples"] or 0)
    total = old_count + new_count
    if total:
        for field in ("hashrate_avg_hs", "power_avg_w", "power_plug_avg_w", "temp_avg_c"):
            old_value = getattr(existing, field)
            new_value = payload[field]
            if old_value is None:
                setattr(existing, field, new_value)
            elif new_value is not None:
                setattr(existing, field, round((float(old_value) * old_count + float(new_value) * new_count) / total, 4))
        for field in ("hashrate_max_hs", "power_max_w", "power_plug_max_w", "temp_max_c"):
            old_value = getattr(existing, field)
            new_value = payload[field]
            if old_value is None or (new_value is not None and new_value > old_value):
                setattr(existing, field, new_value)
    existing.samples = total
    return False


# ── Purge bornée ──────────────────────────────────────────────────────
def _purge_table(db: Session, model, column, cutoff: str, extra: list | None = None) -> tuple[int, bool]:
    deleted, batches = 0, 0
    while batches < PURGE_MAX_BATCHES:
        batches += 1
        filters = [column < cutoff, *(extra or [])]
        ids = [
            row[0]
            for row in db.query(model.id).filter(*filters).limit(PURGE_BATCH).all()
        ]
        if not ids:
            return deleted, False
        deleted += (
            db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        )
        db.commit()
        if len(ids) < PURGE_BATCH:
            return deleted, False
    return deleted, True


def _bounded_cut(hard_cut: datetime, cursor_key: str, db: Session) -> str | None:
    """Coupe de purge : jamais au-delà du curseur d'agrégation correspondant.

    Sans curseur, l'agrégation n'a encore rien traité : on ne purge rien.
    Le curseur peut être en retard de plusieurs passages (lots bornés) ; la
    purge attend alors qu'il ait rattrapé, plutôt que d'effacer du brut non
    agrégé.
    """
    raw = _get_setting(db, cursor_key)
    cursor = _parse(raw) if raw else None
    if cursor is None:
        return None
    return min(hard_cut, cursor).isoformat()


def purge(db: Session, retention: dict[str, int] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Purge les étages selon la rétention, sans jamais dépasser l'agrégation."""
    from app.models import Metric, MetricAgg, PlugPowerSample

    moment = _now(now)
    settings = retention or get_retention(db)
    raw_cut = _bounded_cut(
        moment - timedelta(days=int(settings["raw_days"])), AGG_CURSOR_KEY, db
    )
    hour_cut = _bounded_cut(
        moment - timedelta(days=int(settings["hourly_days"])), DAY_CURSOR_KEY, db
    )

    metrics, cut1 = (0, False)
    plugs, cut2 = (0, False)
    if raw_cut is not None:
        metrics, cut1 = _purge_table(db, Metric, Metric.ts, raw_cut)
        plugs, cut2 = _purge_table(db, PlugPowerSample, PlugPowerSample.ts, raw_cut)
    # Les prix ne sont jamais purgés : l'export comptable relit le prix à la
    # date d'une réception, y compris plusieurs années en arrière.
    prices = 0
    hours, cut4 = (0, False)
    if hour_cut is not None:
        hours, cut4 = _purge_table(
            db,
            MetricAgg,
            MetricAgg.ts,
            hour_cut,
            [MetricAgg.bucket == "hour"],
        )
    daily = 0
    if int(settings.get("daily_days") or 0) > 0:
        day_cut = (moment - timedelta(days=int(settings["daily_days"]))).isoformat()
        daily, _ = _purge_table(
            db, MetricAgg, MetricAgg.ts, day_cut, [MetricAgg.bucket == "day"]
        )
    return {
        "metrics": metrics,
        "plug_samples": plugs,
        "price_samples": prices,
        "hour_buckets": hours,
        "day_buckets": daily,
        "truncated": any((cut1, cut2, cut4)),
    }


def run_maintenance(db: Session | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Agrège puis purge ; journalise le passage. Une panne ne casse pas la collecte."""
    from app.database import SessionLocal

    own = db is None
    session = db or SessionLocal()
    try:
        aggregated = aggregate(session, now)
        purged = purge(session)
        summary = {"aggregate": aggregated, "purge": purged}
        try:
            from app import observability

            observability.mark_routine(
                session,
                "history",
                True,
                (
                    f"{aggregated['hour_buckets']} buckets heure, "
                    f"{aggregated['day_buckets']} buckets jour ; "
                    f"purge {purged['metrics']} métriques brutes"
                ),
                interval_s=86400,
            )
        except Exception:  # pragma: no cover - la journalisation ne bloque rien
            logger.warning("journalisation de l'historique en échec", exc_info=True)
        logger.info("historique : %s", summary)
        return summary
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning("maintenance de l'historique en échec", exc_info=True)
        try:
            from app import observability

            observability.mark_routine(session, "history", False, str(exc)[:200], interval_s=86400)
        except Exception:
            pass
        return {"error": str(exc)}
    finally:
        if own:
            session.close()


# ── Séries ────────────────────────────────────────────────────────────
RANGES = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "1y": timedelta(days=365),
}


def window(range_key: str, start: str | None, end: str | None, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Fenêtre UTC demandée ; ``custom`` exige ``start`` et ``end``."""
    moment = _now(now)
    stop = _parse(end) if end else moment
    if stop is None:
        raise ValueError("date de fin invalide")
    if range_key == "custom":
        begin = _parse(start)
        if begin is None:
            raise ValueError("période libre : start et end requis")
        if begin >= stop:
            raise ValueError("période vide : start doit précéder end")
        return begin, stop
    if range_key not in RANGES:
        raise ValueError(f"période inconnue : {range_key}")
    begin = _parse(start) if start else moment - RANGES[range_key]
    if begin is None:
        raise ValueError("date de début invalide")
    if begin >= stop:
        raise ValueError("période vide")
    return begin, stop


def series(
    db: Session,
    scope: str,
    target: str,
    range_key: str,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Série temporelle par machine, device ou coin.

    Résolution : brute jusqu'à 48 h, horaire jusqu'à 60 j, journalière au-delà.
    Une période sans données renvoie ``points: []`` et ``count: 0``.
    """
    from app.models import Machine, Metric, MetricAgg, PoolBalanceSample, PriceSample, Wallet

    begin, stop = window(range_key, start, end, now)
    span = stop - begin
    if scope in ("machine", "device"):
        resolution = "raw" if span <= timedelta(hours=48) else ("hour" if span <= timedelta(days=60) else "day")
        points: list[dict[str, Any]] = []
        if resolution == "raw":
            if scope == "machine":
                if db.query(Machine.id).filter(Machine.uuid == target).first() is None:
                    raise LookupError("machine inconnue")
                rows = (
                    db.query(Metric)
                    .filter(Metric.machine_uuid == target, Metric.ts >= begin.isoformat(), Metric.ts < stop.isoformat())
                    .order_by(Metric.ts.asc())
                    .limit(20000)
                    .all()
                )
                grouped: dict[str, list[Any]] = {}
                for row in rows:
                    grouped.setdefault(row.ts, []).append(row)
                for ts, items in grouped.items():
                    point = {"ts": ts, "hashrate_hs": None, "power_w": None, "temp_c": None, "power_plug_w": None}
                    hashrate = [float(i.hashrate_hs) for i in items if i.hashrate_hs is not None]
                    power = [float(i.power_w) for i in items if i.power_w is not None]
                    temp = [float(i.temp_c) for i in items if i.temp_c is not None]
                    if hashrate:
                        point["hashrate_hs"] = round(sum(hashrate), 2)
                    if power:
                        point["power_w"] = round(sum(power), 1)
                    if temp:
                        point["temp_c"] = round(sum(temp) / len(temp), 1)
                    points.append(point)
            else:
                rows = (
                    db.query(Metric)
                    .filter(Metric.device_uuid == target, Metric.ts >= begin.isoformat(), Metric.ts < stop.isoformat())
                    .order_by(Metric.ts.asc())
                    .limit(20000)
                    .all()
                )
                points = [
                    {
                        "ts": row.ts,
                        "hashrate_hs": row.hashrate_hs,
                        "power_w": row.power_w,
                        "temp_c": row.temp_c,
                        "power_plug_w": None,
                    }
                    for row in rows
                ]
        else:
            bucket = resolution
            rows = (
                db.query(MetricAgg)
                .filter(
                    MetricAgg.scope == scope,
                    MetricAgg.scope_uuid == target,
                    MetricAgg.bucket == bucket,
                    MetricAgg.ts >= begin.isoformat(),
                    MetricAgg.ts < stop.isoformat(),
                )
                .order_by(MetricAgg.ts.asc())
                .limit(20000)
                .all()
            )
            points = [
                {
                    "ts": row.ts,
                    "hashrate_hs": row.hashrate_avg_hs,
                    "hashrate_max_hs": row.hashrate_max_hs,
                    "power_w": row.power_avg_w,
                    "power_max_w": row.power_max_w,
                    "temp_c": row.temp_avg_c,
                    "temp_max_c": row.temp_max_c,
                    "power_plug_w": row.power_plug_avg_w,
                    "power_plug_max_w": row.power_plug_max_w,
                    "samples": row.samples,
                }
                for row in rows
            ]
        return {
            "scope": scope,
            "target": target,
            "range": range_key,
            "resolution": resolution,
            "start": begin.isoformat(),
            "end": stop.isoformat(),
            "count": len(points),
            "points": points,
        }

    if scope != "coin":
        raise ValueError(f"portée inconnue : {scope}")

    wallets = db.query(Wallet).filter(Wallet.coin_ticker == target.upper()).all()
    if not wallets:
        return {
            "scope": "coin",
            "target": target.upper(),
            "range": range_key,
            "resolution": "raw",
            "start": begin.isoformat(),
            "end": stop.isoformat(),
            "count": 0,
            "points": [],
        }
    wallet_ids = [wallet.id for wallet in wallets]
    samples = (
        db.query(PoolBalanceSample)
        .filter(
            PoolBalanceSample.wallet_id.in_(wallet_ids),
            PoolBalanceSample.ts >= begin.isoformat(),
            PoolBalanceSample.ts < stop.isoformat(),
        )
        .order_by(PoolBalanceSample.ts.asc())
        .limit(20000)
        .all()
    )
    by_ts: dict[str, float] = {}
    for sample in samples:
        if sample.total is None:
            continue
        by_ts[sample.ts] = round(by_ts.get(sample.ts, 0.0) + float(sample.total), 12)
    prices = (
        db.query(PriceSample)
        .filter(PriceSample.ticker == target.upper(), PriceSample.ts < stop.isoformat())
        .order_by(PriceSample.ts.asc())
        .limit(20000)
        .all()
    )
    price_points = [(price.ts, price.eur) for price in prices if price.eur is not None]
    points = []
    for ts, balance in by_ts.items():
        price = None
        for price_ts, value in price_points:
            if price_ts <= ts:
                price = value
            else:
                break
        points.append(
            {
                "ts": ts,
                "balance": balance,
                "value_eur": round(balance * float(price), 4) if price is not None else None,
                "price_eur": price,
            }
        )
    return {
        "scope": "coin",
        "target": target.upper(),
        "range": range_key,
        "resolution": "raw",
        "start": begin.isoformat(),
        "end": stop.isoformat(),
        "count": len(points),
        "points": points,
    }


# ── Export comptable ──────────────────────────────────────────────────
def _price_at(db: Session, ticker: str, ts: str, cache: dict[str, list[tuple[str, float, str]]]) -> tuple[float | None, str]:
    from app.models import PriceSample

    key = ticker.upper()
    if key not in cache:
        rows = (
            db.query(PriceSample)
            .filter(PriceSample.ticker == key)
            .order_by(PriceSample.ts.asc())
            .all()
        )
        cache[key] = [(row.ts, float(row.eur), row.source or "") for row in rows if row.eur is not None]
    found = (None, "inconnu")
    for price_ts, eur, source in cache[key]:
        if price_ts <= ts:
            found = (eur, source or "coingecko")
        else:
            break
    return found


def gains_rows(
    db: Session,
    start: str | None = None,
    end: str | None = None,
    coin: str | None = None,
) -> dict[str, Any]:
    """Réceptions déduites des soldes de pool successifs (§4.16).

    Les paiements de pool ne sont pas encore stockés comme un journal de
    paiements : chaque hausse du solde ``paid`` entre deux relevés est une
    réception, datée du relevé. Aucune ligne n'est inventée ; les blocs solo
    ne sont pas historisés et la réponse le dit (``solo_rows: 0``).
    """
    from app.models import PoolBalance, PoolBalanceSample, Wallet

    query = db.query(Wallet)
    if coin:
        query = query.filter(Wallet.coin_ticker == coin.upper())
    wallets = query.order_by(Wallet.coin_ticker.asc(), Wallet.id.asc()).all()
    begin = _parse(start).isoformat() if start else None
    stop = _parse(end).isoformat() if end else None
    price_cache: dict[str, list[tuple[str, float, str]]] = {}
    rows: list[dict[str, Any]] = []
    for wallet in wallets:
        samples = (
            db.query(PoolBalanceSample)
            .filter(PoolBalanceSample.wallet_id == wallet.id)
            .order_by(PoolBalanceSample.ts.asc())
            .all()
        )
        balance = db.query(PoolBalance).filter(PoolBalance.wallet_id == wallet.id).first()
        previous: float | None = None
        previous_ts: str | None = None
        for sample in samples:
            paid = sample.paid
            if paid is None:
                continue
            if previous is not None and paid > previous:
                amount = round(float(paid) - float(previous), 12)
                ts = sample.ts
                if (begin and ts < begin) or (stop and ts >= stop):
                    previous, previous_ts = float(paid), ts
                    continue
                price, source = _price_at(db, wallet.coin_ticker, ts, price_cache)
                reference = ""
                if (
                    balance is not None
                    and balance.last_payment_tx
                    and balance.last_payment_at
                    and previous_ts is not None
                    and previous_ts < balance.last_payment_at <= ts
                ):
                    reference = balance.last_payment_tx
                rows.append(
                    {
                        "coin": wallet.coin_ticker.upper(),
                        "quantite": amount,
                        "date_reception": ts,
                        "valeur_eur": round(amount * price, 6) if price is not None else None,
                        "prix_source": source if price is not None else "inconnu",
                        "source": _source_label(wallet),
                        "reference": reference,
                    }
                )
            previous = float(paid)
            previous_ts = sample.ts
    rows.sort(key=lambda item: (item["date_reception"], item["coin"]))
    return {"rows": rows, "solo_rows": 0, "count": len(rows)}


def _source_label(wallet) -> str:
    pool = str(getattr(wallet, "pool", "") or "").lower()
    if pool in ("solo", "solo-pool", "solo_pool"):
        return "solo"
    return "pool"


def _fr(value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def csv_export(
    db: Session,
    start: str | None = None,
    end: str | None = None,
    coin: str | None = None,
) -> bytes:
    """CSV gains : ``;``, décimales françaises, UTF-8 BOM, note fiscale.

    Aucune adresse de wallet ni IP : la référence n'est qu'une tx de pool
    quand elle est connue.
    """
    result = gains_rows(db, start, end, coin)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow([f"# {EXPORT_NOTE}"])
    writer.writerow([f"# blocs solo : non historisés (solo_rows={result['solo_rows']})"])
    writer.writerow(EXPORT_HEADER)
    for row in result["rows"]:
        writer.writerow(
            [
                row["coin"],
                _fr(row["quantite"], 12),
                row["date_reception"],
                _fr(row["valeur_eur"], 6) if row["valeur_eur"] is not None else "",
                row["prix_source"],
                row["source"],
                row["reference"],
            ]
        )
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")
