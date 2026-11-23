"""6.5-ter — la synchro interroge le site Luckypool, jamais le stratum."""

import asyncio

import httpx
import pytest

from app import coins, payments, pools
from app.database import SessionLocal

STRATUM = "stratum+tcp://pearl-eu2.luckypool.io:3360"
ADDRESS = "prl1qfixtureaddress0000000000000000000000000000000000"


def test_stratum_farm_maps_to_the_observed_site():
    assert pools.luckypool_host("PRL", STRATUM) == "pearl.luckypool.io"
    assert pools.luckypool_host("PRL", "pearl-eu2.luckypool.io:3360") == "pearl.luckypool.io"
    assert pools.luckypool_host("PRL", "pearl-eu1.luckypool.io") == "pearl.luckypool.io"
    assert pools.luckypool_host("PRL", "pearl-us1.luckypool.io:3360") == "pearl.luckypool.io"
    assert pools.luckypool_host("PRL", "pearl.luckypool.io") == "pearl.luckypool.io"
    assert pools.luckypool_host("KSH", "korsh.luckypool.io") == "korsh.luckypool.io"
    assert pools.luckypool_host("WAM", "wamcoin.luckypool.io") == "wamcoin.luckypool.io"
    assert coins.LUCKYPOOL_API_HOSTS["pearl-eu2"] == "pearl.luckypool.io"


def test_sync_calls_the_web_site_not_the_stratum(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/api/stats":
            return httpx.Response(200, json={"config": {"coinUnits": 100000000}})
        return httpx.Response(200, json={"payments": []})

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(handler))
    wallet = payments.PaymentWallet("PRL", ADDRESS, "luckypool", "pearl-eu2.luckypool.io")
    with SessionLocal() as db:
        result = asyncio.run(payments.sync_luckypool_wallet(db, wallet))
    assert result["ok"] is True, result
    assert result["host"] == "pearl.luckypool.io"
    assert any(url.startswith("https://pearl.luckypool.io/api/stats_address") for url in seen)
    assert all("pearl-eu2" not in url for url in seen)


def test_unknown_host_raises_before_any_request(monkeypatch):
    calls = []

    def handler(request):  # pragma: no cover - ne doit jamais tourner
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="inconnu"):
        pools.luckypool_host("PRL", "pearl-eu2.example.org:3360")
    wallet = payments.PaymentWallet("PRL", ADDRESS, "luckypool", "pearl-eu2.example.org")
    result = asyncio.run(payments.sync_luckypool_wallet(None, wallet))
    assert result["ok"] is False and "inconnu" in result["error"]
    assert calls == []
