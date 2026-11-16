"""Pool connectors (§4.16) — Luckypool first: that is where the Pearl is.

What the public API actually offers (probed 2026-11-16):

* ``GET https://pearl.luckypool.io/api/stats`` → 200 JSON: ``config`` (fee,
  ``coinUnits``, ``minPaymentThreshold``, ``paymentInterval``), ``stats``
  (pool-wide miners/workers/hashrate/blocks), ``payments`` (last pool payouts),
  ``charts``, ``network``.
* Every per-wallet shape returns 404 (``/api/miner/{addr}``,
  ``/api/payments/{addr}``, ``/api/miner/{pool}/{addr}``,
  ``/api/miner-stats/{addr}``, ``?address=`` variants). No documented endpoint
  gives a wallet balance, unpaid or immature amount.

The connector therefore returns the threshold and the pool aggregates, and
declares ``available: False`` for the wallet figures instead of inventing them.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("fossor.pools")

LUCKYPOOL_STATS_URL = "https://pearl.luckypool.io/api/stats"
REQUEST_TIMEOUT_S = 15.0

TRANSPORT = None  # monkeypatched in tests

UNAVAILABLE_NOTE = (
    "L'API publique de Luckypool n'expose aucune statistique par wallet : "
    "seul /api/stats répond, avec des agrégats du pool. Solde, non payé et "
    "immature indisponibles — valeurs non inventées."
)


def offline() -> bool:
    return os.environ.get("FOSSOR_OFFLINE") == "1"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _units_per_coin(config: dict[str, Any]) -> float:
    """``coinUnits`` is the number of atomic units in one coin (MiningCore).

    Real value on the Pearl pool: ``100000000`` (1 PRL = 1e8 units). A small
    value is read as a decimal exponent so older payloads still parse.
    """
    raw = _int(config.get("coinUnits"))
    if raw is None or raw <= 0:
        return float(10 ** 8)
    if raw <= 24:
        return float(10 ** raw)
    return float(raw)


def _coins(value: Any, units_per_coin: float) -> float | None:
    """Atomic units → coin units (Luckypool returns strings)."""
    raw = _int(value)
    if raw is None:
        return None
    return round(raw / units_per_coin, 12)


def parse_luckypool(
    payload: dict[str, Any],
    address: str = "",
    coin: str = "PRL",
    source_url: str = LUCKYPOOL_STATS_URL,
) -> dict[str, Any]:
    """Shape a ``/api/stats`` payload into a pool-balance record."""
    payload = payload if isinstance(payload, dict) else {}
    config = payload.get("config") or {}
    stats = payload.get("stats") or {}
    pool = stats.get("pool") or {}

    units = _units_per_coin(config)

    return {
        "pool": "luckypool",
        "coin": coin,
        "wallet": address,
        "available": False,
        "paid": None,
        "unpaid": None,
        "immature": None,
        "threshold": _coins(config.get("minPaymentThreshold"), units),
        "last_payment_at": None,
        "last_payment_amount": None,
        "last_payment_tx": None,
        "source_url": source_url,
        "fetched_at": _stamp(),
        "note": UNAVAILABLE_NOTE,
        "raw": {
            "pool_miners": pool.get("miners"),
            "pool_workers": pool.get("workers"),
            "pool_hashrate_hs": pool.get("hashrate"),
            "blocks_found": pool.get("blocksFound"),
            "fee": config.get("fee"),
            "payment_interval_s": config.get("paymentInterval"),
            "coin_units": units,
        },
    }


async def luckypool_balance(address: str, coin: str = "PRL") -> dict[str, Any]:
    """Fetch Luckypool pool stats and shape them for *address*.

    Any failure returns ``ok: False`` with the error — never a zeroed balance.
    """
    if offline():
        return {"ok": False, "error": "réseau désactivé (FOSSOR_OFFLINE=1)", "pool": "luckypool"}
    if not (address or "").strip():
        return {"ok": False, "error": "adresse de wallet absente", "pool": "luckypool"}

    import httpx

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=TRANSPORT) as client:
            response = await client.get(LUCKYPOOL_STATS_URL)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Luckypool indisponible: %s", exc)
        return {"ok": False, "error": str(exc), "pool": "luckypool"}

    record = parse_luckypool(payload, address=address, coin=coin)
    record["ok"] = True
    return record


CONNECTORS = {"luckypool": luckypool_balance}


async def refresh_wallet(wallet: Any) -> dict[str, Any]:
    """Dispatch on ``wallet.pool``; unknown or empty pool is a no-op."""
    name = (getattr(wallet, "pool", "") or "").strip().lower()
    connector = CONNECTORS.get(name)
    if connector is None:
        return {"ok": False, "skipped": True, "error": f"pool inconnu: {name or '(vide)'}"}
    return await connector(wallet.address, coin=wallet.coin_ticker)
