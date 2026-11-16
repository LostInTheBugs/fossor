"""Correction 2c — a campaign whose wallet has a pool balance must be valued.

Rule (§4.1 bis / correction (a)): ``balance`` = unpaid + immature (what the pool
still owes), ``balance_note`` says so, and the already-paid amount is shown
separately without being counted again in the value.
"""

from __future__ import annotations

import pytest

from app import prices


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("FOSSOR_OFFLINE", "1")
    prices.clear_cache()
    yield
    prices.clear_cache()


class TestPearlCampaignValue:
    def test_unit_balance_is_unpaid_plus_immature(self, setup_test_env):
        from app.models import MinedCampaign, PoolBalance
        from app.routers.mined import _campaign_json

        campaign = MinedCampaign(
            legacy_key="current",
            coin_ticker="PRL",
            coin_name="Pearl",
            state="active",
            balance_note="",
        )
        pool = PoolBalance(
            wallet_id=1,
            pool="luckypool",
            coin_ticker="PRL",
            available=True,
            paid=1474.747,
            unpaid=1.54,
            immature=1.49,
            threshold=5.0,
        )
        out = _campaign_json(campaign, None, {"eur": 0.5, "usd": 0.55}, None, pool)

        assert out["balance"] == 3.03
        assert out["balance_note"] == "sur le pool (non payé + immature)"
        # Paid PRL already sits on the wallet: displayed, never valued again.
        assert out["pool_balance"]["paid"] == 1474.747
        assert out["value_eur"] == pytest.approx(1.515)
        assert out["value_usd"] == pytest.approx(1.6665)

    async def test_api_total_includes_the_pool_owed_balance(self, client):
        from app.database import SessionLocal
        from app.models import Coin, MinedCampaign, PoolBalance, Wallet

        db = SessionLocal()
        try:
            db.add(Coin(ticker="PRL", name="Pearl", manual_price_usd=0.55, manual_price_eur=0.5))
            wallet = Wallet(coin_ticker="PRL", address="prl1testonly", pool="luckypool",
                            pool_host="pearl.luckypool.io")
            db.add(wallet)
            db.add(MinedCampaign(legacy_key="current", coin_ticker="PRL", coin_name="Pearl",
                                 state="active", balance_note=""))
            db.commit()
            db.add(PoolBalance(wallet_id=wallet.id, pool="luckypool", coin_ticker="PRL",
                               available=True, paid=1474.747, unpaid=1.54, immature=1.49,
                               threshold=5.0))
            db.commit()
        finally:
            db.close()

        response = await client.get("/api/mined")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"]["eur"] == pytest.approx(1.515)
        campaign = body["campaigns"][0]
        assert campaign["balance"] == 3.03
        assert campaign["value_eur"] == pytest.approx(1.515)
        assert campaign["balance_note"] == "sur le pool (non payé + immature)"
        assert campaign["pool_balance"]["paid"] == 1474.747
