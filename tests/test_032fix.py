"""Correctif 032-fix — prix par jour, démarrage non bloquant, diagnostic d'échec.

Toutes les valeurs sont fictives : ni adresse réelle, ni appel réseau (le
transport httpx est simulé, Docker est remplacé par un faux runner).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app import payments
from app.database import SessionLocal
from app.models import Coin, PoolPayment, PriceSample, Wallet
from updater.updater import Config, Updater, read_env_tag

COIN = "TSTP"
ADDRESS = "TSTWALLETPAYMENTS000000000000000000000002"
IMAGE = "ghcr.io/lostinthebugs/fossor"
TARGET = "2026.11.993"
OLD = "2026.11.992"

# 2025-10-31T08:40:00Z, jour de référence des paiements fictifs.
BASE_TS = 1761900000
DAY = 86400


def _wallet(db, ticker=COIN):
    wallet = Wallet(coin_ticker=ticker, address=ADDRESS, label="fictif", pool="luckypool")
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


def _coin(db, ticker=COIN, cg_id="fictif-coin"):
    db.add(Coin(ticker=ticker, name="Fictif", kind="gpu",
                coingecko_id=cg_id, coingecko_verified=True))
    db.commit()


def _payment(db, wallet, ts, tx):
    stamp = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    row = PoolPayment(
        wallet_id=wallet.id, coin_ticker=COIN, pool="luckypool", amount=1.0,
        received_at=stamp, tx=tx, address_hint=payments.mask_address(ADDRESS),
        source="luckypool",
        dedupe_key=payments.payment_key("luckypool", tx, stamp, 1.0),
    )
    db.add(row)
    db.commit()
    return row


# ── 1. Un prix par jour de réception ─────────────────────────────────
async def test_three_payments_on_three_days_produce_three_price_samples(monkeypatch):
    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)
    monkeypatch.setattr(payments, "PRICE_CALL_MIN_INTERVAL_S", 0.0)
    seen_dates: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/stats"):
            return httpx.Response(200, json={
                "config": {"coinUnits": 100000000},
                "stats": {"pool": {}},
            })
        if "/coins/fictif-coin/history" in str(request.url):
            date = request.url.params["date"]
            seen_dates.append(date)
            return httpx.Response(200, json={
                "market_data": {"current_price": {"eur": 10.0 + len(seen_dates)}},
            })
        return httpx.Response(200, json={
            "stats": {"wallet": ADDRESS},
            "payments": [
                {"timestamp": BASE_TS, "txHash": "jour-1", "amount": "100000000"},
                {"timestamp": BASE_TS - DAY, "txHash": "jour-2", "amount": "200000000"},
                {"timestamp": BASE_TS - 2 * DAY, "txHash": "jour-3", "amount": "300000000"},
            ],
        })

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(handler))
    db = SessionLocal()
    try:
        _coin(db)
        wallet = _wallet(db)
        result = await payments.sync_luckypool_wallet(db, wallet)
        assert result["ok"] and result["new"] == 3
        samples = db.query(PriceSample).filter(PriceSample.ticker == COIN).all()
        assert len(samples) == 3, "un échantillon par (coin, jour)"
        assert len({row.ts[:10] for row in samples}) == 3
        assert len(seen_dates) == 3, "trois jours, trois appels historiques"
        listed = payments.list_payments(db, coin=COIN)
        values = sorted(row["value_eur"] for row in listed["rows"])
        assert None not in values and len(set(values)) == 3, "chaque jour a son propre prix"
    finally:
        db.close()


# ── 2. Mise à niveau 031 → nouvelle version, avec des paiements ──────
async def test_upgrade_from_031_prices_each_payment_its_own_day(monkeypatch):
    """Base d'avant le correctif : 3 paiements, **un seul** prix historique."""
    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)
    monkeypatch.setattr(payments, "PRICE_CALL_MIN_INTERVAL_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        date = request.url.params["date"]
        return httpx.Response(200, json={
            "market_data": {"current_price": {"eur": 50.0 + int(date[:2])}},
        })

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(handler))
    db = SessionLocal()
    try:
        _coin(db)
        wallet = _wallet(db)
        first = _payment(db, wallet, BASE_TS - 2 * DAY, "up-1")
        _payment(db, wallet, BASE_TS - DAY, "up-2")
        third = _payment(db, wallet, BASE_TS, "up-3")
        db.add(PriceSample(
            ticker=COIN,
            ts=f"{first.received_at[:10]}T00:00:00+00:00",
            eur=1.0,
            source="coingecko-history",
        ))
        db.commit()
        assert db.query(PriceSample).count() == 1

        calls = await payments.backfill_missing_prices(db, budget=10)
        samples = db.query(PriceSample).filter(PriceSample.ticker == COIN).all()
        assert calls == 2, "seuls les deux jours sans prix sont demandés"
        assert len(samples) == 3 and len({row.ts[:10] for row in samples}) == 3
        listed = {row["tx"]: row for row in payments.list_payments(db, coin=COIN)["rows"]}
        assert all(row["value_eur"] for row in listed.values())
        assert listed["up-3"]["value_eur"] != listed["up-2"]["value_eur"]
        assert third.tx in listed
    finally:
        db.close()


# ── 3. Démarrage avec 11 paiements et CoinGecko lent ─────────────────
async def test_startup_with_eleven_payments_answers_under_five_seconds(monkeypatch, client):
    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)
    monkeypatch.setattr(payments, "STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(payments, "PRICE_CALL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(payments, "MAX_HISTORY_PRICES_FIRST_SYNC", 3)

    async def slow(request: httpx.Request) -> httpx.Response:
        if "/coins/fictif-coin/history" in str(request.url):
            await asyncio.sleep(10)
            return httpx.Response(200, json={"market_data": {"current_price": {"eur": 7.0}}})
        return httpx.Response(200, json={"market_data": {"current_price": {"eur": 7.0}}})

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(slow))
    db = SessionLocal()
    try:
        _coin(db)
        wallet = _wallet(db)
        for index in range(11):
            _payment(db, wallet, BASE_TS - index * DAY, f"demarrage-{index}")
        assert db.query(PoolPayment).count() == 11
    finally:
        db.close()

    payments.start()
    try:
        started = time.monotonic()
        response = await client.get("/api/mined/payments")
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert elapsed < 5.0, f"réponse en {elapsed:.2f} s"
        assert len(response.json()["rows"]) == 11
        # La synchronisation tourne encore en tâche de fond, sans bloquer l'API.
        assert payments._task is not None and not payments._task.done()
    finally:
        payments.stop()


# ── 4. L'updater conserve logs et inspect d'un conteneur qui échoue ───
class FailingRunner:
    """Faux Docker : la cible ne répond jamais, l'ancienne version répond."""

    def __init__(self, healthy=(), report=(
        "== docker logs --tail 200 fossor\nligne-de-log-fictive\n"
        "== docker inspect fossor\nstatus=exited exit=1 oom=false health=none"
    )):
        self.healthy = set(healthy)
        self.report = report
        self.reports: list[str] = []

    def pull(self, reference):
        return True, "pulled"

    def image_present(self, reference):
        return True

    def compose_up(self, restore=None):
        return True, "recreated"

    def container_running(self):
        return True

    def start_helper(self, argv):
        return True, "amorce"

    def health_once(self, expected):
        return (expected in self.healthy), f"version {expected}"

    def container_report(self, service, tail=200):
        self.reports.append(service)
        return self.report


def _config(tmp_path: Path) -> Config:
    compose = tmp_path / "compose"
    compose.mkdir()
    (compose / ".env").write_text(
        f"FOSSOR_MASTER_KEY=abc\nFOSSOR_IMAGE_TAG={OLD}\n", encoding="utf-8"
    )
    data = tmp_path / "data"
    (data / "update").mkdir(parents=True)
    (data / "backups").mkdir()
    return Config(
        update_dir=data / "update",
        compose_dir=compose,
        project="fossor-test",
        image=IMAGE,
        backup_dir=data / "backups",
        data_dir=data,
        health_timeout=1,
        health_poll=0.05,
        poll=2,
    )


def _backup(config: Config) -> Path:
    path = config.backup_dir / f"pre-{TARGET}-20261116T030000Z"
    path.mkdir()
    con = sqlite3.connect(path / "fossor.db")
    con.execute("create table machines (id integer primary key, name text)")
    con.commit()
    con.close()
    return path


def _request(target: str = TARGET):
    return {"target": target, "image": IMAGE,
            "requested_at": "2026-11-23T03:00:00+00:00", "nonce": "fix032123456"}


def test_failed_update_keeps_container_logs_and_inspect(tmp_path):
    config = _config(tmp_path)
    _backup(config)
    live = config.data_dir / "fossor.db"
    con = sqlite3.connect(live)
    con.execute("create table machines (id integer primary key, name text, extra integer)")
    con.commit()
    con.close()

    runner = FailingRunner(healthy={OLD})
    updater = Updater(config, runner)
    assert updater.handle(_request()) == "rolled_back"
    assert read_env_tag(config.env_path.read_text(encoding="utf-8")) == OLD

    files = sorted(config.update_dir.glob(f"failed-{TARGET}-*.log"))
    assert len(files) == 1, "un seul journal d'échec par tentative"
    content = files[0].read_text(encoding="utf-8")
    assert "ligne-de-log-fictive" in content
    assert "status=exited exit=1 oom=false health=none" in content
    assert "docker logs --tail 200" in content and "docker inspect" in content
    assert runner.reports == ["fossor"]

    history = [json.loads(line) for line in config.history_path.read_text().splitlines()]
    logs = [item for item in history if item["event"] == "container_logs"]
    assert len(logs) == 1
    assert logs[0]["path"] == str(files[0])
    assert logs[0]["target"] == TARGET


def test_health_attempts_are_journalised(tmp_path):
    config = _config(tmp_path)
    _backup(config)
    runner = FailingRunner(healthy=set())
    updater = Updater(config, runner)
    updater.handle(_request())

    history = [json.loads(line) for line in config.history_path.read_text().splitlines()]
    attempts = [item for item in history if item["event"] == "health_attempt"]
    assert attempts, "chaque tentative de santé laisse une trace"
    assert all("url" in item and "duration_ms" in item for item in attempts)
    assert all(item["ok"] is False for item in attempts)
    assert any("version" in str(item["detail"]) for item in attempts)
    assert attempts[0]["url"].endswith("/api/version")
