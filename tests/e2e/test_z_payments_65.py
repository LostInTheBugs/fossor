"""e2e — section « Paiements reçus » de Tokens minés (§4.16, étape 6.5).

Données strictement fictives : aucune adresse réelle, aucun appel de pool.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR

ADMIN_PASSWORD = "adminpass"
DB_PATH = f"{E2E_DATA_DIR}/fossor.db"
COIN = "PAY"
ADDRESS = "PAYWALLET0000000000000000000000000000000042"
TX = "tx-e2e-paiement-fictif"

os.environ["FOSSOR_DATA_DIR"] = E2E_DATA_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)(), engine


def _seed() -> None:
    from app.models import PoolPayment, PriceSample, SoloBlock, Wallet

    db, engine = _session()
    try:
        wallet = Wallet(coin_ticker=COIN, address=ADDRESS, label="fictif 6.5", pool="luckypool")
        db.add(wallet)
        db.flush()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db.add(PriceSample(ticker=COIN, ts=(now - timedelta(days=2)).isoformat(),
                           eur=2.0, source="coingecko"))
        db.add(PoolPayment(
            wallet_id=wallet.id, coin_ticker=COIN, pool="luckypool", amount=3.0,
            received_at=(now - timedelta(days=1)).isoformat(), tx=TX,
            address_hint="PAYWAL…0042", source="luckypool",
            dedupe_key=f"luckypool|tx:{TX}",
        ))
        db.add(SoloBlock(coin_ticker=COIN, height=4242, reward=1.0,
                         found_at=(now - timedelta(hours=12)).isoformat(),
                         node="noeud-fictif", tx="tx-e2e-solo"))
        db.commit()
    finally:
        db.close()
        engine.dispose()


def _cleanup() -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{DB_PATH}")
    try:
        with engine.begin() as conn:
            wallet = conn.execute(
                text("SELECT id FROM wallets WHERE coin_ticker = :c AND address = :a"),
                {"c": COIN, "a": ADDRESS},
            ).first()
            if wallet:
                conn.execute(text("DELETE FROM pool_payments WHERE wallet_id = :i"), {"i": wallet[0]})
                conn.execute(text("DELETE FROM wallets WHERE id = :i"), {"i": wallet[0]})
            conn.execute(text("DELETE FROM solo_blocks WHERE coin_ticker = :c"), {"c": COIN})
            conn.execute(text("DELETE FROM price_samples WHERE ticker = :c"), {"c": COIN})
    finally:
        engine.dispose()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_payments_received_section(e2e_server):
    _cleanup()
    _seed()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(e2e_server + "/", wait_until="domcontentloaded", timeout=15000)
            await page.fill("#username", "admin")
            await page.fill("#password", ADMIN_PASSWORD)
            await page.locator("button[type=submit]").click()
            await page.wait_for_url("**/dashboard", timeout=15000)

            await page.goto(e2e_server + "/mined", wait_until="networkidle", timeout=15000)
            await page.click("button[data-panel='payments']")
            await page.wait_for_selector("#panel-payments.active", timeout=10000)
            await page.wait_for_selector("#paymentsBody tr", timeout=15000)

            text = await page.inner_text("#paymentsBody")
            assert COIN in text
            assert "luckypool" in text
            assert "PAYWAL…0042" in text, "adresse tronquée attendue"
            assert ADDRESS not in text, "l'adresse complète ne doit jamais apparaître"
            assert "6" in text, "3 PAY × 2 € = 6 €"
            assert "4242" in text, "le bloc solo doit être listé"
            await browser.close()
    finally:
        _cleanup()
