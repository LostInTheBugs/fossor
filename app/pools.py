"""Pool connectors (§4.16) — Luckypool first: that is where the Pearl is.

Endpoints actually served by a Luckypool coin site (probed 2026-11-16):

* ``GET https://<coin>.luckypool.io/api/stats`` → ``config`` (``coinUnits``,
  ``minPaymentThreshold``, fee, paymentInterval) and the pool aggregates.
* ``GET https://<coin>.luckypool.io/api/stats_address?address=<wallet>`` →
  ``stats`` (paid / unlocked / locked, paymentThreshold, hashrate, shares) and
  ``workers`` (name, minerAgent, hashrate). The page declares it through
  ``window.coin.api`` and the bundle contains ``Qe("/stats_address",{address:e})``.

``coinUnits`` is the number of atomic units in one coin (``100000000`` on the
Pearl pool: 1 PRL = 1e8 units). It is cross-checked against
``minPaymentThreshold`` (100000000 units = 1 PRL) so a wrong divisor is caught
instead of silently multiplying balances by 1000.

A missing wallet or a rejected address is an error (``ok: False``), never a
zeroed balance.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("fossor.pools")

# Per-coin subdomain of the Luckypool frontend (``pearl.luckypool.io``).
LUCKYPOOL_HOSTS = {"PRL": "pearl", "QTC": "qtc", "WAM": "wam"}
DEFAULT_UNITS_PER_COIN = 100_000_000
REQUEST_TIMEOUT_S = 15.0
# Kept for compatibility with the 2b tests: default /api/stats URL (Pearl).
LUCKYPOOL_STATS_URL = "https://pearl.luckypool.io/api/stats"

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


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def luckypool_host(coin: str = "PRL", pool_host: str = "") -> str:
    """Hôte du **site** Luckypool — jamais un point d'entrée stratum.

    Un profil stocke ``stratum+tcp://pearl-eu2.luckypool.io:3360`` ; interroger
    cet hôte en HTTPS échoue sur le certificat (le stratum ne sert pas le
    site).  ``LUCKYPOOL_API_HOSTS`` ramène chaque ferme connue au site observé
    dans le client du pool (``pearl.luckypool.io``).  Un hôte inconnu lève
    avant toute requête : on ne devine pas une URL, et on ne désactive jamais
    la vérification TLS.
    """
    from app.coins import LUCKYPOOL_API_HOSTS, LUCKYPOOL_DOMAIN, pool_host as host_of

    raw = (pool_host or "").strip().lower().rstrip(".")
    raw = host_of(raw) or raw
    if raw:
        mapped = LUCKYPOOL_API_HOSTS.get(raw)
        if mapped is None and raw.endswith("." + LUCKYPOOL_DOMAIN):
            mapped = LUCKYPOOL_API_HOSTS.get(raw.split(".", 1)[0])
        if mapped:
            return mapped
        if "." not in raw:
            return f"{raw}.luckypool.io"
        raise ValueError(
            f"hôte Luckypool inconnu : {raw} — aucune API web connue pour cet hôte "
            "(un serveur stratum ne répond pas en HTTPS)"
        )
    ticker = (coin or "").strip().upper()
    sub = LUCKYPOOL_HOSTS.get(ticker) or (ticker.lower() if ticker else "")
    if not sub:
        raise ValueError("coin absent : impossible de choisir le sous-domaine Luckypool")
    return f"{sub}.luckypool.io"


def stats_url(host: str) -> str:
    return f"https://{host}/api/stats"


def address_url(host: str, address: str) -> str:
    return f"https://{host}/api/stats_address?address={quote(address, safe='')}"


def _units_per_coin(config: dict[str, Any]) -> tuple[float, bool]:
    """``coinUnits`` as a number of units per coin, plus its threshold check.

    A value ≤ 24 is read as a decimal exponent (older payloads). The check is
    ``minPaymentThreshold / units ≈ 1``: the pool's minimum is 1 coin.
    """
    raw = _int(config.get("coinUnits"))
    if raw is None or raw <= 0:
        return float(DEFAULT_UNITS_PER_COIN), False
    units = float(10 ** raw) if raw <= 24 else float(raw)
    minimum = _int(config.get("minPaymentThreshold"))
    verified = minimum is not None and units > 0 and abs(minimum / units - 1.0) < 0.01
    return units, verified


def _coins(value: Any, units_per_coin: float) -> float | None:
    """Atomic units → coin units (Luckypool returns strings)."""
    raw = _int(value)
    if raw is None:
        return None
    return round(raw / units_per_coin, 12)


def _hashrate(value: Any) -> float | None:
    """H/s, as a float (some fields are strings)."""
    return _float(value)


def parse_stats(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape a ``/api/stats`` payload into divisor + pool aggregates."""
    payload = payload if isinstance(payload, dict) else {}
    config = payload.get("config") or {}
    stats = payload.get("stats") or {}
    pool = stats.get("pool") or {}
    units, verified = _units_per_coin(config)
    return {
        "units_per_coin": units,
        "units_verified": verified,
        "min_payment_threshold": _coins(config.get("minPaymentThreshold"), units),
        "pool_hashrate_hs": _hashrate(pool.get("hashrate") if pool else stats.get("hashrate")),
        "pool_miners": pool.get("miners"),
        "pool_workers": pool.get("workers"),
        "blocks_found": pool.get("blocksFound"),
        "fee_pct": _float(config.get("fee")),
        "payment_interval_s": _int(config.get("paymentInterval")),
        "symbol": config.get("symbol"),
    }


def parse_luckypool(
    payload: dict[str, Any],
    address: str = "",
    coin: str = "PRL",
    source_url: str = LUCKYPOOL_STATS_URL,
) -> dict[str, Any]:
    """Pool-only record (no per-wallet call) — kept for the §4.16 aggregate view."""
    parsed = parse_stats(payload)
    return {
        "pool": "luckypool",
        "coin": coin,
        "wallet": address,
        "available": False,
        "paid": None,
        "unpaid": None,
        "immature": None,
        "threshold": parsed["min_payment_threshold"],
        "last_payment_at": None,
        "last_payment_amount": None,
        "last_payment_tx": None,
        "source_url": source_url,
        "fetched_at": _stamp(),
        "note": UNAVAILABLE_NOTE,
        "raw": {
            "pool_miners": parsed["pool_miners"],
            "pool_workers": parsed["pool_workers"],
            "pool_hashrate_hs": parsed["pool_hashrate_hs"],
            "blocks_found": parsed["blocks_found"],
            "fee": parsed["fee_pct"],
            "payment_interval_s": parsed["payment_interval_s"],
            "coin_units": parsed["units_per_coin"],
            "coin_units_verified": parsed["units_verified"],
        },
    }


def _workers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("workers")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        avg = row.get("hashrateAvg")
        avg = avg if isinstance(avg, dict) else {}
        out.append({
            "name": (row.get("name") or "").strip(),
            "agent": row.get("minerAgent") or "",
            "hashrate_hs": _hashrate(row.get("hashrate")),
            "hashrate_avg_24h_hs": _hashrate(avg.get("24h")),
            "accepted_shares": _int(row.get("acceptedShares")),
            "rejected_shares": _int(row.get("rejectedShares")),
            "last_share": row.get("lastShare"),
        })
    return out


def parse_stats_address(
    payload: dict[str, Any],
    units_per_coin: float = float(DEFAULT_UNITS_PER_COIN),
    coin: str = "PRL",
    source_url: str = "",
) -> dict[str, Any]:
    """Shape a ``/api/stats_address`` payload into a wallet balance record."""
    payload = payload if isinstance(payload, dict) else {}
    stats = payload.get("stats") or {}
    if not isinstance(stats, dict) or not stats:
        return {
            "pool": "luckypool",
            "coin": coin,
            "wallet": "",
            "available": False,
            "paid": None,
            "unpaid": None,
            "immature": None,
            "threshold": None,
            "last_payment_at": None,
            "last_payment_amount": None,
            "last_payment_tx": None,
            "source_url": source_url,
            "fetched_at": _stamp(),
            "note": "Réponse /api/stats_address sans bloc « stats »",
            "raw": {},
        }

    avg = stats.get("hashrateAvg") if isinstance(stats.get("hashrateAvg"), dict) else {}
    payments = payload.get("payments") if isinstance(payload.get("payments"), list) else []
    last_payment = payments[0] if payments and isinstance(payments[0], dict) else {}
    paid = _coins(stats.get("paid"), units_per_coin)
    unpaid = _coins(stats.get("unlocked"), units_per_coin)
    immature = _coins(stats.get("locked"), units_per_coin)
    threshold = _coins(stats.get("paymentThreshold"), units_per_coin)

    return {
        "pool": "luckypool",
        "coin": coin,
        "wallet": stats.get("wallet") or "",
        "available": True,
        "paid": paid,
        "unpaid": unpaid,
        "immature": immature,
        "threshold": threshold,
        "last_payment_at": last_payment.get("time") or last_payment.get("created") or None,
        "last_payment_amount": _coins(
            last_payment.get("amount") or last_payment.get("value"), units_per_coin
        ),
        "last_payment_tx": last_payment.get("tx") or last_payment.get("transaction") or None,
        "source_url": source_url,
        "fetched_at": _stamp(),
        "note": "",
        "hashrate_hs": _hashrate(stats.get("hashrate")),
        "hashrate_avg_24h_hs": _hashrate(avg.get("24h")),
        "accepted_shares": _int(stats.get("acceptedShares")),
        "rejected_shares": _int(stats.get("rejectedShares")),
        "blocks_found": _int(stats.get("blocksFound")),
        "last_share": stats.get("lastShare"),
        "workers": _workers(payload),
        "raw": {
            "coin_units": units_per_coin,
            "payment_threshold_units": stats.get("paymentThreshold"),
            "unlocked_units": stats.get("unlocked"),
            "locked_units": stats.get("locked"),
            "paid_units": stats.get("paid"),
        },
    }


async def _get(client: Any, url: str) -> dict[str, Any]:
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def luckypool_balance(address: str, coin: str = "PRL", pool_host: str = "") -> dict[str, Any]:
    """Fetch ``/api/stats`` + ``/api/stats_address`` and shape the wallet record.

    Any failure returns ``ok: False`` with the error — never a zeroed balance.
    """
    if offline():
        return {"ok": False, "error": "réseau désactivé (FOSSOR_OFFLINE=1)", "pool": "luckypool"}
    if not (address or "").strip():
        return {"ok": False, "error": "adresse de wallet absente", "pool": "luckypool"}
    try:
        host = luckypool_host(coin, pool_host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "pool": "luckypool"}

    import httpx

    stats: dict[str, Any] = {}
    units = float(DEFAULT_UNITS_PER_COIN)
    verified = False
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=TRANSPORT) as client:
            try:
                stats = await _get(client, stats_url(host))
                parsed = parse_stats(stats)
                units = parsed["units_per_coin"]
                verified = parsed["units_verified"]
            except Exception as exc:  # stats is optional: address still readable
                logger.warning("Luckypool /api/stats indisponible (%s): %s", host, exc)
            url = address_url(host, address.strip())
            payload = await _get(client, url)
    except Exception as exc:
        logger.warning("Luckypool stats_address indisponible (%s): %s", host, exc)
        return {"ok": False, "error": str(exc), "pool": "luckypool", "host": host}

    record = parse_stats_address(payload, units_per_coin=units, coin=coin, source_url=url)
    if not record.get("available"):
        return {
            "ok": False,
            "error": "réponse /api/stats_address sans statistiques",
            "pool": "luckypool",
            "host": host,
            "source_url": url,
        }

    aggregate = parse_stats(stats) if stats else {}
    record.update({
        "ok": True,
        "host": host,
        "units_per_coin": units,
        "units_verified": verified,
        "pool_hashrate_hs": aggregate.get("pool_hashrate_hs"),
        "pool_miners": aggregate.get("pool_miners"),
        "pool_workers": aggregate.get("pool_workers"),
        "fee_pct": aggregate.get("fee_pct"),
        "note": (
            "Soldes lus sur /api/stats_address ; unités atomiques ÷ "
            f"{units:g} (vérifié par minPaymentThreshold = 1 {coin})"
            if verified else
            f"Soldes lus sur /api/stats_address ; diviseur {units:g} NON vérifié"
        ),
    })
    record["raw"].update({
        "pool_hashrate_hs": aggregate.get("pool_hashrate_hs"),
        "coin_units_verified": verified,
        "min_payment_threshold": aggregate.get("min_payment_threshold"),
    })
    return record


CONNECTORS = {"luckypool": luckypool_balance}


async def refresh_wallet(wallet: Any) -> dict[str, Any]:
    """Dispatch on ``wallet.pool``; unknown or empty pool is a no-op."""
    name = (getattr(wallet, "pool", "") or "").strip().lower()
    connector = CONNECTORS.get(name)
    if connector is None:
        return {"ok": False, "skipped": True, "error": f"pool inconnu: {name or '(vide)'}"}
    return await connector(
        wallet.address,
        coin=wallet.coin_ticker,
        pool_host=getattr(wallet, "pool_host", "") or "",
    )
