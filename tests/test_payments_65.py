"""Étape 6.5 — paiements de pool, blocs solo et export comptable.

Toutes les réponses sont fictives et suivent les formats observés : aucune
adresse réelle, aucun appel réseau.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import history, payments
from app.database import SessionLocal
from app.models import (
    Coin,
    PoolBalanceSample,
    PoolPayment,
    PriceSample,
    SoloBlock,
    Wallet,
)

COIN = "TST"
ADDRESS = "TSTWALLETPAYMENTS000000000000000000000001"


def _wallet(db, ticker=COIN, pool="luckypool", address=ADDRESS):
    wallet = Wallet(coin_ticker=ticker, address=address, label="fictif", pool=pool)
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


# ── Parseur Luckypool ─────────────────────────────────────────────────
def test_luckypool_parser_reads_documented_payment_fields():
    payload = {
        "payments": [
            {
                "timestamp": 1762000000000,  # ms, comme le bundle public
                "txHash": "fictive-tx-0002",
                "amount": "250000000",  # 2,5 TST en unités atomiques
                "fee": "1000000",
                "payees": 1,
            },
            {
                "timestamp": 1761900000,  # s
                "txHash": "fictive-tx-0001",
                "amount": "100000000",
                "fee": "0",
                "payees": 1,
            },
            {"timestamp": 1761800000, "txHash": "sans-montant"},
        ]
    }
    rows = payments.parse_luckypool_payments(payload, units_per_coin=100_000_000, coin="TST")
    assert [row["tx"] for row in rows] == ["fictive-tx-0001", "fictive-tx-0002"]
    assert rows[0]["amount"] == 1.0
    assert rows[1]["amount"] == 2.5
    assert rows[0]["received_at"] == "2025-10-31T08:40:00+00:00"
    assert rows[0]["pool"] == "luckypool"


def test_luckypool_parser_reads_flat_timestamp_tx_amount_strings():
    """Forme réelle du pool : liste plate ``timestamp:txHash:amount``, sans objet."""
    payload = {
        "payments": [
            "1762000000:" + "a" * 64 + ":250000000",
            "1761900000:" + "b" * 64 + ":100000000",
            "1761800000:" + "c" * 64 + ":1",
            "tronque:" + "e" * 64,
            "inv-ts:" + "f" * 64,
        ]
    }
    rows = payments.parse_luckypool_payments(payload, units_per_coin=100_000_000, coin="TST")
    assert len(rows) == 3, "trois lignes valides, aucune inventée"
    assert [row["tx"] for row in rows] == ["c" * 64, "b" * 64, "a" * 64]
    assert rows[0]["amount"] == 0.00000001
    assert rows[1]["amount"] == 1.0
    assert rows[2]["amount"] == 2.5
    assert rows[2]["received_at"] == "2025-11-01T12:26:40+00:00"
    assert rows[0]["pool"] == "luckypool" and rows[0]["coin"] == "TST"
    assert rows[2]["fee"] == 0.0


def test_luckypool_parser_reads_flat_fee_and_keeps_dict_form():
    payload = {
        "payments": [
            "1761700000:" + "d" * 64 + ":300000000:50000",
            {"timestamp": 1762000000000, "txHash": "fictive-tx-0002", "amount": "250000000"},
        ]
    }
    rows = payments.parse_luckypool_payments(payload, units_per_coin=100_000_000, coin="TST")
    assert len(rows) == 2, "les deux formes coexistent"
    assert [row["tx"] for row in rows] == ["d" * 64, "fictive-tx-0002"]
    assert rows[0]["amount"] == 3.0
    assert rows[1]["amount"] == 2.5
    assert rows[0]["fee"] == 0.0005
    assert rows[1]["fee"] == 0.0


def test_address_is_truncated_everywhere():
    assert payments.mask_address(ADDRESS).startswith("TSTWAL")
    assert ADDRESS not in payments.mask_address(ADDRESS)
    assert payments.mask_address("court") == "cour…"


# ── Idempotence et incréments ────────────────────────────────────────
def test_payments_are_not_duplicated_and_incremental_sync_only_adds_new():
    db = SessionLocal()
    try:
        wallet = _wallet(db)
        first = [
            {"coin": COIN, "pool": "luckypool", "amount": 1.0,
             "received_at": "2026-11-01T10:00:00+00:00", "tx": "tx-a", "source": "luckypool"},
            {"coin": COIN, "pool": "luckypool", "amount": 2.0,
             "received_at": "2026-11-02T10:00:00+00:00", "tx": "tx-b", "source": "luckypool"},
        ]
        assert len(payments.record_payments(db, wallet, first)) == 2
        db.commit()
        assert len(payments.record_payments(db, wallet, first)) == 0, "pas de doublon"
        db.commit()

        newer = first + [
            {"coin": COIN, "pool": "luckypool", "amount": 3.0,
             "received_at": "2026-11-03T10:00:00+00:00", "tx": "tx-c", "source": "luckypool"},
        ]
        added = payments.record_payments(db, wallet, newer)
        db.commit()
        assert [row.tx for row in added] == ["tx-c"]
        assert db.query(PoolPayment).count() == 3
        assert db.query(PoolPayment).first().address_hint == payments.mask_address(ADDRESS)
    finally:
        db.close()


def test_missing_tx_falls_back_to_date_and_amount_key():
    db = SessionLocal()
    try:
        wallet = _wallet(db, ticker="NOP")
        row = {"coin": "NOP", "pool": "luckypool", "amount": 4.0,
               "received_at": "2026-11-04T10:00:00+00:00", "tx": "", "source": "luckypool"}
        assert len(payments.record_payments(db, wallet, [row])) == 1
        db.commit()
        assert len(payments.record_payments(db, wallet, [row])) == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_luckypool_sync_uses_mock_stats_address(monkeypatch):
    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/stats"):
            return httpx.Response(200, json={
                "config": {"coinUnits": 100000000, "minPaymentThreshold": 100000000},
                "stats": {"pool": {}},
            })
        return httpx.Response(200, json={
            "stats": {"wallet": ADDRESS, "paid": "300000000"},
            "payments": [
                {"timestamp": 1762000000, "txHash": "mock-1", "amount": "100000000"},
                {"timestamp": 1762100000, "txHash": "mock-2", "amount": "200000000"},
            ],
        })

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(handler))
    db = SessionLocal()
    try:
        wallet = _wallet(db)
        result = await payments.sync_luckypool_wallet(db, wallet)
        assert result["ok"] and result["new"] == 2
        again = await payments.sync_luckypool_wallet(db, wallet)
        assert again["ok"] and again["new"] == 0, "la synchro incrémentale ne reprend que le neuf"
        assert db.query(PoolPayment).count() == 2
    finally:
        db.close()


# ── Blocs solo ───────────────────────────────────────────────────────
def test_solo_blocks_are_detected_from_fictive_rpc_payload():
    payload = [
        {"category": "immature", "amount": 12.5, "blockheight": 4200,
         "time": 1762000000, "txid": "solo-a"},
        {"category": "generate", "amount": 12.5, "blockheight": 4200,
         "time": 1762000100, "txid": "solo-a"},
        {"category": "receive", "amount": 1.0, "blockheight": 4201,
         "time": 1762000200, "txid": "pas-un-bloc"},
    ]
    rows = payments.parse_solo_transactions(payload, coin="KSH", node="rig-solo")
    assert len(rows) == 1
    assert rows[0]["height"] == 4200 and rows[0]["reward"] == 12.5
    assert rows[0]["node"] == "rig-solo"


def test_solo_blocks_are_stored_once():
    db = SessionLocal()
    try:
        rows = payments.parse_solo_transactions([
            {"category": "generate", "amount": 8.0, "blockheight": 77,
             "time": 1762000000, "txid": "solo-x"},
        ], coin="BIG", node="ryzen-solo")
        assert len(payments.record_solo_blocks(db, rows)) == 1
        db.commit()
        assert len(payments.record_solo_blocks(db, rows)) == 0
        assert db.query(SoloBlock).count() == 1
    finally:
        db.close()


# ── Prix historique CoinGecko ────────────────────────────────────────
@pytest.mark.asyncio
async def test_historical_price_is_cached_from_coingecko(monkeypatch):
    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/coins/tst-coin/history" in str(request.url)
        assert "date=01-11-2026" in str(request.url)
        return httpx.Response(200, json={"market_data": {"current_price": {"eur": 1.25}}})

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(handler))
    db = SessionLocal()
    try:
        db.add(Coin(ticker="HIS", name="Historique", kind="gpu",
                    coingecko_id="tst-coin", coingecko_verified=True))
        db.commit()
        value = await payments.ensure_historical_price(db, "HIS", "2026-11-01T10:00:00+00:00")
        assert value == 1.25
        cached = db.query(PriceSample).filter(PriceSample.ticker == "HIS").one()
        assert cached.source == "coingecko-history"
        # Deuxième appel : le cache répond, aucun réseau.
        assert await payments.ensure_historical_price(db, "HIS", "2026-11-01T12:00:00+00:00") == 1.25
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unlisted_coin_has_no_invented_price(monkeypatch):
    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)
    db = SessionLocal()
    try:
        db.add(Coin(ticker="NOL", name="Non listé", kind="cpu", coingecko_verified=False))
        db.commit()
        assert await payments.ensure_historical_price(db, "NOL", "2026-11-01T10:00:00+00:00") is None
        assert db.query(PriceSample).count() == 0
    finally:
        db.close()


# ── Export CSV et sources ────────────────────────────────────────────
def test_export_has_pool_solo_and_pool_delta_rows_without_wallet_or_ip():
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        pool_wallet = _wallet(db)
        delta_wallet = _wallet(db, ticker="DLT", pool="rplant",
                               address="DLTWALLETDELTA00000000000000000000002")
        db.add(PriceSample(ticker=COIN, ts=_iso(now - timedelta(days=3)), eur=2.0, source="coingecko"))
        db.add(PriceSample(ticker="KSH", ts=_iso(now - timedelta(days=3)), eur=0.5, source="coingecko"))
        db.commit()
        payments.record_payments(db, pool_wallet, [{
            "coin": COIN, "pool": "luckypool", "amount": 2.5,
            "received_at": _iso(now - timedelta(days=2)), "tx": "tx-fictive-pool",
            "source": "luckypool",
        }])
        payments.record_solo_blocks(db, [{
            "coin": "KSH", "height": 900, "reward": 4.0,
            "found_at": _iso(now - timedelta(days=1)), "tx": "tx-fictive-solo",
            "node": "rig-solo-fictif",
        }])
        db.add(PoolBalanceSample(wallet_id=delta_wallet.id,
                                 ts=_iso(now - timedelta(hours=4)), paid=10.0))
        db.add(PoolBalanceSample(wallet_id=delta_wallet.id,
                                 ts=_iso(now - timedelta(hours=2)), paid=11.0))
        db.commit()

        payload = history.csv_export(db)
        text = payload.decode("utf-8-sig")
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=";"))
        assert rows[0] == history.EXPORT_HEADER
        by_source = {row[5]: row for row in rows[1:]}
        assert set(by_source) == {"pool", "solo", "pool-delta"}
        assert by_source["pool"][3] == "5", "2,5 × 2 €"
        assert by_source["solo"][3] == "2", "4 × 0,5 €"
        assert by_source["pool-delta"][3] == "", "aucun prix : vide, jamais inventé"
        assert by_source["pool-delta"][4] == "inconnu"
        assert ADDRESS not in text
        assert "DLTWALLETDELTA" not in text
        assert "192.0.2." not in text
        assert "tx-fictive-solo" in text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_payments_api_returns_truncated_address(client):
    db = SessionLocal()
    try:
        wallet = _wallet(db)
        payments.record_payments(db, wallet, [{
            "coin": COIN, "pool": "luckypool", "amount": 1.5,
            "received_at": "2026-11-02T10:00:00+00:00", "tx": "tx-api",
            "source": "luckypool",
        }])
        db.commit()
    finally:
        db.close()
    response = await client.get("/api/mined/payments")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["rows"][0]["address"] == payments.mask_address(ADDRESS)
    assert ADDRESS not in response.text


# ── Blocs solo par SSH (6.5-fin) ─────────────────────────────────────
import asyncio
import json
import logging
from pathlib import Path


def _solo_probe():
    return {
        "key": "bigcoin",
        "cli": "/home/fictif/bigcoin-mine/bigcoin/bigcoin-cli",
        "datadir": "/home/fictif/bigcoin-mine/bigcoin/data",
        "pid": 4242,
        "coin": "bigcoin",
    }


def _host(db, name="hote-fictif"):
    from app.models import Machine

    machine = Machine(name=name, host="192.0.2.11", ssh_user="fictif", machine_kind="host")
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def test_solo_blocks_come_from_the_node_cli_over_ssh(monkeypatch):
    calls = []

    async def fake_probes(machine):
        return [_solo_probe()]

    payload = {
        "result": [
            {"category": "immature", "amount": 0.5, "blockheight": 900,
             "time": 1762000000, "txid": "fictif-a"},
            {"category": "generate", "amount": 0.5, "blockheight": 900,
             "time": 1762000000, "txid": "fictif-a"},
            {"category": "generate", "amount": 0.5, "blockheight": 901,
             "time": 1762000100, "txid": "fictif-b"},
        ]
    }

    async def fake_ssh(machine, command, **kwargs):
        calls.append(command)
        return {"ok": True, "stdout": "### SOLO bigcoin\n" + json.dumps(payload) + "\n"}

    async def no_price(*args, **kwargs):
        return None

    monkeypatch.setattr(payments, "solo_node_probes", fake_probes)
    monkeypatch.setattr("app.ssh_client.ssh_run_command", fake_ssh)
    monkeypatch.setattr(payments, "ensure_historical_price", no_price)

    with SessionLocal() as db:
        machine = _host(db, "hote-fictif-cli")
        report = asyncio.run(payments.sync_solo_nodes(db, machines=[machine]))
        blocks = (
            db.query(SoloBlock)
            .filter(SoloBlock.coin_ticker == "BIG", SoloBlock.height.in_((900, 901)))
            .all()
        )
        assert {block.height for block in blocks} == {900, 901}
        assert all(block.reward == 0.5 for block in blocks)
        assert report["ok"] is True
        assert report["nodes"][0]["new"] == 2
        assert len(calls) == 1
        # même binaire et même datadir que la sonde ### NODE
        assert "/home/fictif/bigcoin-mine/bigcoin/bigcoin-cli" in calls[0]
        assert 'D=/home/fictif/bigcoin-mine/bigcoin/data' in calls[0]
        assert '-datadir="$D" listtransactions' in calls[0]
        assert "listtransactions '*' 200 0" in calls[0]
        assert "password" not in calls[0] and "http" not in calls[0]


def test_solo_sync_stores_no_rpc_account():
    source = Path(payments.__file__).read_text()
    assert "solo_rpc_nodes" not in source
    assert not hasattr(payments, "SOLO_RPC_KEY")


def test_solo_node_unreachable_is_reported(monkeypatch, caplog):
    async def fake_probes(machine):
        return [_solo_probe()]

    async def fake_ssh(machine, command, **kwargs):
        return {"ok": False, "error": "Connexion refusée par le nœud fictif"}

    monkeypatch.setattr(payments, "solo_node_probes", fake_probes)
    monkeypatch.setattr("app.ssh_client.ssh_run_command", fake_ssh)

    with SessionLocal() as db:
        machine = _host(db, "hote-fictif-injoignable")
        with caplog.at_level(logging.WARNING, logger="fossor.payments"):
            report = asyncio.run(payments.sync_solo_nodes(db, machines=[machine]))
        assert report["ok"] is False
        assert report["nodes"][0]["ok"] is False
        assert "refusée" in report["nodes"][0]["error"]
        assert "blocs solo indisponibles" in caplog.text
