"""Électricité et rentabilité réelle (§4.15).

Three tariff modes, all in Europe/Paris:

* ``fixed``  — one price per kWh (default 0,20 €/kWh);
* ``ranges`` — time ranges (heures creuses / pleines); a range may cross
  midnight (``22:00`` → ``06:00``) and the whole day must be covered once;
* ``tempo``  — coloured days (bleu / blanc / rouge) × HC / HP. The colour of the
  day is typed in by the operator; ``unknown`` is priced as blue.

Energy is integrated from the ``metrics`` rows (one per device per round):
trapezoid between consecutive points, and a gap longer than five minutes is
skipped instead of being filled with a made-up value. The CPU has no power
sensor, so its wattage is typed in per machine; the platform (motherboard,
PSU losses) is a second per-machine constant (default 50 W).

Revenue starts simple (§4.15): the 24 h delta of the pool balance of the coin's
wallets, times the coin price. It is split between the machines mining that coin
by their latest hashrate share.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

try:
    PARIS = ZoneInfo("Europe/Paris")
except ZoneInfoNotFoundError:  # minimal image without tzdata: never crash at import
    PARIS = timezone(timedelta(hours=1))
MAX_GAP_S = 300.0
TARIFF_KEY = "energy_tariff"
PERIODS = {"1h": 1, "24h": 24, "7d": 24 * 7}
DEFAULT_PLATFORM_W = 50.0
TEMPO_COLORS = ("blue", "white", "red")

DEFAULT_TARIFF: dict[str, Any] = {
    "mode": "fixed",
    "timezone": "Europe/Paris",
    "fixed_price": 0.20,
    "ranges": [{"start": "00:00", "end": "24:00", "price": 0.20, "label": "base"}],
    "tempo": {
        "day": "unknown",
        # EDF Tempo grid (editable) — unknown day is priced as blue.
        "blue": {"hc": 0.1296, "hp": 0.1609},
        "white": {"hc": 0.1486, "hp": 0.1894},
        "red": {"hc": 0.1568, "hp": 0.7562},
    },
}


# ── Tariff validation ────────────────────────────────────────────────
def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} : nombre attendu")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} : nombre attendu")
    if number < 0:
        raise ValueError(f"{field} : valeur négative refusée")
    return number


def _hhmm(value: Any, field: str) -> int:
    if not isinstance(value, str) or ":" not in value:
        raise ValueError(f"{field} : format HH:MM attendu")
    hours, _, minutes = value.partition(":")
    try:
        h, m = int(hours), int(minutes)
    except ValueError:
        raise ValueError(f"{field} : format HH:MM attendu")
    if h == 24 and m == 0:
        return 1440
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"{field} : heure invalide")
    return h * 60 + m


def _range_minutes(start: int, end: int) -> list[int]:
    if start == end:
        return []
    if start < end:
        return list(range(start, end))
    return list(range(start, 1440)) + list(range(0, end))


def normalize_tariff(raw: Any) -> dict[str, Any]:
    """Validate a tariff; raise ``ValueError`` with a French message."""
    if not isinstance(raw, dict):
        raise ValueError("tarif : objet attendu")
    mode = str(raw.get("mode") or "fixed").strip().lower()
    if mode not in ("fixed", "ranges", "tempo"):
        raise ValueError("mode de tarif inconnu : fixed, ranges ou tempo attendu")

    out: dict[str, Any] = {"mode": mode, "timezone": "Europe/Paris"}

    if mode == "fixed":
        out["fixed_price"] = _number(raw.get("fixed_price", 0.20), "prix fixe")
        return out

    if mode == "ranges":
        ranges = raw.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            raise ValueError("plages : au moins une plage est requise")
        coverage = [0] * 1440
        clean: list[dict[str, Any]] = []
        for index, item in enumerate(ranges):
            if not isinstance(item, dict):
                raise ValueError(f"plage {index + 1} : objet attendu")
            start = _hhmm(item.get("start"), f"plage {index + 1} début")
            end = _hhmm(item.get("end"), f"plage {index + 1} fin")
            price = _number(item.get("price"), f"plage {index + 1} prix")
            minutes = _range_minutes(start, end)
            if not minutes:
                raise ValueError(f"plage {index + 1} : début et fin identiques")
            for minute in minutes:
                coverage[minute] += 1
            clean.append({
                "start": item.get("start"),
                "end": item.get("end"),
                "price": price,
                "label": str(item.get("label") or ""),
            })
        if any(count != 1 for count in coverage):
            raise ValueError(
                "les plages doivent couvrir 24 h sans chevauchement "
                "(chaque minute exactement une fois)"
            )
        out["ranges"] = clean
        return out

    tempo_raw = raw.get("tempo")
    tempo: dict[str, Any] = tempo_raw if isinstance(tempo_raw, dict) else {}
    day = str(tempo.get("day") or "unknown").strip().lower()
    if day not in ("unknown", *TEMPO_COLORS):
        raise ValueError("jour coloré inconnu : unknown, blue, white ou red attendu")
    clean_tempo: dict[str, Any] = {"day": day}
    defaults = DEFAULT_TARIFF["tempo"]
    for color in TEMPO_COLORS:
        raw_color = tempo.get(color)
        values: dict[str, Any] = raw_color if isinstance(raw_color, dict) else {}
        fallback = defaults[color]
        clean_tempo[color] = {
            "hc": _number(values.get("hc", fallback["hc"]), f"tempo {color} HC"),
            "hp": _number(values.get("hp", fallback["hp"]), f"tempo {color} HP"),
        }
    out["tempo"] = clean_tempo
    return out


def default_tariff() -> dict[str, Any]:
    """Deep-ish copy of the default tariff (safe to hand to the API)."""
    return normalize_tariff(DEFAULT_TARIFF)


def get_tariff(db: Session) -> dict[str, Any]:
    """Stored tariff, normalised; a corrupt row falls back to the default."""
    from app.ai_config import get_setting

    raw = get_setting(db, TARIFF_KEY, None)
    if raw is None:
        return default_tariff()
    try:
        return normalize_tariff(raw)
    except ValueError:
        return default_tariff()


def set_tariff(db: Session, raw: Any) -> dict[str, Any]:
    from app.ai_config import set_setting

    tariff = normalize_tariff(raw)
    set_setting(db, TARIFF_KEY, tariff)
    db.commit()
    return tariff


# ── Price of an instant ──────────────────────────────────────────────
def _local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(PARIS)


def price_at(moment: datetime, tariff: dict[str, Any]) -> float:
    """€/kWh at *moment* (UTC or aware), in Europe/Paris local time."""
    local = _local(moment)
    minute = local.hour * 60 + local.minute
    mode = tariff.get("mode", "fixed")

    if mode == "fixed":
        return float(tariff.get("fixed_price", 0.20))

    if mode == "ranges":
        for item in tariff.get("ranges") or []:
            start = _hhmm(item.get("start"), "plage début")
            end = _hhmm(item.get("end"), "plage fin")
            if start < end:
                hit = start <= minute < end
            else:
                hit = minute >= start or minute < end
            if hit:
                return float(item.get("price", 0.0))
        return 0.0

    tempo = tariff.get("tempo") or {}
    day = str(tempo.get("day") or "unknown")
    if day not in TEMPO_COLORS:
        day = "blue"
    peak = 6 * 60 <= minute < 22 * 60
    grid = tempo.get(day) or {}
    return float(grid.get("hp" if peak else "hc", 0.0))


def tariff_label(tariff: dict[str, Any]) -> str:
    mode = tariff.get("mode")
    if mode == "fixed":
        return f"fixe {float(tariff.get('fixed_price', 0.0)):.4f} €/kWh"
    if mode == "ranges":
        parts = [
            f"{r.get('label') or (str(r.get('start')) + '-' + str(r.get('end')))} "
            f"{float(r.get('price', 0.0)):.4f}"
            for r in tariff.get("ranges") or []
        ]
        return "plages : " + " / ".join(parts)
    day = str((tariff.get("tempo") or {}).get("day") or "unknown")
    return f"tempo jour {day} (HC/HP, Europe/Paris)"


# ── Integration ──────────────────────────────────────────────────────
def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def integrate_energy(
    points: list[tuple[datetime, float | None]],
    tariff: dict[str, Any],
    max_gap_s: float = MAX_GAP_S,
) -> dict[str, Any]:
    """Integrate a power series: kWh and €, skipping gaps longer than *max_gap_s*."""
    clean = sorted(
        ((moment, watts) for moment, watts in points if moment is not None),
        key=lambda item: item[0],
    )
    kwh = 0.0
    cost = 0.0
    covered_s = 0.0
    segments = 0

    for (start, start_w), (end, end_w) in zip(clean, clean[1:]):
        gap = (end - start).total_seconds()
        if gap <= 0 or gap > max_gap_s:
            continue
        if start_w is None or end_w is None:
            continue  # a missing reading breaks the series, it is not filled
        watts = (start_w + end_w) / 2
        energy = max(0.0, watts) / 1000.0 * gap / 3600.0
        middle = start + timedelta(seconds=gap / 2)
        kwh += energy
        cost += energy * price_at(middle, tariff)
        covered_s += gap
        segments += 1

    return {
        "kwh": round(kwh, 9),
        "cost_eur": round(cost, 9),
        "covered_s": round(covered_s, 3),
        "segments": segments,
    }


def constant_energy(
    watts: float | None,
    start: datetime,
    end: datetime,
    tariff: dict[str, Any],
) -> dict[str, Any]:
    """Energy of a constant load between two instants, priced minute by minute."""
    if watts is None or watts <= 0 or end <= start:
        return {"kwh": 0.0, "cost_eur": 0.0, "covered_s": 0.0, "segments": 0}

    kwh = 0.0
    cost = 0.0
    cursor = start
    while cursor < end:
        step_end = min(cursor + timedelta(minutes=1), end)
        hours = (step_end - cursor).total_seconds() / 3600.0
        energy = watts / 1000.0 * hours
        kwh += energy
        cost += energy * price_at(cursor + (step_end - cursor) / 2, tariff)
        cursor = step_end

    return {
        "kwh": round(kwh, 9),
        "cost_eur": round(cost, 9),
        "covered_s": round((end - start).total_seconds(), 3),
        "segments": 1,
    }


# ── Report ───────────────────────────────────────────────────────────
def _period_hours(period: str) -> int:
    key = (period or "24h").strip().lower()
    if key not in PERIODS:
        raise ValueError("période inconnue : 1h, 24h ou 7d attendu")
    return PERIODS[key]


def _sum(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kwh": round(sum(r["kwh"] for r in results), 9),
        "cost_eur": round(sum(r["cost_eur"] for r in results), 9),
        "covered_s": round(sum(r["covered_s"] for r in results), 3),
    }


def _pool_delta(db: Session, ticker: str, now: datetime) -> dict[str, Any]:
    """24 h change of the summed pool balance of every wallet of *ticker*."""
    from app.models import PoolBalanceSample, Wallet

    wallet_ids = [
        wallet.id for wallet in db.query(Wallet).filter(Wallet.coin_ticker == ticker).all()
    ]
    if not wallet_ids:
        return {"delta": None, "span_h": None, "wallets": 0, "note": "aucun wallet"}

    since = (now - timedelta(hours=24)).isoformat()
    latest_rows = {}
    oldest_rows = {}
    for wallet_id in wallet_ids:
        latest = (
            db.query(PoolBalanceSample)
            .filter(PoolBalanceSample.wallet_id == wallet_id, PoolBalanceSample.ts <= now.isoformat())
            .order_by(PoolBalanceSample.ts.desc())
            .first()
        )
        oldest = (
            db.query(PoolBalanceSample)
            .filter(PoolBalanceSample.wallet_id == wallet_id, PoolBalanceSample.ts <= since)
            .order_by(PoolBalanceSample.ts.desc())
            .first()
        )
        if latest is not None and oldest is not None and latest.total is not None and oldest.total is not None:
            latest_rows[wallet_id] = latest
            oldest_rows[wallet_id] = oldest

    if not latest_rows:
        return {"delta": None, "span_h": None, "wallets": len(wallet_ids),
                "note": "pas encore d'historique de solde sur 24 h"}

    newest_candidates = [m for m in (_ts(row.ts) for row in latest_rows.values()) if m]
    previous_candidates = [m for m in (_ts(row.ts) for row in oldest_rows.values()) if m]
    newest = max(newest_candidates)
    previous = min(previous_candidates)
    span_h = (newest - previous).total_seconds() / 3600.0 if newest and previous else None
    if span_h is None or span_h < 12:
        return {"delta": None, "span_h": round(span_h, 2) if span_h else None,
                "wallets": len(latest_rows), "note": "historique de solde trop court"}

    delta = sum(
        (latest_rows[wid].total or 0.0) - (oldest_rows[wid].total or 0.0)
        for wid in latest_rows
    )
    return {
        "delta": round(delta, 12),
        "span_h": round(span_h, 2),
        "wallets": len(latest_rows),
        "note": "",
    }


def build_report(
    db: Session,
    period: str = "24h",
    quotes: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cost (period), 24 h cost, revenue/day and net/day for every machine."""
    from app.models import Coin, Device, Machine, MachineStatus, Metric

    hours = _period_hours(period)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start = now - timedelta(hours=hours)
    day_start = now - timedelta(hours=24)
    tariff = get_tariff(db)
    quotes = quotes or {}

    statuses = {row.machine_uuid: row for row in db.query(MachineStatus).all()}
    coins = {row.ticker: row for row in db.query(Coin).all()}
    machines = db.query(Machine).order_by(Machine.name.asc()).all()

    # Machine rows + their device labels.
    devices_by_machine: dict[int, dict[str, Device]] = {}
    for device in db.query(Device).all():
        devices_by_machine.setdefault(device.machine_id, {})[device.uuid] = device

    report_machines: list[dict[str, Any]] = []
    warnings: list[str] = []
    coin_machines: dict[str, list[dict[str, Any]]] = {}

    for machine in machines:
        status = statuses.get(machine.uuid or "")
        collected = bool(machine.enabled) and str(machine.usage) != "none"
        coin = (status.coin if status else "") or ""
        # §4.2 / §4.2 bis : le coût se calcule sur la puissance *à la prise*,
        # charge DC / rendement interpolé des alimentations configurées.
        try:
            from app.routers.psu import evaluate_machine_power

            wall_factor = float(evaluate_machine_power(db, machine).get("wall_factor") or 1.0)
        except Exception:
            wall_factor = 1.0

        rows = (
            db.query(Metric)
            .filter(
                Metric.machine_uuid == machine.uuid,
                Metric.ts >= start.isoformat(),
                Metric.ts <= now.isoformat(),
            )
            .order_by(Metric.ts.asc())
            .all()
        )
        by_device: dict[str | None, list[tuple[datetime, float | None]]] = {}
        for row in rows:
            moment = _ts(row.ts)
            if moment is None:
                continue
            watts = row.power_w
            if watts is not None and wall_factor != 1.0:
                watts = round(float(watts) * wall_factor, 2)
            by_device.setdefault(row.device_uuid, []).append((moment, watts))

        all_moments = [moment for series in by_device.values() for moment, _ in series]
        window_start = min(all_moments) if all_moments else None
        window_end = max(all_moments) if all_moments else None

        device_reports: list[dict[str, Any]] = []
        device_results: list[dict[str, Any]] = []
        day_device_results: list[dict[str, Any]] = []

        for device_uuid, series in by_device.items():
            device = devices_by_machine.get(machine.id, {}).get(device_uuid or "")
            kind = device.kind if device else ("gpu" if device_uuid is None else "gpu")
            model = (device.model if device else "") or (
                "GPU non apparié" if device_uuid is None else "Device")
            if kind == "cpu" and machine.cpu_power_w and not any(w is not None for _, w in series):
                window = (window_start, window_end)
                result = constant_energy(
                    machine.cpu_power_w * wall_factor, window[0], window[1], tariff
                ) if window[0] and window[1] else _sum([])
                day_result = constant_energy(
                    machine.cpu_power_w * wall_factor,
                    max(window_start or day_start, day_start), window_end or now, tariff
                ) if window_start and window_end else _sum([])
                source = "saisie"
            else:
                result = integrate_energy(series, tariff)
                day_series = [(m, w) for m, w in series if m >= day_start]
                day_result = integrate_energy(day_series, tariff)
                source = "mesure"
            device_results.append(result)
            day_device_results.append(day_result)
            powers = [w for _, w in series if w is not None]
            device_reports.append({
                "uuid": device_uuid,
                "kind": kind,
                "model": model,
                "kwh": result["kwh"],
                "cost_eur": result["cost_eur"],
                "power_w": round(sum(powers) / len(powers), 2) if powers else None,
                "source": source,
            })

        # CPU load: a machine-level wattage is used when no CPU power was measured.
        cpu_present = any(row["kind"] == "cpu" for row in device_reports)
        if machine.cpu_power_w and not cpu_present and window_start and window_end:
            cpu = constant_energy(machine.cpu_power_w, window_start, window_end, tariff)
            cpu_day = constant_energy(
                machine.cpu_power_w, max(window_start, day_start), window_end, tariff
            )
            device_results.append(cpu)
            day_device_results.append(cpu_day)
            device_reports.append({
                "uuid": None,
                "kind": "cpu",
                "model": "CPU (puissance saisie)",
                "kwh": cpu["kwh"],
                "cost_eur": cpu["cost_eur"],
                "power_w": machine.cpu_power_w,
                "source": "saisie",
            })

        # Platform load: constant over the collected window.
        if window_start and window_end and machine.platform_power_w:
            platform = constant_energy(machine.platform_power_w, window_start, window_end, tariff)
            platform_day = constant_energy(
                machine.platform_power_w, max(window_start, day_start), window_end, tariff
            )
            device_results.append(platform)
            day_device_results.append(platform_day)
            device_reports.append({
                "uuid": None,
                "kind": "platform",
                "model": "Plateforme (carte mère, alim)",
                "kwh": platform["kwh"],
                "cost_eur": platform["cost_eur"],
                "power_w": machine.platform_power_w,
                "source": "saisie",
            })

        total = _sum(device_results)
        day_total = _sum(day_device_results)
        # Puissance moyenne de la machine, tous postes confondus : elle sert au
        # prorata de l'électricité entre les mineurs d'un même coin (§4.15).
        avg_power_w = round(
            sum(float(d["power_w"]) for d in device_reports if d.get("power_w")), 2
        ) or None
        entry = {
            "uuid": machine.uuid,
            "name": machine.name,
            "enabled": bool(machine.enabled),
            "collected": collected,
            "online": bool(status.online) if status else False,
            "coin": coin,
            "cpu_power_w": machine.cpu_power_w,
            "platform_power_w": machine.platform_power_w,
            "kwh": total["kwh"],
            "cost_eur": total["cost_eur"],
            "cost_eur_day": day_total["cost_eur"],
            # €/mois affiché à côté du €/jour (jour × 30), même base à la prise.
            "cost_eur_month": round(day_total["cost_eur"] * 30, 9),
            "avg_power_w": avg_power_w,
            "covered_s": total["covered_s"],
            "revenue_eur_day": None,
            "net_eur_day": None,
            "devices": device_reports,
        }
        report_machines.append(entry)
        if coin and collected:
            coin_machines.setdefault(coin, []).append(entry)

    # Revenue per coin, split by latest hashrate share; l'électricité du coin est
    # répartie au prorata de la puissance de chaque machine, et le coût net par
    # coin = revenu estimé − cette part (§4.15).
    coin_report: list[dict[str, Any]] = []
    for ticker, entries in coin_machines.items():
        electricity = round(sum(float(e["cost_eur_day"] or 0.0) for e in entries), 9)
        power_total = sum(float(e.get("avg_power_w") or 0.0) for e in entries)
        for entry in entries:
            share_power = (
                float(entry.get("avg_power_w") or 0.0) / power_total
                if power_total > 0 else 1.0 / len(entries)
            )
            entry["electricity_share_eur_day"] = round(electricity * share_power, 9)
        coin_entry = {
            "ticker": ticker,
            "machines": len(entries),
            "electricity_eur_day": electricity,
            "revenue_eur_day": None,
            "net_eur_day": None,
            "note": "",
        }
        quote = quotes.get(ticker) or {}
        price = quote.get("eur")
        if price is None:
            coin = coins.get(ticker)
            price = coin.manual_price_eur if coin and coin.manual_price_eur is not None else None
        if price is None:
            coin_entry["note"] = "prix du coin inconnu"
            coin_report.append(coin_entry)
            for entry in entries:
                entry["revenue_note"] = "prix du coin inconnu"
            continue

        delta = _pool_delta(db, ticker, now)
        if delta["delta"] is None:
            coin_entry["note"] = delta["note"]
            coin_report.append(coin_entry)
            for entry in entries:
                entry["revenue_note"] = delta["note"]
            continue

        hashrates = []
        for entry in entries:
            status = statuses.get(entry["uuid"])
            hashrates.append(float(status.total_hashrate_hs or 0.0) if status else 0.0)
        total_hash = sum(hashrates)
        shares = (
            [value / total_hash for value in hashrates]
            if total_hash > 0 else [1.0 / len(entries)] * len(entries)
        )
        revenue = delta["delta"] * float(price)
        for entry, share in zip(entries, shares):
            entry["revenue_eur_day"] = round(revenue * share, 9)
            entry["net_eur_day"] = round((revenue * share) - entry["cost_eur_day"], 9)
            entry["hashrate_share"] = round(share, 6)
            entry["balance_delta_24h"] = round(delta["delta"] * share, 12)
            entry["price_eur"] = float(price)
            entry["revenue_note"] = ""
        coin_entry["revenue_eur_day"] = round(revenue, 9)
        coin_entry["net_eur_day"] = round(revenue - electricity, 9)
        coin_entry["price_eur"] = float(price)
        coin_report.append(coin_entry)

    for entry in report_machines:
        if entry["revenue_eur_day"] is None and not entry.get("revenue_note"):
            entry["revenue_note"] = "aucune donnée de pool"

    total_kwh = round(sum(m["kwh"] for m in report_machines), 9)
    total_cost = round(sum(m["cost_eur"] for m in report_machines), 9)
    total_cost_day = round(sum(m["cost_eur_day"] for m in report_machines), 9)
    priced = [m for m in report_machines if m["revenue_eur_day"] is not None]
    total_revenue = round(sum(m["revenue_eur_day"] for m in priced), 9) if priced else None

    # §4.19 : un tiret muet n'apprend rien à l'exploitant — la raison voyage
    # avec la valeur, le tableau de bord l'affiche sous la tuile.
    from app.ai_config import get_setting

    if not report_machines:
        cost_note = "aucune machine suivie"
    elif get_setting(db, TARIFF_KEY, None) is None:
        cost_note = "tarif non configuré (défaut 0,20 €/kWh)"
    elif total_kwh <= 0:
        cost_note = "aucune mesure de puissance"
    else:
        cost_note = ""
    if total_revenue is not None:
        revenue_note = ""
    else:
        notes = [c.get("note") for c in coin_report if c.get("note")]
        revenue_note = str(notes[0]) if notes else "prix inconnu"

    return {
        "as_of": now.isoformat(),
        "period": (period or "24h").lower(),
        "hours": hours,
        "timezone": "Europe/Paris",
        "tariff": {**tariff, "label": tariff_label(tariff)},
        "total": {
            "kwh": total_kwh,
            "cost_eur": total_cost,
            "cost_eur_day": total_cost_day,
            "cost_eur_month": round(total_cost_day * 30, 9),
            "revenue_eur_day": total_revenue,
            "revenue_note": revenue_note,
            "cost_note": cost_note,
            "net_eur_day": round(total_revenue - total_cost_day, 9) if total_revenue is not None else None,
            "machines": len(report_machines),
            "priced_machines": len(priced),
        },
        "coins": coin_report,
        "machines": report_machines,
        "warnings": warnings,
    }
