"""Built-in pool → coin table and the base coin catalogue (2026.11.003-c6).

The mapping lives in the code, not in the ``coins`` table: a real installation
started with an empty catalogue, so pool-based inference found nothing and
``coin`` stayed empty for almost every miner.  The catalogue is consulted
first and may complete or override this table; the built-in table is always
the fallback.

Pure module: no database import at module level, no network.
"""

from __future__ import annotations

from typing import Any, Iterable

# Base coins created at startup when the catalogue is empty.  ``algo`` is the
# algorithm SRBMiner/MULTI and cpuminer announce on the real fleet.
BASE_COINS: tuple[dict[str, Any], ...] = (
    {
        "ticker": "PRL",
        "name": "Pearl",
        "algo": "pearlhash",
        "kind": "gpu",
        "pool_hosts": [
            "pearl.luckypool.io",
            "pearl-eu1.luckypool.io",
            "pearl-eu2.luckypool.io",
            "pearl-us1.luckypool.io",
        ],
    },
    {
        "ticker": "KSH",
        "name": "Korsh",
        "algo": "yespower",
        "kind": "both",
        "pool_hosts": ["pool.korsh.org", "korsh.luckypool.io"],
    },
    {
        "ticker": "WAM",
        "name": "Wamcoin",
        "algo": "yespower",
        "kind": "both",
        "pool_hosts": ["pool.wamcoin.org", "wamcoin.luckypool.io"],
    },
    {
        "ticker": "BIG",
        "name": "Bigcoin",
        "algo": "sha256d",
        "kind": "both",
        "pool_hosts": [],
    },
)

# Exact pool host → ticker.
POOL_COINS: dict[str, str] = {
    "pearl.luckypool.io": "PRL",
    "pearl-eu1.luckypool.io": "PRL",
    "pearl-eu2.luckypool.io": "PRL",
    "pearl-us1.luckypool.io": "PRL",
    "pearl.luckypool.io:3360": "PRL",
    "pool.korsh.org": "KSH",
    "korsh.luckypool.io": "KSH",
    "pool.wamcoin.org": "WAM",
    "wamcoin.luckypool.io": "WAM",
}

# Any ``pearl*`` sub-domain of the Luckypool pool farms is Pearl.
LUCKYPOOL_DOMAIN = "luckypool.io"
LUCKYPOOL_PREFIXES: dict[str, str] = {"pearl": "PRL"}

# Fallback when the pool is unknown (SRBMiner announces ``pearlhash``).
ALGO_COINS: dict[str, str] = {"pearlhash": "PRL"}


def pool_host(value: str) -> str:
    """``stratum+tcp://user@pearl-eu2.luckypool.io:3360`` → ``pearl-eu2.luckypool.io``."""
    text = (value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("@")[-1].split("/")[0].split("?")[0]
    return text.split(":")[0]


def coin_for_pool(pool: str) -> str:
    """Ticker for a pool URL, from the built-in table only (catalogue first)."""
    host = pool_host(pool)
    if not host:
        return ""
    if host in POOL_COINS:
        return POOL_COINS[host]
    if host == LUCKYPOOL_DOMAIN or host.endswith("." + LUCKYPOOL_DOMAIN):
        label = host.split(".", 1)[0]
        for prefix, ticker in LUCKYPOOL_PREFIXES.items():
            if label.startswith(prefix):
                return ticker
    for needle, ticker in POOL_COINS.items():
        if needle in host:
            return ticker
    return ""


def coin_for_algo(algo: str) -> str:
    """Ticker announced by the algorithm (``pearlhash`` → ``PRL``)."""
    return ALGO_COINS.get(str(algo or "").strip().lower(), "")


def builtin_hosts() -> dict[str, str]:
    """Pool host → ticker, from ``BASE_COINS`` (includes every known host)."""
    hosts: dict[str, str] = {}
    for coin in BASE_COINS:
        for host in coin["pool_hosts"]:
            hosts[str(host).lower()] = coin["ticker"]
    hosts.update(POOL_COINS)
    return hosts


def builtin_tickers() -> list[str]:
    return [coin["ticker"] for coin in BASE_COINS]


def builtin_names() -> dict[str, str]:
    """Coin name (lower case) → ticker, for node solo mining."""
    return {str(coin["name"]).lower(): coin["ticker"] for coin in BASE_COINS}


def seed_builtin_coins(db) -> int:
    """Create the base coins when the catalogue is empty.  Returns rows added.

    Never touches an existing catalogue: it only fills a void (the production
    database had zero rows, which is why every pool lookup failed).
    """
    from app.models import Coin

    try:
        if db.query(Coin).count() > 0:
            return 0
    except Exception:  # pragma: no cover - table missing before create_all
        return 0

    added = 0
    for entry in BASE_COINS:
        db.add(Coin(
            ticker=entry["ticker"],
            name=entry["name"],
            algo=entry["algo"],
            kind=entry["kind"],
            pool_hosts=list(entry["pool_hosts"]),
        ))
        added += 1
    db.commit()
    return added
