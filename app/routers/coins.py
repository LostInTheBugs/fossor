"""Coin catalogue (§4.3) and wallets (§4.4) — CRUD.

A wallet row holds an address, where it lives and where its backup is. A seed, a
passphrase, a password or a private key is **never** accepted, stored or
returned: every input model forbids unknown fields, so sending one is a 422 and
nothing reaches the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Coin, PoolBalance, Wallet

router = APIRouter(prefix="/api", tags=["catalogue"])

# Documented for reviewers: these fields do not exist anywhere in the model.
FORBIDDEN_FIELDS = (
    "seed", "seed_phrase", "mnemonic", "password", "passphrase",
    "private_key", "privkey", "secret", "secret_ref",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(db: Session, action: str, ref: str, result: str = "ok") -> None:
    db.add(AuditLog(actor="fred", action=action, object_ref=ref, result=result))


def _coin_json(coin: Coin, wallets: int = 0) -> dict[str, Any]:
    return {
        "ticker": coin.ticker,
        "name": coin.name,
        "algo": coin.algo,
        "kind": coin.kind,
        "site_url": coin.site_url,
        "ann_url": coin.ann_url,
        "explorer_url": coin.explorer_url,
        "coingecko_id": coin.coingecko_id,
        "coingecko_verified": bool(coin.coingecko_verified),
        "exchanges": coin.exchanges or [],
        "pool_hosts": coin.pool_hosts or [],
        "manual_price_usd": coin.manual_price_usd,
        "manual_price_eur": coin.manual_price_eur,
        "notes": coin.notes,
        "wallets": wallets,
    }


def _wallet_json(wallet: Wallet) -> dict[str, Any]:
    return {
        "id": wallet.id,
        "coin_ticker": wallet.coin_ticker,
        "address": wallet.address,
        "label": wallet.label,
        "location": wallet.location,
        "backup_path": wallet.backup_path,
        "pool": wallet.pool,
        "pool_host": wallet.pool_host,
    }


# ── Coins ─────────────────────────────────────────────────────────────
class CoinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=20)
    name: str = ""
    algo: str = ""
    kind: Literal["cpu", "gpu", "both"] = "both"
    site_url: str | None = None
    ann_url: str | None = None
    explorer_url: str | None = None
    coingecko_id: str | None = None
    coingecko_verified: bool = False
    exchanges: list[dict[str, Any]] = Field(default_factory=list)
    pool_hosts: list[str] = Field(default_factory=list)
    manual_price_usd: float | None = None
    manual_price_eur: float | None = None
    notes: str | None = None


class CoinUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    algo: str | None = None
    kind: Literal["cpu", "gpu", "both"] | None = None
    site_url: str | None = None
    ann_url: str | None = None
    explorer_url: str | None = None
    coingecko_id: str | None = None
    coingecko_verified: bool | None = None
    exchanges: list[dict[str, Any]] | None = None
    pool_hosts: list[str] | None = None
    manual_price_usd: float | None = None
    manual_price_eur: float | None = None
    notes: str | None = None


@router.get("/coins")
def list_coins(db: Session = Depends(get_db), _user=Depends(require_login)):
    counts: dict[str, int] = {}
    for wallet in db.query(Wallet).all():
        counts[wallet.coin_ticker] = counts.get(wallet.coin_ticker, 0) + 1
    coins = db.query(Coin).order_by(Coin.ticker.asc()).all()
    return [_coin_json(coin, counts.get(coin.ticker, 0)) for coin in coins]


@router.post("/coins", status_code=201)
def create_coin(body: CoinIn, db: Session = Depends(get_db), _user=Depends(require_login)):
    ticker = body.ticker.strip().upper()
    if db.query(Coin).filter(Coin.ticker == ticker).first():
        raise HTTPException(status_code=409, detail=f"Coin {ticker} déjà au catalogue")
    coin = Coin(**{**body.model_dump(exclude={"ticker"}), "ticker": ticker})
    db.add(coin)
    _audit(db, "coin_create", ticker)
    db.commit()
    db.refresh(coin)
    return _coin_json(coin)


@router.patch("/coins/{ticker}")
def update_coin(
    ticker: str,
    body: CoinUpdate,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    coin = db.query(Coin).filter(Coin.ticker == ticker.strip().upper()).first()
    if coin is None:
        raise HTTPException(status_code=404, detail="Coin inconnu")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(coin, field, value)
    coin.updated_at = _now()
    _audit(db, "coin_update", coin.ticker)
    db.commit()
    return _coin_json(coin)


@router.delete("/coins/{ticker}")
def delete_coin(ticker: str, db: Session = Depends(get_db), _user=Depends(require_login)):
    coin = db.query(Coin).filter(Coin.ticker == ticker.strip().upper()).first()
    if coin is None:
        raise HTTPException(status_code=404, detail="Coin inconnu")
    linked = db.query(Wallet).filter(Wallet.coin_ticker == coin.ticker).count()
    if linked:
        raise HTTPException(status_code=409, detail=f"{linked} wallet(s) référencent {coin.ticker}")
    db.delete(coin)
    _audit(db, "coin_delete", coin.ticker)
    db.commit()
    return {"deleted": coin.ticker}


# ── Wallets ───────────────────────────────────────────────────────────
class WalletIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coin_ticker: str = Field(min_length=1, max_length=20)
    address: str = Field(min_length=1, max_length=255)
    label: str = ""
    location: str = ""
    backup_path: str = ""
    pool: str = ""
    pool_host: str = ""


class WalletUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    location: str | None = None
    backup_path: str | None = None
    pool: str | None = None
    pool_host: str | None = None
    address: str | None = None


@router.get("/wallets")
def list_wallets(db: Session = Depends(get_db), _user=Depends(require_login)):
    balances = {row.wallet_id: row for row in db.query(PoolBalance).all()}
    out = []
    for wallet in db.query(Wallet).order_by(Wallet.coin_ticker.asc(), Wallet.id.asc()).all():
        balance = balances.get(wallet.id)
        out.append({
            **_wallet_json(wallet),
            "pool_balance": None if balance is None else {
                "available": bool(balance.available),
                "paid": balance.paid,
                "unpaid": balance.unpaid,
                "immature": balance.immature,
                "threshold": balance.threshold,
                "fetched_at": balance.fetched_at,
            },
        })
    return out


@router.post("/wallets", status_code=201)
def create_wallet(body: WalletIn, db: Session = Depends(get_db), _user=Depends(require_login)):
    ticker = body.coin_ticker.strip().upper()
    if db.query(Coin).filter(Coin.ticker == ticker).first() is None:
        raise HTTPException(status_code=400, detail=f"Coin {ticker} absent du catalogue")
    exists = (
        db.query(Wallet)
        .filter(Wallet.coin_ticker == ticker, Wallet.address == body.address.strip())
        .first()
    )
    if exists:
        raise HTTPException(status_code=409, detail="Cette adresse existe déjà pour ce coin")
    wallet = Wallet(**{**body.model_dump(exclude={"coin_ticker"}), "coin_ticker": ticker})
    wallet.address = wallet.address.strip()
    db.add(wallet)
    _audit(db, "wallet_create", f"{ticker}:{wallet.address[:12]}")
    db.commit()
    db.refresh(wallet)
    return _wallet_json(wallet)


@router.patch("/wallets/{wallet_id}")
def update_wallet(
    wallet_id: int,
    body: WalletUpdate,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet inconnu")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(wallet, field, value)
    wallet.updated_at = _now()
    _audit(db, "wallet_update", f"{wallet.coin_ticker}:{wallet.address[:12]}")
    db.commit()
    return _wallet_json(wallet)


@router.delete("/wallets/{wallet_id}")
def delete_wallet(wallet_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet inconnu")
    db.query(PoolBalance).filter(PoolBalance.wallet_id == wallet.id).delete()
    db.delete(wallet)
    _audit(db, "wallet_delete", f"{wallet.coin_ticker}:{wallet.address[:12]}")
    db.commit()
    return {"deleted": wallet_id}
