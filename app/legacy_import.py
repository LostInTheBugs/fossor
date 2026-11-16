"""Import of the legacy mining history (``mining-status.json`` + ``mining-live.json``).

The two files are the state of the v2.21 page. This module turns them into
catalogue rows (coins, wallets) and campaigns:

* running campaigns = ``current``, ``current_cpu`` and every ``*_spec`` key that
  is not ``stopped`` — plus the tickers that only exist in the live file
  (the Pearl campaign is published there, under the Luckypool pool);
* ended campaigns = every ``history`` entry that carries a ``mined`` sentence or
  a start date, with the amount parsed by :func:`parse_mined_amount`;
* wallets = every ``adresse`` / ``wallet`` field, with a path-only backup.

Everything is keyed so that importing twice creates nothing the second time:
``legacy_key`` for campaigns, ``(coin_ticker, address)`` for wallets, ``ticker``
for coins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.market_registry import (
    clean_backup_path,
    guess_kind,
    parse_mined_amount,
    registry_for,
)

logger = logging.getLogger("fossor.legacy_import")

# Keys of the status file that are not campaigns.
NON_CAMPAIGN_KEYS = {
    "mode", "updated", "last_tick", "history", "events", "analyses",
    "markets", "rig", "pcr", "config", "stats", "version",
}
ACTIVE_KEYS = ("current", "current_cpu")
HASHRATE_KEYS = ("hashrate_ths", "hashrate_hs", "hashrate_khs", "hashrate_mhs")


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _campaign_key(prefix: str, ticker: str, entry: dict[str, Any]) -> str:
    raw = "|".join([
        ticker,
        _s(entry.get("start_iso")),
        _s(entry.get("end_iso")),
        _s(entry.get("mined")),
    ])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{ticker}:{digest}"


def _balance_from_text(*texts: str | None, ticker: str = "") -> float | None:
    """Last ``solde <n> TICKER`` / ``wallet <n> TICKER`` mention of a free text."""
    if not ticker:
        return None
    pattern = re.compile(
        r"(?:solde|wallet)\s+(?:de\s+)?([\d][\d\s\u00a0\u202f.,]*)\s*" + re.escape(ticker) + r"\b",
        re.IGNORECASE,
    )
    found: float | None = None
    for text in texts:
        if not text:
            continue
        for match in pattern.finditer(str(text)):
            value = parse_mined_amount(match.group(1), ticker)
            if value is not None:
                found = value
    return found


def _links(entry: dict[str, Any]) -> dict[str, str]:
    links: dict[str, str] = {}
    for field, label in (
        ("lien_ann", "ann"),
        ("lien_site", "site"),
        ("site", "site"),
        ("lien_explorer", "explorer"),
        ("explorer", "explorer"),
    ):
        value = _s(entry.get(field))
        if value and label not in links:
            links[label] = value
    return links


def _coin_entry(ticker: str, entry: dict[str, Any], markets: dict[str, Any]) -> dict[str, Any]:
    """Catalogue row for *ticker*, mixing the registry and the file's overrides."""
    reg = registry_for(ticker)
    override = markets.get(ticker) if isinstance(markets.get(ticker), dict) else {}

    cg_id = _s(override.get("cg") or reg.get("cg"))
    verified = bool(reg.get("cg_verified")) and cg_id == (reg.get("cg") or "")

    exchanges = override.get("exchanges")
    if not isinstance(exchanges, list):
        exchanges = reg.get("exchanges") or []

    name = _s(entry.get("coin")) or _s(reg.get("name")) or ticker
    algo = _s(entry.get("algo"))
    machine = _s(entry.get("machine"))
    note = _s(override.get("note") or reg.get("note"))

    return {
        "ticker": ticker,
        "name": name,
        "algo": algo,
        "kind": _s(reg.get("kind")) or guess_kind(algo, machine, note),
        "ann_url": _s(entry.get("lien_ann")) or None,
        "site_url": _s(entry.get("lien_site") or entry.get("site")) or None,
        "explorer_url": _s(entry.get("explorer") or entry.get("lien_explorer")) or None,
        "coingecko_id": cg_id or None,
        "coingecko_verified": verified,
        "exchanges": [e for e in exchanges if isinstance(e, dict)],
        "manual_price_usd": _float(override.get("price_usd")),
        "manual_price_eur": _float(override.get("price_eur")),
        "notes": note or None,
    }


def _machine_of(entry: dict[str, Any]) -> str:
    machine = _s(entry.get("machine"))
    if machine:
        return machine
    hardware = entry.get("hardware")
    if isinstance(hardware, dict):
        return _s(hardware.get("name"))
    return ""


def _campaign(
    key: str,
    entry: dict[str, Any],
    ticker: str,
    state: str,
    balances: dict[str, float],
    markets: dict[str, Any],
) -> dict[str, Any]:
    raw = _s(entry.get("mined")) or None
    amount = parse_mined_amount(raw, ticker) if raw else None

    balance = balances.get(ticker)
    balance_note = "solde live" if balance is not None else ""
    if balance is None and amount is not None and state == "active":
        balance, balance_note = amount, "montant miné (fichier d'état)"
    if balance is None:
        extracted = _balance_from_text(_s(entry.get("note")), ticker=ticker)
        if extracted is not None:
            balance, balance_note = extracted, "extrait de la note"

    address = _s(entry.get("adresse") or entry.get("wallet")) or None

    return {
        "legacy_key": key,
        "source": "legacy",
        "coin_ticker": ticker,
        "coin_name": _s(entry.get("coin") or registry_for(ticker).get("name") or ticker),
        "state": state,
        "start_iso": _s(entry.get("start_iso")) or None,
        "end_iso": _s(entry.get("end_iso")) or None,
        "mined_raw": raw,
        "mined_amount": amount,
        "mined_unit": ticker,
        "verdict": _s(entry.get("verdict")) or None,
        "machine": _machine_of(entry) or None,
        "pool": _s(entry.get("pool")) or None,
        "algo": _s(entry.get("algo")) or None,
        "wallet_address": address,
        "hashrate": _s(entry.get("hashrate")) or None,
        "links": _links(entry),
        "notes": _s(entry.get("notes") or entry.get("note")) or None,
        "last_balance": balance,
        "balance_note": balance_note,
    }


def _wallet(ticker: str, entry: dict[str, Any], pool: str = "") -> dict[str, Any] | None:
    address = _s(entry.get("adresse") or entry.get("wallet"))
    if not address:
        return None
    return {
        "coin_ticker": ticker,
        "address": address,
        "label": _s(entry.get("coin") or ticker),
        "location": _machine_of(entry),
        "backup_path": clean_backup_path(_s(entry.get("wallet_path"))),
        "pool": pool,
    }


def _live_balances(live: dict[str, Any]) -> dict[str, float]:
    balances: dict[str, float] = {}
    for key, obj in (live or {}).items():
        if not isinstance(obj, dict):
            continue
        ticker = _s(obj.get("ticker")).upper()
        value = _float(obj.get("balance"))
        if ticker and value is not None:
            balances[ticker] = value
    return balances


LIVE_TICKER_HINTS = (
    ("pearl", "PRL"),
    ("quantus", "QTC"),
    ("qhash", "QTC"),
    ("wam", "WAM"),
    ("korsh", "KSH"),
)


def _infer_ticker(obj: dict[str, Any]) -> str:
    """Live blocks often have no ``ticker``: the algo/pool/name tell it."""
    blob = " ".join(_s(obj.get(key)) for key in ("algo", "pool", "name", "coin")).lower()
    for hint, ticker in LIVE_TICKER_HINTS:
        if hint in blob:
            return ticker
    return ""


def _live_campaigns(status: dict[str, Any], live: dict[str, Any], known: set[str]) -> list[dict[str, Any]]:
    """Campaigns that only exist in the live file (Pearl/Luckypool, …)."""
    out: list[dict[str, Any]] = []
    for key, obj in (live or {}).items():
        if not isinstance(obj, dict) or key in ("rig", "pcr"):
            continue
        ticker = _s(obj.get("ticker")).upper() or _infer_ticker(obj)
        if not ticker or ticker in known:
            continue
        if not any(k in obj for k in ("balance", *HASHRATE_KEYS, "shares")):
            continue
        hashrate = ""
        for field in ("hashrate", "hashrate_ths", "hashrate_hs", "hashrate_khs", "hashrate_mhs"):
            if obj.get(field) not in (None, ""):
                hashrate = f"{obj.get(field)} {field.replace('hashrate', '').strip('_') or ''}".strip()
                break
        entry = {
            "coin": None,
            "ticker": ticker,
            "algo": obj.get("algo"),
            "machine": obj.get("name"),
            "pool": obj.get("pool"),
            "mined": None,
            "verdict": None,
            "hashrate": hashrate or None,
        }
        campaign = _campaign(f"live:{key}", entry, ticker, "active", _live_balances(live), status.get("markets") or {})
        campaign["hashrate"] = hashrate or None
        out.append(campaign)
    return out


def build_plan(
    status: dict[str, Any] | None,
    live: dict[str, Any] | None,
    source: str = "legacy",
) -> dict[str, Any]:
    """Turn both files into ``{coins, wallets, campaigns, warnings}`` (no DB access)."""
    status = status if isinstance(status, dict) else {}
    live = live if isinstance(live, dict) else {}
    markets = status.get("markets") if isinstance(status.get("markets"), dict) else {}

    warnings: list[str] = []
    balances = _live_balances(live)
    campaigns: list[dict[str, Any]] = []
    coins: dict[str, dict[str, Any]] = {}
    wallets: dict[tuple[str, str], dict[str, Any]] = {}

    def remember_coin(ticker: str, entry: dict[str, Any]) -> None:
        if ticker in coins:
            return
        coins[ticker] = _coin_entry(ticker, entry, markets)

    def remember_wallet(ticker: str, entry: dict[str, Any], pool: str = "") -> None:
        wallet = _wallet(ticker, entry, pool=pool)
        if wallet is None:
            return
        wallets.setdefault((wallet["coin_ticker"], wallet["address"]), wallet)

    # ── running campaigns of the status file ──────────────────────────
    for key in list(status.keys()):
        if key in NON_CAMPAIGN_KEYS:
            continue
        entry = status.get(key)
        if not isinstance(entry, dict):
            continue
        if not (key in ACTIVE_KEYS or key.endswith("_spec")):
            continue
        if entry.get("stopped"):
            continue
        ticker = _s(entry.get("ticker") or entry.get("coin")).upper()
        if not ticker:
            warnings.append(f"{key}: sans ticker, ignoré")
            continue
        campaigns.append(_campaign(key, entry, ticker, "active", balances, markets))
        remember_coin(ticker, entry)
        pool = "luckypool" if "luckypool" in _s(entry.get("pool")).lower() else ""
        remember_wallet(ticker, entry, pool=pool)

    # ── running campaigns only published in the live file (Pearl) ─────
    known = {c["coin_ticker"] for c in campaigns}
    for campaign in _live_campaigns(status, live, known):
        campaigns.append(campaign)
        ticker = campaign["coin_ticker"]
        coins.setdefault(ticker, _coin_entry(ticker, {}, markets))
        entry = next(
            (
                obj for obj in live.values()
                if isinstance(obj, dict)
                and (_s(obj.get("ticker")).upper() or _infer_ticker(obj)) == ticker
            ),
            {},
        )
        if entry:
            pool = "luckypool" if "luckypool" in _s(entry.get("pool")).lower() else ""
            remember_wallet(ticker, entry, pool=pool)

    # ── history ───────────────────────────────────────────────────────
    for index, entry in enumerate(status.get("history") or []):
        if not isinstance(entry, dict):
            continue
        ticker = _s(entry.get("ticker") or entry.get("coin")).upper()
        if not ticker:
            warnings.append(f"history[{index}]: sans ticker, ignoré")
            continue
        mined = _s(entry.get("mined"))
        if not mined and not _s(entry.get("start_iso")):
            warnings.append(f"history[{index}] ({ticker}): ni montant ni date, ignoré")
            continue
        state = "blocked" if re.search(r"bloqu", _s(entry.get("status")), re.IGNORECASE) else "ended"
        campaigns.append(_campaign(_campaign_key("history", ticker, entry), entry, ticker, state, balances, markets))
        remember_coin(ticker, entry)
        remember_wallet(ticker, entry)

    return {
        "source": source,
        "coins": list(coins.values()),
        "wallets": list(wallets.values()),
        "campaigns": campaigns,
        "warnings": warnings,
        "counts": {
            "coins": len(coins),
            "wallets": len(wallets),
            "campaigns_active": sum(1 for c in campaigns if c["state"] == "active"),
            "campaigns_history": sum(1 for c in campaigns if c["state"] != "active"),
        },
    }


def apply_plan(db, plan: dict[str, Any]) -> dict[str, int]:
    """Write the plan; idempotent (same keys → no new row)."""
    from app.models import Coin, MinedCampaign, Wallet

    counters = {
        "coins_created": 0,
        "coins_updated": 0,
        "wallets_created": 0,
        "wallets_updated": 0,
        "campaigns_created": 0,
        "campaigns_updated": 0,
    }
    stamp = _now()

    for item in plan.get("coins", []):
        row = db.query(Coin).filter(Coin.ticker == item["ticker"]).first()
        if row is None:
            db.add(Coin(**{k: v for k, v in item.items() if v is not None}))
            counters["coins_created"] += 1
            continue
        changed = False
        for field, value in item.items():
            if field == "ticker" or value in (None, "", [], {}):
                continue
            if getattr(row, field, None) != value:
                setattr(row, field, value)
                changed = True
        if changed:
            row.updated_at = stamp
            counters["coins_updated"] += 1
    db.flush()

    for item in plan.get("wallets", []):
        row = (
            db.query(Wallet)
            .filter(Wallet.coin_ticker == item["coin_ticker"], Wallet.address == item["address"])
            .first()
        )
        if row is None:
            db.add(Wallet(**item))
            counters["wallets_created"] += 1
            continue
        changed = False
        for field in ("label", "location", "backup_path", "pool"):
            value = item.get(field) or ""
            if value and getattr(row, field, "") != value:
                setattr(row, field, value)
                changed = True
        if changed:
            row.updated_at = stamp
            counters["wallets_updated"] += 1
    db.flush()

    for item in plan.get("campaigns", []):
        row = db.query(MinedCampaign).filter(MinedCampaign.legacy_key == item["legacy_key"]).first()
        if row is None:
            db.add(MinedCampaign(**item))
            counters["campaigns_created"] += 1
            continue
        changed = False
        for field, value in item.items():
            if field == "legacy_key":
                continue
            if getattr(row, field, None) != value:
                setattr(row, field, value)
                changed = True
        if changed:
            row.updated_at = stamp
            counters["campaigns_updated"] += 1

    db.commit()
    return counters


def preview(plan: dict[str, Any], limit: int = 40) -> dict[str, Any]:
    """Small, secret-free view of a plan (addresses are shortened)."""
    def short(address: str) -> str:
        return address if len(address) <= 18 else f"{address[:10]}…{address[-6:]}"

    return {
        "coins": [c["ticker"] for c in plan.get("coins", [])],
        "wallets": [
            {"coin": w["coin_ticker"], "address": short(w["address"]), "pool": w["pool"]}
            for w in plan.get("wallets", [])[:limit]
        ],
        "campaigns": [
            {
                "key": c["legacy_key"],
                "coin": c["coin_ticker"],
                "state": c["state"],
                "mined_amount": c["mined_amount"],
                "start_iso": c["start_iso"],
            }
            for c in plan.get("campaigns", [])[:limit]
        ],
    }


def dumps(plan: dict[str, Any]) -> str:
    """Debug helper — never used to persist (no secret ever enters a plan)."""
    return json.dumps(preview(plan), ensure_ascii=False, indent=1)
