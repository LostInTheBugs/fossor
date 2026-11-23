"""Historique des paiements de pool et des blocs solo (§4.16, étape 6.5).

Sources observées :

* Luckypool — la page ``/assets/index-*.js`` de ``pearl.luckypool.io`` lit
  ``payments`` dans la réponse ``GET /api/stats_address?address=…`` (store
  ``pool.payments``) et affiche pour chaque ligne ``timestamp``, ``txHash``,
  ``amount``, ``fee``, ``payees``. Aucune autre route de paiements n'est
  appelée par le client : l'historique complet est donc déjà dans
  ``stats_address``. Les montants sont en unités atomiques (÷ ``coinUnits``).
* RPlant, pool Korsh et les autres pools de Fossor n'exposent pas de route de
  paiements documentée dans ce dépôt : aucun connecteur n'est inventé, ils
  restent en repli ``pool-delta``.
* Blocs solo — le parseur accepte une réponse ``listtransactions`` de nœud
  Bitcoin-like (``category`` = ``generate`` ou ``immature``, ``amount``,
  ``blockheight``, ``time``/``timeReceived``, ``txid``), lue en lecture seule.

Aucune adresse complète n'est écrite en base : ``address_hint`` est tronquée
à la réception et c'est le seul champ exposé à l'UI et au CSV.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("fossor.payments")

PAYMENT_POOLS = {"luckypool"}
COINGECKO_HISTORY_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/history"
SYNC_INTERVAL_KEY = "pool_payments_interval"
SYNC_LAST_KEY = "pool_payments_last_sync"
DEFAULT_SYNC_INTERVAL_S = 3600
MIN_SYNC_INTERVAL_S = 300
MAX_SYNC_INTERVAL_S = 86400
MAX_PAYMENTS_PER_SYNC = 500
SOLO_MAX_NODES = 5
SOLO_MAX_TRANSACTIONS = 200
MAX_HISTORY_PRICES_PER_SYNC = 10
REQUEST_TIMEOUT_S = 20.0

TRANSPORT = None  # monkeypatched in tests
_task: asyncio.Task | None = None


def offline() -> bool:
    return os.environ.get("FOSSOR_OFFLINE") == "1"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_address(address: str | None) -> str:
    """``prl1abc…wxyz`` — jamais l'adresse complète (§4.16)."""
    text = str(address or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        return text[:4] + "…"
    return text[:6] + "…" + text[-4:]


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_iso(value: Any) -> str | None:
    """Timestamp Luckypool (s ou ms) ou chaîne ISO → ISO UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e11:  # millisecondes
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return to_iso(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def payment_key(pool: str, tx: str, received_at: str, amount: float) -> str:
    """Clé d'unicité : pool + tx, sinon pool + date + montant."""
    clean = (tx or "").strip()
    if clean:
        return f"{pool}|tx:{clean}"[:200]
    return f"{pool}|at:{received_at}|{float(amount):.12f}"[:200]


def _units_from_config(payload: dict[str, Any]) -> float:
    from app.pools import parse_stats

    return float(parse_stats(payload).get("units_per_coin") or 100_000_000)


def parse_luckypool_payments(
    payload: dict[str, Any],
    units_per_coin: float = 100_000_000.0,
    coin: str = "PRL",
) -> list[dict[str, Any]]:
    """Shape the ``payments`` array of ``/api/stats_address``.

    Champs observés dans le bundle public : ``timestamp``, ``txHash``,
    ``amount``, ``fee``, ``payees``. Un montant absent ou nul est ignoré ;
    aucune ligne n'est inventée.
    """
    payload = payload if isinstance(payload, dict) else {}
    rows = payload.get("payments")
    if not isinstance(rows, list):
        return []
    units = float(units_per_coin or 100_000_000.0)
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        atomic = _float(row.get("amount"))
        if atomic is None:
            continue
        amount = round(atomic / units, 12)
        if amount <= 0:
            continue
        received_at = to_iso(row.get("timestamp") or row.get("time") or row.get("created"))
        if not received_at:
            continue
        tx = str(row.get("txHash") or row.get("tx") or row.get("transaction") or "").strip()
        out.append({
            "coin": (coin or "PRL").upper(),
            "pool": "luckypool",
            "amount": amount,
            "received_at": received_at,
            "tx": tx,
            "fee": round((_float(row.get("fee")) or 0.0) / units, 12),
            "source": "luckypool",
        })
    out.sort(key=lambda item: (item["received_at"], item["tx"]))
    return out


def record_payments(db: Any, wallet: Any, rows: list[dict[str, Any]]) -> list[Any]:
    """Insert only unseen rows (unique ``dedupe_key``), in a bounded batch."""
    from app.models import PoolPayment

    hint = mask_address(getattr(wallet, "address", ""))
    added: list[Any] = []
    for row in rows[:MAX_PAYMENTS_PER_SYNC]:
        key = payment_key(row["pool"], row["tx"], row["received_at"], row["amount"])
        exists = db.query(PoolPayment.id).filter(PoolPayment.dedupe_key == key).first()
        if exists:
            continue
        item = PoolPayment(
            wallet_id=getattr(wallet, "id", None),
            coin_ticker=row["coin"],
            pool=row["pool"],
            amount=row["amount"],
            received_at=row["received_at"],
            tx=row.get("tx") or "",
            address_hint=hint,
            source=row.get("source") or row["pool"],
            dedupe_key=key,
        )
        db.add(item)
        added.append(item)
    if added:
        db.flush()
    return added


async def ensure_historical_price(db: Any, ticker: str, received_at: str) -> float | None:
    """Cache the CoinGecko price of the payment's day in ``price_samples``.

    Only verified ``coingecko_id`` coins are queried, exactly like the live
    price client. A coin without a listing stays unpriced — no invented value.
    """
    from app.models import Coin, PriceSample

    key = (ticker or "").upper()
    if not key or offline():
        return None
    day = (received_at or "")[:10]
    if not day:
        return None
    cached = (
        db.query(PriceSample)
        .filter(PriceSample.ticker == key, PriceSample.ts.like(f"{day}%"))
        .order_by(PriceSample.ts.asc())
        .first()
    )
    if cached is not None and cached.eur is not None:
        return float(cached.eur)
    coin = db.query(Coin).filter(Coin.ticker == key).first()
    if coin is None or not coin.coingecko_id or not coin.coingecko_verified:
        return None
    date_param = datetime.fromisoformat(day + "T00:00:00+00:00").strftime("%d-%m-%Y")
    url = COINGECKO_HISTORY_URL.format(coin_id=quote(coin.coingecko_id, safe=""))
    try:
        import httpx

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=TRANSPORT) as client:
            response = await client.get(url, params={"date": date_param, "localization": "false"})
        response.raise_for_status()
        payload = response.json() or {}
        eur = _float(((payload.get("market_data") or {}).get("current_price") or {}).get("eur"))
    except Exception as exc:
        logger.warning("CoinGecko historique indisponible (%s, %s): %s", key, day, exc)
        return None
    if eur is None or eur <= 0:
        return None
    db.add(PriceSample(ticker=key, ts=f"{day}T00:00:00+00:00", eur=eur, source="coingecko-history"))
    db.commit()
    return eur


async def sync_luckypool_wallet(db: Any, wallet: Any) -> dict[str, Any]:
    """Read the full payment history once, then only rows newer than the cursor."""
    from app import notifications, pools
    from app.models import PoolPayment, PriceSample

    if offline():
        return {"ok": False, "skipped": True, "error": "réseau désactivé (FOSSOR_OFFLINE=1)"}
    address = (getattr(wallet, "address", "") or "").strip()
    if not address:
        return {"ok": False, "error": "adresse de wallet absente"}
    host = pools.luckypool_host(wallet.coin_ticker, getattr(wallet, "pool_host", "") or "")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=TRANSPORT) as client:
            stats = await pools._get(client, pools.stats_url(host))
            units = _units_from_config(stats)
            payload = await pools._get(client, pools.address_url(host, address))
    except Exception as exc:
        logger.warning("Luckypool paiements indisponibles (%s): %s", host, exc)
        return {"ok": False, "error": str(exc), "host": host}

    parsed = parse_luckypool_payments(payload, units_per_coin=units, coin=wallet.coin_ticker)
    cursor = (
        db.query(PoolPayment.received_at)
        .filter(PoolPayment.wallet_id == wallet.id)
        .order_by(PoolPayment.received_at.desc())
        .first()
    )
    newest = cursor[0] if cursor else None
    fresh = [row for row in parsed if newest is None or row["received_at"] > newest]
    added = record_payments(db, wallet, fresh)
    for item in added:
        notifications.enqueue(
            db,
            "payment_received",
            f"Paiement reçu sur {item.coin_ticker} : +{item.amount:.8f} {item.coin_ticker} "
            f"(wallet {item.address_hint}).",
        )
    prices = 0
    for item in added:
        if prices >= MAX_HISTORY_PRICES_PER_SYNC:
            break
        priced = (
            db.query(PriceSample.id)
            .filter(PriceSample.ticker == item.coin_ticker, PriceSample.ts <= item.received_at)
            .first()
        )
        if priced is None:
            await ensure_historical_price(db, item.coin_ticker, item.received_at)
            prices += 1
    from app.ai_config import set_setting

    set_setting(db, f"pool_payments_synced:{wallet.id}", _stamp())
    db.commit()
    return {
        "ok": True,
        "host": host,
        "seen": len(parsed),
        "new": len(added),
        "cursor": newest,
        "units_per_coin": units,
    }


# ── Blocs solo ────────────────────────────────────────────────────────
def parse_solo_transactions(
    payload: Any,
    coin: str = "KSH",
    node: str = "",
) -> list[dict[str, Any]]:
    """Blocs trouvés dans une réponse ``listtransactions`` (lecture seule).

    Seules les catégories ``generate`` et ``immature`` comptent. La hauteur
    est obligatoire : sans elle, la ligne ne peut pas être dédupliquée et est
    ignorée plutôt que devinée.
    """
    rows = payload if isinstance(payload, list) else (payload or {}).get("result") or []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("category") or "").lower() not in ("generate", "immature"):
            continue
        height = row.get("blockheight", row.get("height", row.get("blockindex")))
        try:
            height = int(height)
        except (TypeError, ValueError):
            continue
        reward = _float(row.get("amount"))
        if reward is None:
            reward = _float(row.get("reward"))
        if reward is None:
            continue
        found_at = to_iso(row.get("time") or row.get("timeReceived") or row.get("blocktime"))
        if not found_at:
            continue
        out.append({
            "coin": (coin or "").upper(),
            "height": height,
            "reward": abs(round(reward, 12)),
            "found_at": found_at,
            "tx": str(row.get("txid") or "").strip(),
            "node": node,
        })
    # Une même hauteur peut apparaître en « immature » puis « generate ».
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for row in sorted(out, key=lambda item: item["found_at"]):
        unique[(row["coin"], row["height"])] = row
    return sorted(unique.values(), key=lambda item: (item["found_at"], item["height"]))


def record_solo_blocks(db: Any, rows: list[dict[str, Any]]) -> list[Any]:
    from app.models import SoloBlock

    added: list[Any] = []
    for row in rows:
        exists = (
            db.query(SoloBlock.id)
            .filter(SoloBlock.coin_ticker == row["coin"], SoloBlock.height == row["height"])
            .first()
        )
        if exists:
            continue
        item = SoloBlock(
            coin_ticker=row["coin"],
            height=row["height"],
            reward=row["reward"],
            found_at=row["found_at"],
            node=row.get("node") or "",
            tx=row.get("tx") or "",
        )
        db.add(item)
        added.append(item)
    if added:
        db.flush()
    return added


def solo_coin_ticker(probe: dict[str, Any]) -> str:
    """Ticker of a detected solo node (``bigcoin`` -> ``BIG``)."""
    from app.external_miners import COIN_ALIASES, DEFAULT_COIN_HINTS

    key = str(probe.get("coin") or probe.get("key") or "").strip().lower()
    ticker = str(DEFAULT_COIN_HINTS.get(key) or "").upper() or key.upper()
    return str(COIN_ALIASES.get(ticker, ticker)).upper()


async def solo_node_probes(machine: Any) -> list[dict[str, Any]]:
    """Solo nodes of *machine*, detected by the same ``ps`` probe as collect.

    Nothing is configured by hand: the list is exactly what the CPU collector
    already sees (daemon, script, ``*-cli``), never a settings field.
    """
    from app.cpu_stats import plan_cpu_probes
    from app.ssh_client import ps_miners_command, ssh_run_command

    raw = await ssh_run_command(machine, ps_miners_command(), audit_kind="solo-transactions")
    if not raw.get("ok"):
        raise RuntimeError(str(raw.get("error") or "sonde ps indisponible"))
    return list(plan_cpu_probes(str(raw.get("stdout") or "")).get("nodes") or [])


def parse_cli_response(stdout: str, key: str) -> Any | None:
    """JSON printed after ``### SOLO <key>``; ``None`` if the CLI said nothing."""
    import json

    marker = f"### SOLO {key}"
    body: list[str] = []
    lines = (stdout or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() != marker:
            continue
        for row in lines[index + 1:]:
            if row.startswith("### "):
                break
            body.append(row)
        break
    text = "\n".join(body).strip()
    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


async def sync_solo_nodes(db: Any, machines: list[Any] | None = None) -> dict[str, Any]:
    """``listtransactions`` through SSH, on the nodes the collector detected.

    Same binary and same datadir as the ``### NODE`` probe, read-only and
    bounded. No RPC account, no wallet key and no password is stored or sent:
    the SSH key of the machine is the only credential. A missing CLI or a
    refused right is written to the journal and reported, never worked around.
    """
    from app.models import Machine, PriceSample
    from app.ssh_client import cpu_probe_command, ssh_run_command

    rows = machines if machines is not None else (
        db.query(Machine).filter(Machine.machine_kind == "host").all()
    )
    if not rows:
        return {"ok": False, "skipped": True, "error": "aucune machine SSH"}
    if offline():
        return {"ok": False, "skipped": True, "error": "réseau désactivé (FOSSOR_OFFLINE=1)"}

    report: list[dict[str, Any]] = []
    for machine in rows:
        label = str(getattr(machine, "name", "") or getattr(machine, "host", "") or "")
        try:
            probes = await solo_node_probes(machine)
        except Exception as exc:
            logger.warning("sonde solo indisponible (%s) : %s", label, exc)
            report.append({"machine": label, "ok": False, "error": str(exc)})
            continue
        if not probes:
            report.append({"machine": label, "ok": True, "skipped": True, "nodes": 0})
            continue
        for probe in probes[:SOLO_MAX_NODES]:
            key = str(probe.get("key") or "")
            node = f"{label}:{key}"
            try:
                command = cpu_probe_command(
                    {"nodes": [probe]}, extra="listtransactions", tx_count=SOLO_MAX_TRANSACTIONS
                )
                raw = await ssh_run_command(machine, command, audit_kind="solo-transactions")
                if not raw.get("ok"):
                    raise RuntimeError(str(raw.get("error") or "SSH en échec"))
                payload = parse_cli_response(str(raw.get("stdout") or ""), key)
                if payload is None:
                    raise RuntimeError(
                        "CLI du nœud sans réponse listtransactions (binaire ou droits manquants)"
                    )
                found = parse_solo_transactions(
                    payload, coin=solo_coin_ticker(probe), node=node
                )
                added = record_solo_blocks(db, found)
                for item in added:
                    db.commit()
                    priced = (
                        db.query(PriceSample.id)
                        .filter(PriceSample.ticker == item.coin_ticker, PriceSample.ts <= item.found_at)
                        .first()
                    )
                    if priced is None:
                        await ensure_historical_price(db, item.coin_ticker, item.found_at)
                db.commit()
                report.append({"machine": label, "node": key, "ok": True,
                               "seen": len(found), "new": len(added)})
            except Exception as exc:
                db.rollback()
                logger.warning("blocs solo indisponibles (%s) : %s", node, exc)
                report.append({"machine": label, "node": key, "ok": False, "error": str(exc)})
    ok = all(item.get("ok") or item.get("skipped") for item in report)
    return {"ok": ok, "nodes": report}


# ── Orchestration ─────────────────────────────────────────────────────
async def sync_all(db: Any | None = None) -> dict[str, Any]:
    """One bounded pass over Luckypool wallets + configured solo nodes."""
    from app.ai_config import set_setting
    from app.database import SessionLocal
    from app.models import Wallet

    owned = db is None
    db = db or SessionLocal()
    try:
        wallets = db.query(Wallet).filter(Wallet.pool.in_(tuple(PAYMENT_POOLS))).all()
        report: list[dict[str, Any]] = []
        for wallet in wallets:
            try:
                report.append({"wallet_id": wallet.id, **await sync_luckypool_wallet(db, wallet)})
            except Exception as exc:  # one broken wallet never stops the pass
                db.rollback()
                logger.warning("paiements wallet %s en échec: %s", wallet.id, exc)
                report.append({"wallet_id": wallet.id, "ok": False, "error": str(exc)})
        solo = await sync_solo_nodes(db)
        ok = all(item.get("ok") or item.get("skipped") for item in report) and (
            solo.get("ok") or solo.get("skipped")
        )
        set_setting(db, SYNC_LAST_KEY, _stamp())
        db.commit()
        interval = sync_interval(db)
        try:
            from app import observability

            observability.mark_routine(
                db,
                "pool-payments",
                bool(ok),
                f"{len(report)} wallet(s), {sum(int(r.get('new') or 0) for r in report)} nouveau(x) ; "
                f"solo : {solo.get('error') or 'ok'}",
                interval_s=interval,
            )
        except Exception:  # pragma: no cover - a status never breaks the sync
            pass
        return {"ok": bool(ok), "wallets": report, "solo": solo, "interval_s": interval}
    finally:
        if owned:
            db.close()


def list_payments(
    db: Any,
    coin: str | None = None,
    pool: str | None = None,
    source: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Rows for the « Paiements reçus » section — truncated address only."""
    from app.history import _price_at
    from app.models import PoolPayment, SoloBlock

    out: list[dict[str, Any]] = []
    cache: dict[str, list[tuple[str, float, str]]] = {}
    if not source or source in ("pool", "luckypool"):
        query = db.query(PoolPayment)
        if coin:
            query = query.filter(PoolPayment.coin_ticker == coin.upper())
        if pool:
            query = query.filter(PoolPayment.pool == pool.lower())
        if start:
            query = query.filter(PoolPayment.received_at >= start)
        if end:
            query = query.filter(PoolPayment.received_at < end)
        for row in query.order_by(PoolPayment.received_at.desc()).limit(max(1, min(limit, 500))):
            price, price_source = _price_at(db, row.coin_ticker, row.received_at, cache)
            out.append({
                "kind": "pool",
                "coin": row.coin_ticker,
                "pool": row.pool,
                "amount": row.amount,
                "received_at": row.received_at,
                "tx": row.tx,
                "address": row.address_hint,
                "source": row.source,
                "value_eur": round(float(row.amount) * price, 6) if price is not None else None,
                "price_source": price_source if price is not None else "inconnu",
            })
    if not source or source == "solo":
        query = db.query(SoloBlock)
        if coin:
            query = query.filter(SoloBlock.coin_ticker == coin.upper())
        if start:
            query = query.filter(SoloBlock.found_at >= start)
        if end:
            query = query.filter(SoloBlock.found_at < end)
        for row in query.order_by(SoloBlock.found_at.desc()).limit(max(1, min(limit, 500))):
            price, price_source = _price_at(db, row.coin_ticker, row.found_at, cache)
            out.append({
                "kind": "solo",
                "coin": row.coin_ticker,
                "pool": "solo",
                "amount": row.reward,
                "received_at": row.found_at,
                "tx": row.tx,
                "address": row.node,
                "source": "solo",
                "height": row.height,
                "value_eur": round(float(row.reward) * price, 6) if price is not None else None,
                "price_source": price_source if price is not None else "inconnu",
            })
    out.sort(key=lambda item: item["received_at"], reverse=True)
    coins = sorted({row["coin"] for row in out})
    return {"rows": out[: max(1, min(limit, 500))], "count": len(out), "coins": coins}


# ── Scheduler borné ───────────────────────────────────────────────────
def sync_interval(db: Any) -> int:
    from app.ai_config import get_setting

    try:
        value = int(get_setting(db, SYNC_INTERVAL_KEY, DEFAULT_SYNC_INTERVAL_S))
    except (TypeError, ValueError):
        value = DEFAULT_SYNC_INTERVAL_S
    if value <= 0:
        return 0
    return max(MIN_SYNC_INTERVAL_S, min(MAX_SYNC_INTERVAL_S, value))


async def _loop() -> None:
    from app.database import SessionLocal

    while True:
        interval = DEFAULT_SYNC_INTERVAL_S
        try:
            db = SessionLocal()
            try:
                interval = sync_interval(db)
            finally:
                db.close()
            if interval <= 0:
                return
            await sync_all()
        except Exception:  # pragma: no cover - defensive
            logger.warning("synchronisation des paiements en échec", exc_info=True)
        await asyncio.sleep(interval if interval > 0 else DEFAULT_SYNC_INTERVAL_S)


def start() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.create_task(_loop())
    except RuntimeError:  # pragma: no cover - no running loop at import time
        _task = None


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
    _task = None
