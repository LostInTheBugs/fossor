"""Électricité (§4.15) — tarif, puissances saisies et rapport coût / net."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import energy, prices
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Coin, Machine

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
