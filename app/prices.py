"""CoinGecko ``simple/price`` client (§4.16) — 5 min cache, verified ids only.

Only coins whose ``coingecko_id`` is filled **and** marked verified are priced:
a wrong id silently prices another token (``wam`` on CoinGecko is wam.app, not
WAM Coin), so an unverified id is never queried.

The HTTP transport is a module global so tests can plug ``httpx.MockTransport``
without touching the network; ``FOSSOR_OFFLINE=1`` disables every call.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("fossor.prices")

COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
CACHE_TTL_S = 300
REQUEST_TIMEOUT_S = 10.0

TRANSPORT = None  # monkeypatched in tests

_cache: dict[str, dict[str, Any]] = {}
_cache_ts: float = 0.0


def offline() -> bool:
    """True when the process must not touch the network (tests, e2e)."""
    return os.environ.get("FOSSOR_OFFLINE") == "1"


def clear_cache() -> None:
    global _cache_ts
    _cache.clear()
    _cache_ts = 0.0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


async def fetch_prices(ids: list[str], force: bool = False) -> dict[str, dict[str, Any]]:
    """Return ``{coingecko_id: {usd, eur, change_24h, source, fetched_at}}``.

    A network failure never raises: the last cached values are returned instead.
    """
    global _cache_ts

    wanted = sorted({str(i).strip() for i in ids if str(i or "").strip()})
    if not wanted or offline():
        return {}

    fresh = (time.time() - _cache_ts) < CACHE_TTL_S and all(w in _cache for w in wanted)
    if fresh and not force:
        return {w: _cache[w] for w in wanted}

    import httpx

    params = {
        "ids": ",".join(wanted),
        "vs_currencies": "usd,eur",
        "include_24hr_change": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=TRANSPORT) as client:
            response = await client.get(COINGECKO_PRICE_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # network, HTTP status, invalid JSON
        logger.warning("CoinGecko indisponible: %s", exc)
        return {w: _cache[w] for w in wanted if w in _cache}

    stamp = _stamp()
    for coin_id, values in (payload or {}).items():
        if not isinstance(values, dict):
            continue
        _cache[str(coin_id)] = {
            "usd": _num(values.get("usd")),
            "eur": _num(values.get("eur")),
            "change_24h": _num(values.get("usd_24h_change")),
            "source": "coingecko",
            "fetched_at": stamp,
        }
    _cache_ts = time.time()
    return {w: _cache[w] for w in wanted if w in _cache}
