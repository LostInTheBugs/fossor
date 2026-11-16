"""Tokens minés (§6 écran 3) — prices, pool balances, campaigns, legacy import.

* ``GET /api/mined`` reads the database only (no network): campaigns, history,
  prices from the 5 min cache and the last pool balance of every wallet.
* ``POST /api/mined/refresh`` fetches CoinGecko prices and the pool balances,
  then stores them — errors are reported, never raised, never zero-filled.
* ``POST /api/import/legacy`` imports the two v2.21 files, ``dry_run`` first.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import legacy_import, pools, prices
from app.database import get_db
from app.dependencies import require_login
from app.models import (
    AppSetting,
    AuditLog,
    Coin,
    MinedCampaign,
    PoolBalance,
    PoolBalanceSample,
    Wallet,
)

logger = logging.getLogger("fossor.mined")

router = APIRouter(prefix="/api", tags=["mined"])

MAX_LEGACY_BYTES = 4 * 1024 * 1024
LEGACY_FILES = {"status": "mining-status.json", "live": "mining-live.json"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _value(amount: float | None, price: float | None) -> float | None:
    if amount is None or price is None:
        return None
    return round(amount * price, 8)


def _exchanges(coin: Coin | None) -> list[dict[str, Any]]:
    return [e for e in ((coin.exchanges if coin else None) or []) if isinstance(e, dict)]


def _listed(coin: Coin | None, quote: dict[str, Any]) -> bool:
    if quote.get("usd") is not None or quote.get("eur") is not None:
        return True
    return any(not e.get("dex") for e in _exchanges(coin))


async def _quotes(db: Session) -> dict[str, dict[str, Any]]:
    """Price per ticker: CoinGecko cache first, then the optional manual price."""
    coins = db.query(Coin).all()
    ids = [c.coingecko_id for c in coins if c.coingecko_id and c.coingecko_verified]
    fetched = await prices.fetch_prices(ids) if ids else {}

    out: dict[str, dict[str, Any]] = {}
    for coin in coins:
        quote = fetched.get(coin.coingecko_id or "", {}) if coin.coingecko_verified else {}
        usd = quote.get("usd")
        eur = quote.get("eur")
        if usd is not None or eur is not None:
            out[coin.ticker] = {
                "usd": usd,
                "eur": eur,
                "change_24h": quote.get("change_24h"),
                "source": quote.get("source") or "coingecko",
                "fetched_at": quote.get("fetched_at"),
            }
        elif coin.manual_price_usd is not None or coin.manual_price_eur is not None:
            out[coin.ticker] = {
                "usd": coin.manual_price_usd,
                "eur": coin.manual_price_eur,
                "change_24h": None,
                "source": "manuel",
                "fetched_at": None,
            }
        else:
            out[coin.ticker] = {
                "usd": None,
                "eur": None,
                "change_24h": None,
                "source": "",
                "fetched_at": None,
            }
    return out


def _campaign_json(
    row: MinedCampaign,
    coin: Coin | None,
    quote: dict[str, Any],
    wallet: Wallet | None,
    pool_balance: PoolBalance | None,
) -> dict[str, Any]:
    balance = row.last_balance
    if balance is None and row.state == "active":
        balance = row.mined_amount
    return {
        "id": row.id,
        "key": row.legacy_key,
        "coin": row.coin_name or row.coin_ticker,
        "ticker": row.coin_ticker,
        "state": row.state,
        "start_iso": row.start_iso,
        "end_iso": row.end_iso,
        "machine": row.machine,
        "pool": row.pool,
        "algo": row.algo,
        "hashrate": row.hashrate,
        "mined_raw": row.mined_raw,
        "mined_amount": row.mined_amount,
        "balance": balance,
        "balance_note": row.balance_note,
        "price_usd": quote.get("usd"),
        "price_eur": quote.get("eur"),
        "change_24h": quote.get("change_24h"),
        "price_source": quote.get("source") or "",
        "price_fetched_at": quote.get("fetched_at"),
        "value_usd": _value(balance, quote.get("usd")),
        "value_eur": _value(balance, quote.get("eur")),
        "exchanges": _exchanges(coin),
        "listed": _listed(coin, quote),
        "links": row.links or {},
        "verdict": row.verdict,
        "notes": row.notes,
        "wallet_id": wallet.id if wallet else None,
        "wallet": (wallet.address if wallet else row.wallet_address),
        "pool_balance": None if pool_balance is None else {
            "available": bool(pool_balance.available),
            "paid": pool_balance.paid,
            "unpaid": pool_balance.unpaid,
            "immature": pool_balance.immature,
            "threshold": pool_balance.threshold,
            "last_payment_at": pool_balance.last_payment_at,
            "last_payment_amount": pool_balance.last_payment_amount,
            "source_url": pool_balance.source_url,
            "note": pool_balance.note,
            "fetched_at": pool_balance.fetched_at,
        },
    }


def _history_json(row: MinedCampaign, coin: Coin | None, quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.id,
        "coin": row.coin_name or row.coin_ticker,
        "ticker": row.coin_ticker,
        "start_iso": row.start_iso,
        "end_iso": row.end_iso,
        "mined_raw": row.mined_raw,
        "mined_amount": row.mined_amount,
        "price_usd": quote.get("usd"),
        "price_eur": quote.get("eur"),
        "price_source": quote.get("source") or "",
        "value_usd": _value(row.mined_amount, quote.get("usd")),
        "value_eur": _value(row.mined_amount, quote.get("eur")),
        "exchanges": _exchanges(coin),
        "listed": _listed(coin, quote),
        "state": row.state,
        "verdict": row.verdict,
        "links": row.links or {},
    }


def _wallet_for(row: MinedCampaign, wallets: dict, by_coin: dict) -> Wallet | None:
    """Wallet of a campaign: exact address first, else the coin's first wallet.

    Imported campaigns often carry no address (the Pearl wallet lives in the old
    page, not in the JSON files): the wallet added by hand for that coin is used.
    """
    if row.wallet_address:
        wallet = wallets.get((row.coin_ticker, row.wallet_address))
        if wallet is not None:
            return wallet
    return by_coin.get(row.coin_ticker)


@router.get("/mined")
async def mined(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Everything the « Tokens minés » screen needs, in one call."""
    coins = {row.ticker: row for row in db.query(Coin).all()}
    quotes = await _quotes(db)

    wallets = {
        (wallet.coin_ticker, wallet.address): wallet for wallet in db.query(Wallet).all()
    }
    balances = {row.wallet_id: row for row in db.query(PoolBalance).all()}
    by_coin: dict[str, Wallet] = {}
    for wallet in wallets.values():
        current = by_coin.get(wallet.coin_ticker)
        if current is None or (not current.pool and wallet.pool):
            by_coin[wallet.coin_ticker] = wallet

    rows = db.query(MinedCampaign).order_by(MinedCampaign.id.asc()).all()
    campaigns: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    for row in rows:
        coin = coins.get(row.coin_ticker)
        quote = quotes.get(row.coin_ticker) or {}
        if row.state == "active":
            wallet = _wallet_for(row, wallets, by_coin)
            balance = balances.get(wallet.id) if wallet else None
            campaigns.append(_campaign_json(row, coin, quote, wallet, balance))
        else:
            history.append(_history_json(row, coin, quote))

    campaigns.sort(key=lambda c: (-(c["value_eur"] or 0), c["coin"]))
    history.sort(key=lambda h: (h["start_iso"] or ""), reverse=True)

    total_eur = sum(c["value_eur"] for c in campaigns if c["value_eur"] is not None)
    total_usd = sum(c["value_usd"] for c in campaigns if c["value_usd"] is not None)
    priced = sum(1 for c in campaigns if c["price_eur"] is not None or c["price_usd"] is not None)

    return {
        "as_of": _now(),
        "offline": prices.offline(),
        "total": {
            "eur": round(total_eur, 4),
            "usd": round(total_usd, 4),
            "priced_campaigns": priced,
            "unpriced_campaigns": len(campaigns) - priced,
        },
        "history_value": {
            "eur": round(sum(h["value_eur"] for h in history if h["value_eur"] is not None), 4),
            "usd": round(sum(h["value_usd"] for h in history if h["value_usd"] is not None), 4),
        },
        "campaigns": campaigns,
        "history": history,
        "counts": {
            "campaigns": len(campaigns),
            "history": len(history),
            "coins": len(coins),
            "wallets": len(wallets),
        },
    }


@router.post("/mined/refresh")
async def mined_refresh(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Fetch CoinGecko prices and every configured pool balance, store, report."""
    coins = db.query(Coin).all()
    ids = [c.coingecko_id for c in coins if c.coingecko_id and c.coingecko_verified]
    fetched = await prices.fetch_prices(ids, force=True) if ids else {}

    report: list[dict[str, Any]] = []
    stamp = _now()
    for wallet in db.query(Wallet).filter(Wallet.pool != "").all():
        result = await pools.refresh_wallet(wallet)
        if result.get("skipped"):
            report.append({
                "wallet_id": wallet.id, "ticker": wallet.coin_ticker,
                "pool": wallet.pool, "ok": False, "error": result.get("error"),
            })
            continue
        if not result.get("ok"):
            report.append({
                "wallet_id": wallet.id, "ticker": wallet.coin_ticker,
                "pool": wallet.pool, "ok": False, "error": result.get("error"),
            })
            continue

        row = db.query(PoolBalance).filter(PoolBalance.wallet_id == wallet.id).first()
        if row is None:
            row = PoolBalance(wallet_id=wallet.id, pool=wallet.pool, coin_ticker=wallet.coin_ticker)
            db.add(row)
        row.pool = wallet.pool
        row.coin_ticker = wallet.coin_ticker
        row.available = bool(result.get("available"))
        row.paid = result.get("paid")
        row.unpaid = result.get("unpaid")
        row.immature = result.get("immature")
        row.threshold = result.get("threshold")
        row.last_payment_at = result.get("last_payment_at")
        row.last_payment_amount = result.get("last_payment_amount")
        row.last_payment_tx = result.get("last_payment_tx")
        row.source_url = result.get("source_url")
        row.note = result.get("note")
        raw = dict(result.get("raw") or {})
        raw.update({
            "workers": result.get("workers") or [],
            "hashrate_hs": result.get("hashrate_hs"),
            "hashrate_avg_24h_hs": result.get("hashrate_avg_24h_hs"),
            "accepted_shares": result.get("accepted_shares"),
            "rejected_shares": result.get("rejected_shares"),
            "pool_hashrate_hs": result.get("pool_hashrate_hs"),
            "units_per_coin": result.get("units_per_coin"),
            "units_verified": result.get("units_verified"),
        })
        row.raw = raw
        row.fetched_at = stamp
        # §4.15: dated sample, the 24 h balance delta is the revenue estimate.
        parts = [result.get("paid"), result.get("unpaid"), result.get("immature")]
        total = round(sum(p for p in parts if p is not None), 12) if any(
            p is not None for p in parts
        ) else None
        db.add(PoolBalanceSample(
            wallet_id=wallet.id,
            ts=stamp,
            paid=result.get("paid"),
            unpaid=result.get("unpaid"),
            immature=result.get("immature"),
            total=total,
        ))
        report.append({
            "wallet_id": wallet.id,
            "ticker": wallet.coin_ticker,
            "pool": wallet.pool,
            "ok": True,
            "available": bool(result.get("available")),
            "threshold": result.get("threshold"),
            "hashrate_hs": result.get("hashrate_hs"),
            "workers": len(result.get("workers") or []),
            "note": result.get("note"),
        })
    db.commit()

    return {
        "as_of": stamp,
        "prices": {
            "requested": ids,
            "received": {cid: {"usd": vals.get("usd"), "eur": vals.get("eur")} for cid, vals in fetched.items()},
            "offline": prices.offline(),
        },
        "pools": report,
    }


# ── Legacy import ─────────────────────────────────────────────────────
class LegacyImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_url: str | None = None
    live_url: str | None = None
    status: dict[str, Any] | None = None
    live: dict[str, Any] | None = None
    dry_run: bool = True


def _base_url(db: Session) -> str:
    """Allowed origin of the legacy files — configured, never hard-coded."""
    env = (os.environ.get("FOSSOR_LEGACY_BASE_URL") or "").strip()
    if env:
        return env.rstrip("/")
    row = db.query(AppSetting).filter(AppSetting.key == "legacy_import_base_url").first()
    if row is None:
        return ""
    try:
        return str(json.loads(row.value) or "").rstrip("/")
    except (ValueError, TypeError):
        return ""


async def _fetch_legacy(url: str, base: str, filename: str) -> dict[str, Any]:
    if not base:
        raise HTTPException(
            status_code=400,
            detail="URL de base non configurée : FOSSOR_LEGACY_BASE_URL ou réglage legacy_import_base_url",
        )
    clean = url.split("?")[0]
    if not clean.startswith(base + "/") or not clean.endswith("/" + filename):
        raise HTTPException(
            status_code=400,
            detail=f"Seul GET {base}/{filename} est autorisé pour l'import legacy",
        )

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(clean)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{filename} injoignable : {exc}")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"{filename} : HTTP {response.status_code}")
    if len(response.content) > MAX_LEGACY_BYTES:
        raise HTTPException(status_code=413, detail=f"{filename} trop gros")
    try:
        return response.json()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{filename} : JSON invalide")


def _run_import(
    db: Session,
    status: dict[str, Any],
    live: dict[str, Any],
    dry_run: bool,
    sources: dict[str, Any],
) -> dict[str, Any]:
    plan = legacy_import.build_plan(status, live, source="legacy")
    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "sources": sources,
        "counts": plan["counts"],
        "preview": legacy_import.preview(plan),
        "warnings": plan["warnings"],
        "skipped": plan.get("skipped", []),
        "applied": None,
    }
    if dry_run:
        return payload

    counters = legacy_import.apply_plan(db, plan)
    payload["applied"] = counters
    db.add(AuditLog(
        actor="fred",
        action="import_legacy",
        object_ref="legacy",
        result="ok",
        output=json.dumps(counters, ensure_ascii=False),
    ))
    db.commit()
    return payload


@router.post("/import/legacy")
async def import_legacy(
    body: LegacyImportBody | None = None,
    dry_run: bool | None = Query(default=None, description="force dry_run (défaut : true)"),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Import ``mining-status.json`` + ``mining-live.json`` (URLs or inline JSON)."""
    body = body or LegacyImportBody()
    effective_dry_run = body.dry_run if dry_run is None else dry_run
    sources: dict[str, Any] = {}

    if body.status is not None:
        status = body.status
        sources["status"] = "inline"
    else:
        base = _base_url(db)
        url = body.status_url or (f"{base}/{LEGACY_FILES['status']}" if base else "")
        if not url:
            raise HTTPException(status_code=400, detail="status_url manquante")
        status = await _fetch_legacy(url, base, LEGACY_FILES["status"])
        sources["status"] = url

    if body.live is not None:
        live = body.live
        sources["live"] = "inline"
    else:
        base = _base_url(db)
        url = body.live_url or (f"{base}/{LEGACY_FILES['live']}" if base else "")
        if not url:
            raise HTTPException(status_code=400, detail="live_url manquante")
        live = await _fetch_legacy(url, base, LEGACY_FILES["live"])
        sources["live"] = url

    if not isinstance(status, dict) or not isinstance(live, dict):
        raise HTTPException(status_code=400, detail="Les deux fichiers doivent être des objets JSON")

    return _run_import(db, status, live, effective_dry_run, sources)


@router.post("/import/legacy/upload")
async def import_legacy_upload(
    status_file: UploadFile | None = File(default=None),
    live_file: UploadFile | None = File(default=None),
    dry_run: bool = Form(default=True),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Same import, from two uploaded files instead of URLs."""
    if status_file is None or live_file is None:
        raise HTTPException(status_code=400, detail="status_file et live_file sont requis")

    documents: dict[str, dict[str, Any]] = {}
    for key, upload in (("status", status_file), ("live", live_file)):
        raw = await upload.read()
        if len(raw) > MAX_LEGACY_BYTES:
            raise HTTPException(status_code=413, detail=f"{upload.filename} trop gros")
        try:
            documents[key] = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{upload.filename} : JSON invalide")

    sources = {"status": status_file.filename, "live": live_file.filename}
    return _run_import(db, documents["status"], documents["live"], dry_run, sources)
