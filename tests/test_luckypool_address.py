"""6.5-quater — l'adresse envoyée à ``/api/stats_address`` est nue, sans worker.

Un profil SRBMiner stocke parfois ``adresse+diff`` (ou ``adresse.worker``,
``adresse/worker``) : le pool répond alors ``404 {"error": "Address not found"}``
alors que l'adresse mine.  La normalisation est partagée par le solde et les
paiements.
"""

import asyncio
import json
from pathlib import Path

import httpx

from app import payments, pools
from app.database import SessionLocal

ADDRESS = "prl1qfixtureaddress0000000000000000000000000000000000"
# Forme observée en prod : adresse bech32 + suffixe de difficulté après « + ».
SUFFIX = "Ab3dEf9Kq2LmN8pRsTu4VwXyZ7QrStUvWxYz"
PLUS_WORKER = ADDRESS + "+" + SUFFIX
DOT_WORKER = ADDRESS + ".athlon-3000g"
SLASH_WORKER = ADDRESS + "/ryzen"
STRATUM = "stratum+tcp://pearl-eu2.luckypool.io:3360"
FIXTURE = Path(__file__).parent / "fixtures" / "luckypool_stats_address.json"


def _payload():
    if FIXTURE.exists():
        return json.loads(FIXTURE.read_text())
    return {"stats": {"balance": 1, "paid": 0}, "payments": []}


def test_normalize_strips_every_stratum_decoration():
    assert pools.normalize_address(ADDRESS) == ADDRESS
    assert pools.normalize_address(PLUS_WORKER) == ADDRESS
    assert pools.normalize_address(DOT_WORKER) == ADDRESS
    assert pools.normalize_address(SLASH_WORKER) == ADDRESS
    assert pools.normalize_address("  " + ADDRESS + "  ") == ADDRESS
    assert pools.normalize_address(ADDRESS + " " + SUFFIX) == ADDRESS
    assert pools.normalize_address("") == ""
    assert pools.normalize_address(None) == ""
    assert pools.normalize_address("pearl.short") == "pearl.short"  # libellé, pas une adresse


def test_address_url_never_carries_the_worker():
    for decorated in (PLUS_WORKER, DOT_WORKER, SLASH_WORKER, " " + PLUS_WORKER + " "):
        url = pools.address_url("pearl.luckypool.io", decorated)
        assert url == f"https://pearl.luckypool.io/api/stats_address?address={ADDRESS}"
        assert SUFFIX not in url and "athlon" not in url and "ryzen" not in url


def test_balance_sends_the_bare_address(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/api/stats":
            return httpx.Response(200, json={"config": {"coinUnits": 100000000}})
        if not str(request.url).endswith("address=" + ADDRESS):
            return httpx.Response(404, json={"error": "Address not found"})
        return httpx.Response(200, json=_payload())

    monkeypatch.setattr(pools, "TRANSPORT", httpx.MockTransport(handler))
    result = asyncio.run(pools.luckypool_balance(PLUS_WORKER, "PRL", STRATUM))
    assert result["ok"] is True, result
    assert result["host"] == "pearl.luckypool.io"
    address_calls = [url for url in seen if "stats_address" in url]
    assert address_calls == ["https://pearl.luckypool.io/api/stats_address?address=" + ADDRESS]


def test_sync_sends_the_bare_address(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/api/stats":
            return httpx.Response(200, json={"config": {"coinUnits": 100000000}})
        if not str(request.url).endswith("address=" + ADDRESS):
            return httpx.Response(404, json={"error": "Address not found"})
        return httpx.Response(200, json={"payments": []})

    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(handler))
    wallet = payments.PaymentWallet("PRL", PLUS_WORKER, "luckypool", STRATUM)
    with SessionLocal() as db:
        result = asyncio.run(payments.sync_luckypool_wallet(db, wallet))
    assert result["ok"] is True, result
    address_calls = [url for url in seen if "stats_address" in url]
    assert address_calls == ["https://pearl.luckypool.io/api/stats_address?address=" + ADDRESS]
