"""API de l'état de santé (§4.19) : une ligne par machine, seuils réglables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app import health
from app.database import get_db
from app.dependencies import require_login
from app.models import Metric

router = APIRouter(prefix="/api/health", tags=["health"])


def _averages(db: Session) -> dict[str, dict[str, float]]:
    """Hashrate moyen sur 1 h, par machine et par device (clé ``__machine__``)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = (
        db.query(Metric.machine_uuid, Metric.ts, Metric.device_uuid, Metric.hashrate_hs)
        .filter(Metric.ts >= since, Metric.hashrate_hs.isnot(None))
        .all()
    )
    per_device: dict[str, dict[str, list[float]]] = {}
    per_machine: dict[str, dict[str, float]] = {}
    for machine_uuid, ts, device_uuid, hashrate in rows:
        bucket = per_device.setdefault(machine_uuid, {}).setdefault(device_uuid or "", [])
        bucket.append(float(hashrate))
        per_machine.setdefault(machine_uuid, {})[ts] = (
            per_machine.setdefault(machine_uuid, {}).get(ts, 0.0) + float(hashrate)
        )
    averages: dict[str, dict[str, float]] = {}
    for machine_uuid, devices in per_device.items():
        values = {key: sum(items) / len(items) for key, items in devices.items() if items}
        totals = list(per_machine.get(machine_uuid, {}).values())
        if totals:
            values["__machine__"] = sum(totals) / len(totals)
        averages[machine_uuid] = values
    return averages


@router.get("")
def health_status(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Pastille de chaque machine + totaux, pour le tableau de bord."""
    from app.routers.metrics import metrics_live

    machines = metrics_live(db=db, _user=True)
    limits = health.thresholds(db)
    averages = _averages(db)
    rows = []
    for machine in machines:
        state = health.evaluate(machine, averages.get(machine.get("uuid"), {}), limits)
        rows.append(
            {
                **state,
                "uuid": machine.get("uuid"),
                "name": machine.get("name"),
                "host": machine.get("host"),
                "online": bool(machine.get("online")),
                "collected": bool(machine.get("collected")),
                "total_hashrate_hs": machine.get("total_hashrate_hs"),
                "wall_power_w": machine.get("wall_power_w"),
                "devices": len(machine.get("devices") or []),
                # 2026.11.008 : un ASIC n'a aucun device — la carte affiche son
                # modèle, son firmware et, si le nominal est connu, son %.
                "kind": machine.get("kind") or "host",
                "model": (machine.get("asic") or {}).get("model") or "",
                "firmware": (machine.get("asic") or {}).get("firmware") or "",
                "nominal_pct": (machine.get("asic") or {}).get("nominal_pct"),
                "power_source": machine.get("power_source") or "",
            }
        )
    totals = {
        "machines": len(rows),
        "online": sum(1 for row in rows if row["online"]),
        "green": sum(1 for row in rows if row["status"] == "green"),
        "orange": sum(1 for row in rows if row["status"] == "orange"),
        "red": sum(1 for row in rows if row["status"] == "red"),
    }
    return {"machines": rows, "totals": totals, "thresholds": limits}


def _power_series(db: Session, now: datetime, hours: int, buckets: int) -> list[dict]:
    """Puissance DC du parc par tranche, depuis ``Metric.power_w``.

    La prise n'est pas historisée : la courbe somme les puissances *device*
    réellement collectées (DC), une valeur par machine et par tranche (le max
    de la tranche, pour ne pas compter deux collectes de la même machine).
    """
    since = now - timedelta(hours=hours)
    rows = (
        db.query(Metric.machine_uuid, Metric.ts, Metric.power_w)
        .filter(Metric.ts >= since.isoformat(), Metric.power_w.isnot(None))
        .all()
    )
    step = timedelta(hours=hours / max(1, buckets))
    per_bucket: list[dict[tuple[str, str], float]] = [dict() for _ in range(buckets)]
    for machine_uuid, ts, watts in rows:
        try:
            moment = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        index = int((moment - since).total_seconds() // step.total_seconds())
        if not 0 <= index < buckets:
            continue
        key = (machine_uuid, ts)
        per_bucket[index][key] = per_bucket[index].get(key, 0.0) + float(watts)
    series = []
    for index, sums in enumerate(per_bucket):
        per_machine: dict[str, float] = {}
        for (machine_uuid, _ts), watts in sums.items():
            per_machine[machine_uuid] = max(per_machine.get(machine_uuid, 0.0), watts)
        series.append({
            "t": (since + step * (index + 0.5)).isoformat(),
            "w": round(sum(per_machine.values()), 1) if per_machine else None,
        })
    return series


def _psu_level(load_pct: float, warn: float, crit: float) -> str:
    """Même code couleur que la santé : ok / orange / red (§4.2 bis)."""
    if load_pct > crit:
        return "red"
    if load_pct > warn:
        return "orange"
    return "ok"


@router.get("/power")
def power_status(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Suivi des alimentations du parc (§4.2 bis), pour Suivi → Alimentations.

    Une seule source : les ``wall_power_w`` de ``metrics_live``, ceux que sert
    aussi ``/api/health``.  Les lignes par alimentation viennent de
    ``evaluate_machine_power``, la fonction qui alimente déjà ces totaux.
    """
    from app import energy
    from app.models import Machine
    from app.routers.metrics import metrics_live
    from app.routers.psu import evaluate_machine_power

    machines = metrics_live(db=db, _user=True)
    limits = health.thresholds(db)
    warn = float(limits.get("psu_warn_pct", 80.0))
    crit = float(limits.get("psu_crit_pct", 90.0))
    now = datetime.now(timezone.utc)
    rows = []
    total_wall = 0.0
    total_dc = 0.0
    total_measured = 0.0
    total_estimated = 0.0
    worst_pct = 0.0
    worst_label = ""
    for entry in machines:
        wall = entry.get("wall_power_w")
        total_wall += float(wall or 0.0)
        machine = db.query(Machine).filter(Machine.uuid == entry.get("uuid")).first()
        detail = evaluate_machine_power(db, machine) if machine is not None else {}
        dc = float(detail.get("total_dc_w") or 0.0)
        total_dc += dc
        total_measured += float(detail.get("measured_w") or 0.0)
        total_estimated += float(detail.get("estimated_w") or 0.0)
        psus = []
        for psu in detail.get("psus") or []:
            pct = float(psu.get("load_pct") or 0.0)
            psus.append({
                "id": psu.get("id"),
                "name": psu.get("name") or "PSU",
                "rated_w": psu.get("rated_w"),
                "available_dc_w": psu.get("available_dc_w"),
                "load_dc_w": psu.get("load_dc_w"),
                "load_pct": pct,
                "efficiency_pct": psu.get("efficiency_pct"),
                "wall_w": psu.get("wall_w"),
                "status": _psu_level(pct, warn, crit),
            })
        pct = float(entry.get("worst_psu_load_pct") or detail.get("worst_load_pct") or 0.0)
        if pct > worst_pct:
            worst_pct = pct
            worst_label = " · ".join(
                part for part in (entry.get("name") or "",
                                  detail.get("worst_psu") or "") if part
            )
        rows.append({
            "uuid": entry.get("uuid"),
            "name": entry.get("name"),
            "kind": entry.get("kind") or "host",
            "wall_power_w": wall,
            "dc_power_w": round(dc, 1) if detail else None,
            "measured_w": detail.get("measured_w"),
            "estimated_w": detail.get("estimated_w"),
            "power_source": entry.get("power_source") or detail.get("power_source") or "",
            "worst_load_pct": pct,
            "worst_psu": detail.get("worst_psu") or "",
            "status": _psu_level(pct, warn, crit),
            "psus": psus,
        })

    tariff = energy.get_tariff(db)
    price = energy.price_info(now, tariff)
    kw = total_wall / 1000.0
    hourly = round(kw * float(price["price_eur_kwh"]), 4)
    prices = [energy.price_at(now + timedelta(hours=offset), tariff) for offset in range(24)]
    daily = round(kw * sum(prices), 4)
    distinct = sorted({round(float(value), 4) for value in prices})
    peak = None
    if len(distinct) > 1:
        peak = {
            "price_eur_kwh": distinct[-1],
            "hourly_eur": round(kw * distinct[-1], 4),
            "count_hours": sum(1 for value in prices if round(float(value), 4) == distinct[-1]),
        }
    return {
        "machines": rows,
        "total": {
            "wall_power_w": round(total_wall, 1),
            "dc_power_w": round(total_dc, 1),
            "measured_w": round(total_measured, 1),
            "estimated_w": round(total_estimated, 1),
            "worst_load_pct": round(worst_pct, 1),
            "worst_machine": worst_label,
        },
        "thresholds": {"psu_warn_pct": warn, "psu_crit_pct": crit},
        "tariff": {
            "label": energy.tariff_label(tariff),
            "mode": tariff.get("mode"),
            "price_eur_kwh": float(price["price_eur_kwh"]),
            "period": price.get("period") or "",
            "hourly_eur": hourly,
            "daily_eur": daily,
            "peak": peak,
        },
        "history": {"h24": _power_series(db, now, 24, 24), "d7": _power_series(db, now, 24 * 7, 28)},
    }


@router.get("/thresholds")
def get_thresholds(db: Session = Depends(get_db), _user=Depends(require_login)):
    return {"thresholds": health.thresholds(db), "defaults": health.DEFAULTS}


@router.put("/thresholds")
def put_thresholds(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    values = payload.get("thresholds", payload)
    try:
        limits = health.save_thresholds(db, values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "thresholds": limits}
