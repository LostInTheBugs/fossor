"""Luckypool per-wallet endpoint (``/api/stats_address``) — anonymised real shape."""

import json
from pathlib import Path

import httpx
import pytest

from app import pools

FIX = Path(__file__).parent / "fixtures"
FAKE_WALLET = "prl1qexample0000000000000000000000000000000000000000000000000"


def _stats() -> dict:
    return json.loads((FIX / "luckypool_stats.json").read_text())


def _address() -> dict:
    return json.loads((FIX / "luckypool_stats_address.json").read_text())


def test_host_from_ticker_or_wallet_setting():
    assert pools.luckypool_host("PRL") == "pearl.luckypool.io"
    assert pools.luckypool_host("QTC") == "qtc.luckypool.io"
    assert pools.luckypool_host("XYZ", "pearl") == "pearl.luckypool.io"
    assert pools.luckypool_host("XYZ", "pearl.luckypool.io") == "pearl.luckypool.io"
    with pytest.raises(ValueError):
        pools.luckypool_host("", "")


def test_coin_units_verified_by_payment_threshold():
    """coinUnits (1e8) must agree with minPaymentThreshold = 1 PRL."""
    parsed = pools.parse_stats(_stats())
    assert parsed["units_per_coin"] == 100_000_000
    assert parsed["min_payment_threshold"] == 1.0
    assert parsed["units_verified"] is True
    assert parsed["pool_hashrate_hs"] == pytest.approx(1.1591155136429195e19, rel=1e-12)


def test_stats_address_converts_atomic_units():
    record = pools.parse_stats_address(_address(), units_per_coin=1e8, coin="PRL")
    assert record["available"] is True
    assert record["paid"] == 1474.74697035
    assert record["unpaid"] == 1.18839497
    assert record["immature"] == 1.84470694
    assert record["threshold"] == 5.0
    assert record["hashrate_avg_24h_hs"] == 862647001329628
    assert [worker["name"] for worker in record["workers"]] == ["rig-a", "gpu-b"]
    assert record["workers"][0]["agent"] == "SRBMiner-MULTI/3.8.3"
    assert record["workers"][1]["hashrate_hs"] == 303318820114030


def test_wrong_divisor_would_change_the_balances():
    """A 1e8 vs 1e9 divisor is a ×10 error: the threshold check catches it."""
    record = pools.parse_stats_address(_address(), units_per_coin=1e9, coin="PRL")
    assert record["paid"] == 147.474697035
    parsed = pools.parse_stats(_stats())
    assert parsed["min_payment_threshold"] == 1.0  # only 1e8 gives 1 PRL


async def test_balance_calls_stats_then_stats_address(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/api/stats"):
            return httpx.Response(200, json=_stats())
        return httpx.Response(200, json=_address())

    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)
    monkeypatch.setattr(pools, "TRANSPORT", httpx.MockTransport(handler))

    record = await pools.luckypool_balance(FAKE_WALLET, coin="PRL")
    assert record["ok"] is True and record["available"] is True
    assert record["paid"] == 1474.74697035
    assert record["units_verified"] is True
    assert seen == [
        "https://pearl.luckypool.io/api/stats",
        f"https://pearl.luckypool.io/api/stats_address?address={FAKE_WALLET}",
    ]


async def test_wallet_pool_host_selects_the_subdomain(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_stats() if request.url.path.endswith("/api/stats") else _address())

    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)
    monkeypatch.setattr(pools, "TRANSPORT", httpx.MockTransport(handler))

    await pools.luckypool_balance(FAKE_WALLET, coin="QTC", pool_host="qtc")
    assert seen[0] == "https://qtc.luckypool.io/api/stats"


async def test_rejected_address_is_an_error_not_a_zero_balance(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "Address is required"})

    monkeypatch.delenv("FOSSOR_OFFLINE", raising=False)
    monkeypatch.setattr(pools, "TRANSPORT", httpx.MockTransport(handler))

    record = await pools.luckypool_balance(FAKE_WALLET, coin="PRL")
    assert record["ok"] is False
    assert "error" in record and record.get("paid") is None
