"""Étape 2b — catalogue, import legacy, prix, soldes de pool, écran « Tokens minés ».

Every HTTP call is mocked (``httpx.MockTransport``): the legacy files come from
``tests/fixtures`` (anonymised copies of the real ones) and no test ever reaches
a rig, .195 or an exchange.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import legacy_import, pools, prices
from app.market_registry import clean_backup_path, parse_mined_amount

FIX = Path(__file__).resolve().parent / "fixtures"


def _status() -> dict:
    return json.loads((FIX / "mining-status.json").read_text(encoding="utf-8"))


def _live() -> dict:
    return json.loads((FIX / "mining-live.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Default: no network at all; the price tests opt out explicitly."""
    monkeypatch.setenv("FOSSOR_OFFLINE", "1")
    prices.clear_cache()
    yield
    prices.clear_cache()


def _db():
    from app.database import SessionLocal

    return SessionLocal()


# ── Parsing ───────────────────────────────────────────────────────────
class TestParseMined:
    @pytest.mark.parametrize("text,ticker,expected", [
        ("6 949,67 JTM en chaine (…)= 6 999,33 JTM", "JTM", 6999.33),
        ("2 379,25 LMT", "LMT", 2379.25),
        ("0 JTM", "JTM", 0.0),
        ("4300,00132350 BIG (11 adresses, solde au 15/11 12:30Z)", "BIG", 4300.0013235),
        ("59 BTC (59 blocs solo : 44 mûrs + 15 immatures) — non listé", "BTC", 59.0),
        ("4,7463 NACR (2 payouts on-chain)", "NACR", 4.7463),
        ("64 QDAY (mûrs, wallet dédié — non listé)", "QDAY", 64.0),
        ("0 CMFD (miner n'a jamais démarré)", "CMFD", 0.0),
        ("", "BIG", None),
        (None, "BIG", None),
        ("aucun chiffre ici", "BIG", None),
    ])
    def test_amounts(self, text, ticker, expected):
        assert parse_mined_amount(text, ticker) == expected

    def test_backup_path_keeps_paths_only(self):
        raw = (
            "VM:/home/miner/wam/backup/wam-wallet-backup-20260913.dat + copie "
            "~/wallets/wam/ — seed/password dans la VM"
        )
        cleaned = clean_backup_path(raw)
        assert "/home/miner/wam/backup/wam-wallet-backup-20260913.dat" in cleaned
        assert "~/wallets/wam/" in cleaned
        assert "seed" not in cleaned.lower()
        assert "password" not in cleaned.lower()


# ── Import legacy ─────────────────────────────────────────────────────
class TestLegacyImport:
    async def test_dry_run_then_import_then_reimport(self, client):
        payload = {"status": _status(), "live": _live(), "dry_run": True}

        dry = await client.post("/api/import/legacy", json=payload)
        assert dry.status_code == 200, dry.text
        dry_body = dry.json()
        assert dry_body["dry_run"] is True
        assert dry_body["applied"] is None
        assert dry_body["counts"]["campaigns_active"] >= 3
        assert dry_body["counts"]["campaigns_history"] >= 5
        # dry_run wrote nothing
        from app.models import Coin, MinedCampaign, Wallet

        db = _db()
        try:
            assert db.query(Coin).count() == 0
            assert db.query(Wallet).count() == 0
            assert db.query(MinedCampaign).count() == 0
        finally:
            db.close()

        real = await client.post("/api/import/legacy", json={**payload, "dry_run": False})
        assert real.status_code == 200, real.text
        applied = real.json()["applied"]
        assert applied["campaigns_created"] == dry_body["counts"]["campaigns_active"] + dry_body["counts"]["campaigns_history"]
        assert applied["coins_created"] >= 4
        assert applied["wallets_created"] >= 4

        again = await client.post("/api/import/legacy", json={**payload, "dry_run": False})
        assert again.status_code == 200
        counters = again.json()["applied"]
        assert counters["campaigns_created"] == 0
        assert counters["coins_created"] == 0
        assert counters["wallets_created"] == 0

    async def test_upload_endpoint(self, client):
        status_raw = (FIX / "mining-status.json").read_bytes()
        live_raw = (FIX / "mining-live.json").read_bytes()
        response = await client.post(
            "/api/import/legacy/upload",
            files={
                "status_file": ("mining-status.json", status_raw, "application/json"),
                "live_file": ("mining-live.json", live_raw, "application/json"),
            },
            data={"dry_run": "true"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is True
        assert body["sources"]["status"] == "mining-status.json"
        assert body["counts"]["coins"] >= 4

    async def test_url_requires_configured_base(self, client):
        response = await client.post("/api/import/legacy", json={"status_url": "http://192.0.2.195:8090/mining-status.json"})
        assert response.status_code == 400
        assert "URL de base" in response.json()["detail"]

    async def test_url_must_target_the_two_files(self, client, monkeypatch):
        monkeypatch.setenv("FOSSOR_LEGACY_BASE_URL", "http://192.0.2.195:8090")
        response = await client.post("/api/import/legacy", json={"status_url": "http://192.0.2.195:8090/mining.html"})
        assert response.status_code == 400
        assert "autorisé" in response.json()["detail"]


# ── GET /api/mined ────────────────────────────────────────────────────
class TestMinedApi:
    async def test_structure_totals_and_values(self, client, monkeypatch):
        await client.post("/api/import/legacy", json={"status": _status(), "live": _live(), "dry_run": False})

        # A balance on the Pearl campaign gives the total banner something real.
        from app.models import MinedCampaign

        db = _db()
        try:
            pearl = db.query(MinedCampaign).filter(MinedCampaign.coin_ticker == "PRL").first()
            assert pearl is not None
            pearl.last_balance = 12.5
            db.commit()
        finally:
            db.close()

        monkeypatch.delenv("FOSSOR_OFFLINE")
        prices.clear_cache()
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            wanted = request.url.params.get("ids", "").split(",")
            frozen = json.loads((FIX / "coingecko_price.json").read_text())
            return httpx.Response(200, json={k: v for k, v in frozen.items() if k in wanted})

        monkeypatch.setattr(prices, "TRANSPORT", httpx.MockTransport(handler))

        response = await client.get("/api/mined")
        assert response.status_code == 200, response.text
        body = response.json()

        assert set(body) >= {"as_of", "total", "campaigns", "history", "counts"}
        assert body["counts"]["campaigns"] >= 3
        assert body["counts"]["history"] >= 5
        assert body["total"]["eur"] == pytest.approx(12.5 * 0.0039, abs=1e-4)
        assert body["total"]["usd"] == pytest.approx(12.5 * 0.0042, abs=1e-4)
        assert body["total"]["unpriced_campaigns"] >= 1  # WAM and friends have no price

        # only the verified ids are queried
        assert calls and "pearl-2" in calls[0].url.params.get("ids", "")
        assert "wam" not in calls[0].url.params.get("ids", "")

        campaign = next(c for c in body["campaigns"] if c["ticker"] == "PRL")
        assert campaign["price_eur"] == 0.0039
        assert campaign["value_eur"] is not None
        assert campaign["listed"] is True
        assert campaign["exchanges"][0]["name"]
        assert campaign["balance"] == 12.5

        wam = next(c for c in body["campaigns"] if c["ticker"] == "WAM")
        assert wam["listed"] is False
        assert wam["value_eur"] is None
        assert wam["exchanges"][0]["dex"] is True

        history = body["history"]
        assert history[0]["start_iso"] >= history[-1]["start_iso"]
        big = next(h for h in history if h["ticker"] == "BIG")
        assert big["mined_amount"] == pytest.approx(4300.0013235)
        assert big["verdict"]

    async def test_no_secret_and_no_real_identifier(self, client):
        await client.post("/api/import/legacy", json={"status": _status(), "live": _live(), "dry_run": False})
        response = await client.get("/api/mined")
        text = response.text.lower()
        for forbidden in ("seed", "password", "passphrase", "private_key"):
            assert forbidden not in text

        # Only the addresses of the (anonymised) fixtures may appear.
        fixture_addresses = {
            wallet["address"]
            for wallet in legacy_import.build_plan(_status(), _live())["wallets"]
        }
        import re

        for address in re.findall(r"prl1[a-z0-9]+", response.text, re.IGNORECASE):
            assert address in fixture_addresses

        wallets = await client.get("/api/wallets")
        assert wallets.status_code == 200
        for key in ("seed", "password", "passphrase"):
            assert key not in wallets.text.lower()


# ── Prix ──────────────────────────────────────────────────────────────
class TestPrices:
    async def test_cache_avoids_second_call(self, monkeypatch):
        monkeypatch.delenv("FOSSOR_OFFLINE")
        prices.clear_cache()
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"pearl-2": {"usd": 1.5, "eur": 1.4, "usd_24h_change": 3.0}})

        monkeypatch.setattr(prices, "TRANSPORT", httpx.MockTransport(handler))

        first = await prices.fetch_prices(["pearl-2"])
        second = await prices.fetch_prices(["pearl-2"])
        assert first["pearl-2"]["usd"] == 1.5
        assert second["pearl-2"]["usd"] == 1.5
        assert len(calls) == 1  # 5 min cache

        forced = await prices.fetch_prices(["pearl-2"], force=True)
        assert forced["pearl-2"]["eur"] == 1.4
        assert len(calls) == 2

    async def test_failure_keeps_cache_and_never_raises(self, monkeypatch):
        monkeypatch.delenv("FOSSOR_OFFLINE")
        prices.clear_cache()

        def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"pearl-2": {"usd": 2.0, "eur": 1.8}})

        monkeypatch.setattr(prices, "TRANSPORT", httpx.MockTransport(ok))
        assert (await prices.fetch_prices(["pearl-2"], force=True))["pearl-2"]["usd"] == 2.0

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(prices, "TRANSPORT", httpx.MockTransport(boom))
        assert (await prices.fetch_prices(["pearl-2"], force=True))["pearl-2"]["usd"] == 2.0


# ── Pool Luckypool ────────────────────────────────────────────────────
class TestLuckypool:
    def test_parse_fixture_declares_wallet_figures_unavailable(self):
        payload = json.loads((FIX / "luckypool_stats.json").read_text())
        record = pools.parse_luckypool(payload, address="prl1qexample", coin="PRL")
        assert record["pool"] == "luckypool"
        assert record["available"] is False
        assert record["paid"] is None and record["unpaid"] is None and record["immature"] is None
        assert record["threshold"] is not None and record["threshold"] > 0
        assert "indisponibles" in record["note"]
        assert record["raw"]["pool_miners"] > 0

    async def test_balance_uses_public_endpoint(self, monkeypatch):
        monkeypatch.delenv("FOSSOR_OFFLINE")
        payload = json.loads((FIX / "luckypool_stats.json").read_text())
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=payload)

        monkeypatch.setattr(pools, "TRANSPORT", httpx.MockTransport(handler))
        record = await pools.luckypool_balance("prl1qexample", coin="PRL")
        assert record["ok"] is True
        assert seen == [pools.LUCKYPOOL_STATS_URL]
        assert record["source_url"] == pools.LUCKYPOOL_STATS_URL

    async def test_refresh_stores_pool_row(self, client, monkeypatch):
        await client.post("/api/import/legacy", json={"status": _status(), "live": _live(), "dry_run": False})
        # The Pearl campaign carries no address in the files: the wallet is added
        # by hand (as Fred does) and the pool card must attach to it.
        created = await client.post("/api/wallets", json={
            "coin_ticker": "PRL", "address": "prl1qpearlfortest", "pool": "luckypool",
        })
        assert created.status_code == 201, created.text

        payload = json.loads((FIX / "luckypool_stats.json").read_text())

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        monkeypatch.delenv("FOSSOR_OFFLINE")
        monkeypatch.setattr(pools, "TRANSPORT", httpx.MockTransport(handler))
        monkeypatch.setattr(prices, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(200, json={})))
        response = await client.post("/api/mined/refresh")
        assert response.status_code == 200, response.text
        report = response.json()["pools"]
        assert any(entry["ticker"] == "PRL" and entry["ok"] for entry in report)

        body = (await client.get("/api/mined")).json()
        pearl = next(c for c in body["campaigns"] if c["ticker"] == "PRL")
        assert pearl["pool_balance"] is not None
        assert pearl["pool_balance"]["available"] is False
        assert pearl["pool_balance"]["threshold"] is not None


# ── Catalogue / wallets ───────────────────────────────────────────────
class TestCatalogue:
    async def test_crud_coins_and_wallets(self, client):
        created = await client.post("/api/coins", json={"ticker": "prl", "name": "Pearl", "kind": "gpu"})
        assert created.status_code == 201, created.text
        assert created.json()["ticker"] == "PRL"

        duplicate = await client.post("/api/coins", json={"ticker": "PRL"})
        assert duplicate.status_code == 409

        patched = await client.patch("/api/coins/PRL", json={"manual_price_eur": 0.5, "notes": "test"})
        assert patched.status_code == 200
        assert patched.json()["manual_price_eur"] == 0.5

        wallet = await client.post("/api/wallets", json={
            "coin_ticker": "PRL",
            "address": "prl1qaddressfortest",
            "label": "principal",
            "location": "rig de test",
            "backup_path": "/srv/backups/prl.dat",
            "pool": "luckypool",
        })
        assert wallet.status_code == 201, wallet.text
        assert wallet.json()["pool"] == "luckypool"

        again = await client.post("/api/wallets", json={"coin_ticker": "PRL", "address": "prl1qaddressfortest"})
        assert again.status_code == 409

        unknown_coin = await client.post("/api/wallets", json={"coin_ticker": "NOPE", "address": "x"})
        assert unknown_coin.status_code == 400

        listed = await client.get("/api/wallets")
        assert listed.status_code == 200 and len(listed.json()) == 1

        assert (await client.delete("/api/coins/PRL")).status_code == 409  # wallet still linked
        assert (await client.delete("/api/wallets/" + str(wallet.json()["id"]))).status_code == 200
        assert (await client.delete("/api/coins/PRL")).status_code == 200

    async def test_seed_is_refused_before_any_write(self, client):
        response = await client.post("/api/coins", json={"ticker": "BIG", "seed": "abandon abandon"})
        assert response.status_code == 422
        assert "seed" in response.text

        wallet = await client.post("/api/wallets", json={
            "coin_ticker": "PRL", "address": "prl1qx", "password": "hunter2",
        })
        assert wallet.status_code == 422

        from app.models import Coin, Wallet

        db = _db()
        try:
            assert db.query(Coin).count() == 0
            assert db.query(Wallet).count() == 0
        finally:
            db.close()


# ── Plan builder (pure) ───────────────────────────────────────────────
class TestPlan:
    def test_plan_carries_no_path_prose_and_no_duplicate_keys(self):
        plan = legacy_import.build_plan(_status(), _live())
        keys = [c["legacy_key"] for c in plan["campaigns"]]
        assert len(keys) == len(set(keys))
        for wallet in plan["wallets"]:
            assert "seed" not in wallet["backup_path"].lower()
            assert "password" not in wallet["backup_path"].lower()
        tickers = {c["coin_ticker"] for c in plan["campaigns"] if c["state"] == "active"}
        assert {"PRL", "BIG", "WAM", "KSH"} <= tickers
