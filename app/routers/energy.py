"""Électricité (§4.15) — tarif, puissances saisies et rapport coût / net."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import energy, energy_rules as ER, prices, rule_runner as RR
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Coin, EnergyRuleEvent, Machine

router = APIRouter(prefix="/api/energy", tags=["energy"])


async def _quotes(db: Session) -> dict[str, dict[str, Any]]:
    """EUR price per ticker — CoinGecko cache (verified ids only), else manual."""
    coins = db.query(Coin).all()
    ids = [c.coingecko_id for c in coins if c.coingecko_id and c.coingecko_verified]
    fetched = await prices.fetch_prices(ids) if ids else {}
    out: dict[str, dict[str, Any]] = {}
    for coin in coins:
        quote = fetched.get(coin.coingecko_id or "", {}) if coin.coingecko_verified else {}
        eur = quote.get("eur")
        if eur is None and coin.manual_price_eur is not None:
            eur = coin.manual_price_eur
        out[coin.ticker] = {"eur": eur}
    return out


def _machines_power(db: Session) -> list[dict[str, Any]]:
    return [
        {
            "uuid": machine.uuid,
            "name": machine.name,
            "cpu_power_w": machine.cpu_power_w,
            "platform_power_w": machine.platform_power_w,
        }
        for machine in db.query(Machine).order_by(Machine.name.asc()).all()
    ]


@router.get("")
async def energy_report(
    period: str = Query("24h", description="1h, 24h ou 7d"),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Energy, cost, revenue/day and net/day per machine and device."""
    try:
        return energy.build_report(db, period=period, quotes=await _quotes(db))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/tariff")
def get_energy_settings(db: Session = Depends(get_db), _user=Depends(require_login)):
    return {
        "tariff": energy.get_tariff(db),
        "default": energy.default_tariff(),
        "current": energy.current_price(db),
        "rule": ER.get_rule(db),
        "timezone": "Europe/Paris",
        "machines": _machines_power(db),
    }


@router.put("/tariff")
def put_energy_settings(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Save the tariff and the per-machine CPU / platform wattages."""
    raw = payload.get("tariff")
    machines = payload.get("machines")
    tariff = None
    if raw is not None:
        try:
            tariff = energy.normalize_tariff(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    elif machines is None:
        raise HTTPException(status_code=400, detail="tariff ou machines attendu")

    if machines is not None:
        if not isinstance(machines, list):
            raise HTTPException(status_code=400, detail="machines : liste attendue")
        for item in machines:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="machines : objet attendu")
            machine = (
                db.query(Machine).filter(Machine.uuid == item.get("uuid")).first()
            )
            if machine is None:
                continue
            for field in ("cpu_power_w", "platform_power_w"):
                if field not in item:
                    continue
                value = item[field]
                if value in (None, ""):
                    machine.cpu_power_w = None if field == "cpu_power_w" else machine.platform_power_w
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{field} : nombre attendu")
                if number < 0:
                    raise HTTPException(status_code=400, detail=f"{field} : valeur négative refusée")
                setattr(machine, field, number)

    if tariff is not None:
        energy.set_tariff(db, tariff)
    db.add(AuditLog(
        actor="fred",
        action="energy_settings",
        object_ref="energy",
        result="ok",
        output=f"mode={tariff['mode'] if tariff else 'inchangé'}",
    ))
    db.commit()
    return {"ok": True, "tariff": energy.get_tariff(db), "machines": _machines_power(db)}


# ── Couleur Tempo automatique (source publique + cache) ──────────────
@router.post("/tariff/tempo-day")
async def fetch_tempo_day(
    payload: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Récupère la couleur du jour, la met en cache ; échec = repli manuel."""
    from datetime import date

    payload = payload or {}
    requested = payload.get("date")
    day = None
    if requested:
        try:
            day = date.fromisoformat(str(requested))
        except ValueError:
            raise HTTPException(status_code=400, detail="date : format AAAA-MM-JJ attendu")
    result = await energy.fetch_tempo_colour(day)
    if not result["ok"]:
        raise HTTPException(
            status_code=502,
            detail=(
                f"couleur Tempo indisponible ({result['error']}) — "
                "la couleur saisie à la main reste utilisée"
            ),
        )
    try:
        tariff = energy.record_tempo_day(db, result["date"], result["day"], result["source"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditLog(
        actor=str(user), action="energy_tempo_day", object_ref="energy", result="ok",
        output=f"{result['date']} = {result['day']} ({result['source']})",
    ))
    db.commit()
    return {
        "ok": True, "auto": tariff["tempo"]["auto"],
        "current": energy.current_price(db),
    }


# ── Règle « heures chères » (§4.15) ──────────────────────────────────
@router.get("/rule")
def read_rule(db: Session = Depends(get_db), _user=Depends(require_login)):
    events = (
        db.query(EnergyRuleEvent)
        .order_by(EnergyRuleEvent.id.desc())
        .limit(20)
        .all()
    )
    return {
        "rule": ER.get_rule(db),
        "modes": [{"value": mode, "label": ER.MODE_LABELS[mode]} for mode in ER.MODES],
        "current": energy.current_price(db),
        "events": [
            {
                "id": row.id, "ts": row.ts, "kind": row.kind,
                "machine_id": row.machine_id, "device_uuid": row.device_uuid,
                "detail": row.detail, "simulated": bool(row.simulated),
            }
            for row in events
        ],
    }


@router.put("/rule")
def put_rule(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    raw = payload.get("rule", payload)
    try:
        rule = ER.set_rule(db, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditLog(
        actor=str(user), action="energy_rule", object_ref="energy", result="ok",
        output=f"activée={rule['enabled']} mode={rule['mode']} simulation={rule['simulation']}",
    ))
    db.commit()
    return {"ok": True, "rule": rule}


@router.post("/rule/evaluate")
async def evaluate_rule(
    payload: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Évalue la règle maintenant (horloge simulable) et exécute ou simule."""
    from datetime import datetime

    payload = payload or {}
    now = None
    if payload.get("now"):
        try:
            now = datetime.fromisoformat(str(payload["now"]))
        except ValueError:
            raise HTTPException(status_code=400, detail="now : format ISO attendu")
    simulate = payload.get("simulate")
    result = await RR.evaluate(
        db, now=now, simulate=None if simulate is None else bool(simulate), actor=str(user)
    )
    decision = result["decision"]
    db.add(AuditLog(
        actor=str(user), action="energy_rule_evaluate", object_ref="energy",
        result="simulated" if decision["simulated"] else "ok",
        output=(f"active={decision['active']} {decision['reason']} "
                f"actions={decision['counts']}")[:500],
    ))
    db.commit()
    return result


@router.get("/rule/events")
def rule_events(
    limit: int = 50,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    rows = (
        db.query(EnergyRuleEvent)
        .order_by(EnergyRuleEvent.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [
        {
            "id": row.id, "ts": row.ts, "kind": row.kind,
            "machine_id": row.machine_id, "device_uuid": row.device_uuid,
            "detail": row.detail, "simulated": bool(row.simulated),
        }
        for row in rows
    ]
