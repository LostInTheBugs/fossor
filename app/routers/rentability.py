"""Aide au choix du token par défaut (§4.19) — API.

* ``GET /api/tokens/rentability`` : classement des coins par device présent
  (modèle de GPU, modèle de CPU), source et date affichées.
* ``GET /api/tokens/default`` : réglage courant (par type et par device).
* ``POST /api/tokens/default`` : valide un choix — **aucun lancement**, aucune
  campagne, la simulation reste active.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import energy, rentability
from app.ai_config import get_setting, set_setting
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Device, Machine, MachineStatus

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

DEFAULT_TOKENS_SETTING = "default_token_by_model"
KINDS = ("gpu", "cpu")


class DefaultChoice(BaseModel):
    kind: str = Field(..., description="gpu ou cpu")
    model: str = ""
    ticker: str = ""
    scope: str = Field("type", description="type (tous les devices du modèle) ou device")
    device_uuid: str | None = None


def _catalogue(db: Session) -> dict[str, Any]:
    """Tickers connus : catalogue en base, puis coins intégrés."""
    known: dict[str, Any] = {}
    try:
        from app.models import Coin

        for coin in db.query(Coin).all():
            known[str(coin.ticker or "").upper()] = {"id": coin.id, "name": coin.name, "source": "catalog"}
    except Exception:  # pragma: no cover - catalogue absent
        pass
    try:
        from app.coins import BASE_COINS

        for entry in BASE_COINS:
            ticker = str(entry.get("ticker") or entry.get("tag") or "").upper()
            if ticker:
                known.setdefault(ticker, {"id": None, "name": entry.get("name"), "source": "builtin"})
    except Exception:  # pragma: no cover - table intégrée absente
        pass
    return known


def _json_status(db: Session, machine_uuid: str | None) -> dict[str, Any]:
    if not machine_uuid:
        return {}
    row = db.query(MachineStatus).filter(MachineStatus.machine_uuid == machine_uuid).first()
    if not row:
        return {}
    out: dict[str, Any] = {}
    for name in ("cpu_json", "asic_json"):
        raw = getattr(row, name, None)
        if raw:
            try:
                out[name] = json.loads(raw)
            except ValueError:
                out[name] = {}
    out["coin"] = row.coin or ""
    out["total_hashrate_hs"] = row.total_hashrate_hs
    return out


def _models(db: Session) -> list[dict[str, Any]]:
    """Modèles de device présents, avec leurs devices et l'état de santé."""
    devices = (
        db.query(Device)
        .filter(Device.present.is_(True), Device.kind.in_(("gpu", "cpu")))
        .all()
    )
    machines = {machine.id: machine for machine in db.query(Machine).all()}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for device in devices:
        model = (device.model or "").strip()
        if not model:
            continue
        key = (device.kind, model)
        slot = grouped.setdefault(
            key,
            {"kind": device.kind, "model": model, "devices": [], "machines": set(), "current_coin": ""},
        )
        slot["devices"].append(device)
        machine = machines.get(device.machine_id)
        if machine:
            slot["machines"].add(machine.uuid)
            status = _json_status(db, machine.uuid)
            if not slot["current_coin"] and status.get("coin"):
                slot["current_coin"] = status["coin"]
    return list(grouped.values())


def _measured_cpu(slot: dict[str, Any], db: Session) -> dict[str, dict[str, float]]:
    """Hashrate mesuré d'un modèle de CPU, par algorithme/coin."""
    measured: dict[str, dict[str, float]] = {}
    for machine_uuid in slot["machines"]:
        status = _json_status(db, machine_uuid)
        cpu = status.get("cpu_json") or {}
        for miner in cpu.get("miners") or []:
            hashrate = miner.get("hashrate_hs")
            if not hashrate:
                continue
            label = str(miner.get("coin") or miner.get("name") or "").upper() or "CPU"
            if label in ("WAM", "KSH"):
                # wam et KSH sont des algos/coins maison : le classement montre
                # le hashrate mesuré, sans prétendre à un autre algorithme.
                pass
            bucket = measured.setdefault(label, {"hashrate_hs": 0.0})
            bucket["hashrate_hs"] = max(bucket["hashrate_hs"], float(hashrate))
    return measured


@router.get("/rentability")
async def rentability_report(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Classement net/jour par modèle de device présent (§4.19)."""
    tariff = energy.get_tariff(db)
    price_eur_kwh = energy.price_at(datetime.now(timezone.utc), tariff)
    try:
        from app.prices import fetch_prices

        prices = await fetch_prices(["bitcoin"])
        btc_eur = (prices.get("bitcoin") or {}).get("eur")
    except Exception:  # pragma: no cover - prix indisponible
        btc_eur = None
    market = rentability.Market()
    sources: list[dict[str, Any]] = []
    devices_out: list[dict[str, Any]] = []
    coins: list[dict[str, Any]] = []
    coins_error = ""
    try:
        coins = await market.coins()
        sources.append(
            {
                "name": "WhatToMine",
                "url": rentability.WHATTOMINE_COINS_URL,
                "ok": True,
                "fetched_at": market.fetched_at("coins"),
                "detail": f"{len(coins)} coins lus",
            }
        )
    except Exception as exc:
        coins_error = f"WhatToMine indisponible : {exc}"
        sources.append(
            {
                "name": "WhatToMine",
                "url": rentability.WHATTOMINE_COINS_URL,
                "ok": False,
                "fetched_at": None,
                "detail": coins_error,
            }
        )
    sources.append(rentability.hashrate_no_status(db))

    by_tag = {coin["tag"]: coin for coin in coins if coin.get("tag")}
    for slot in _models(db):
        entry: dict[str, Any] = {
            "kind": slot["kind"],
            "model": slot["model"],
            "current_coin": slot["current_coin"],
            "tariff_eur_kwh": price_eur_kwh,
            "method": "whattomine",
            "rows": [],
            "error": "",
        }
        if slot["kind"] == "gpu":
            try:
                entry["rows"] = rentability.rank_rows(
                    await asyncio.wait_for(market.gpu_rows(slot["model"]), timeout=30),
                    coins,
                    btc_eur,
                    price_eur_kwh,
                    slot["current_coin"],
                )
                entry["source_date"] = market.fetched_at(f"gpu:{rentability.gpu_slug(slot['model'])}")
            except Exception as exc:
                entry["error"] = f"hashrates du modèle indisponibles : {exc}"
        else:
            entry["method"] = "mesure"
            measured = _measured_cpu(slot, db)
            from app.cpu_stats import estimate_cpu_w
            from app.stats_parser import format_hashrate

            rows = []
            for label, bucket in measured.items():
                coin = by_tag.get(label.upper())
                hashrate = bucket["hashrate_hs"]
                base = {
                    "hashrate_hs": hashrate,
                    "hashrate_label": format_hashrate(hashrate),
                }
                if coin:
                    rows.append({"coin_id": coin.get("id"), "tag": coin["tag"], "algo": coin.get("algo"), **base})
                else:
                    rows.append({"coin_id": None, "tag": label, "algo": "", **base})
            power, _power_source = estimate_cpu_w(slot["model"])
            for device in slot["devices"]:
                machine = db.query(Machine).filter(Machine.id == device.machine_id).first()
                if machine and machine.cpu_power_w:
                    power = machine.cpu_power_w
                    break
            entry["rows"] = rentability.rank_rows(rows, coins, btc_eur, price_eur_kwh, slot["current_coin"], power)
            entry["source_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if not rows:
                entry["error"] = "aucun hashrate CPU mesuré : lancez un mineur CPU pour obtenir une référence"
        entry["proposal"] = rentability.proposal(entry["rows"]) if entry["rows"] else None
        devices_out.append(entry)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "btc_eur": btc_eur,
        "tariff_eur_kwh": price_eur_kwh,
        "tariff_label": energy.tariff_label(tariff),
        "coins_error": coins_error,
        "sources": sources,
        "devices": devices_out,
    }


@router.get("/default")
def get_default(db: Session = Depends(get_db), _user=Depends(require_login)):
    stored = get_setting(db, DEFAULT_TOKENS_SETTING, {}) or {}
    if not isinstance(stored, dict):
        stored = {}
    devices = []
    for device in db.query(Device).filter(Device.present.is_(True)).all():
        devices.append(
            {
                "uuid": device.uuid,
                "kind": device.kind,
                "model": device.model,
                "default_token_id": getattr(device, "default_token_id", None),
            }
        )
    return {"by_model": stored, "devices": devices}


@router.post("/default")
def set_default(choice: DefaultChoice, db: Session = Depends(get_db), user=Depends(require_login)):
    """Enregistre un token par défaut — sans lancer quoi que ce soit."""
    kind = (choice.kind or "").strip().lower()
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Type de device inconnu (gpu ou cpu)")
    ticker = (choice.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker manquant")
    catalogue = _catalogue(db)
    if catalogue and ticker not in catalogue:
        raise HTTPException(status_code=400, detail=f"Coin {ticker} inconnu du catalogue")
    scope = (choice.scope or "type").strip().lower()
    if scope not in ("type", "device"):
        raise HTTPException(status_code=400, detail="Portée inconnue (type ou device)")
    detail: dict[str, Any] = {"ticker": ticker, "kind": kind, "scope": scope}
    if scope == "device":
        if not choice.device_uuid:
            raise HTTPException(status_code=400, detail="device_uuid requis pour la portée device")
        device = db.query(Device).filter(Device.uuid == choice.device_uuid).first()
        if not device:
            raise HTTPException(status_code=404, detail="Device inconnu")
        entry = catalogue.get(ticker) or {}
        if not entry.get("id"):
            raise HTTPException(
                status_code=400,
                detail=f"Coin {ticker} absent du catalogue : ajoutez-le avant de l'affecter à un device",
            )
        device.default_token_id = int(entry["id"])
        detail["device_uuid"] = device.uuid
    else:
        model = (choice.model or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="Modèle requis pour la portée type")
        stored = get_setting(db, DEFAULT_TOKENS_SETTING, {}) or {}
        if not isinstance(stored, dict):
            stored = {}
        stored[f"{kind}:{model}"] = ticker
        set_setting(db, DEFAULT_TOKENS_SETTING, stored)
        detail["model"] = model
    db.add(
        AuditLog(
            actor=user,
            action="default-token",
            object_ref=f"{kind}:{detail.get('model') or detail.get('device_uuid') or ''}"[:255],
            result="ok",
            output=json.dumps(detail, ensure_ascii=False)[:500],
        )
    )
    db.commit()
    # §4.19 : le choix n'arme rien — il sera lu au prochain retour au défaut.
    detail["launched"] = False
    return {"ok": True, "choice": detail, "simulation": get_setting(db, "simulation_mode", True)}
