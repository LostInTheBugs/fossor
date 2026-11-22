"""e2e — historique : graphique hashrate et export CSV comptable (\u00a710.7).

Données fictives uniquement (192.0.2.x) : la base e2e est semée directement,
aucune machine réelle n'est contactée ni contactable.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR

ADMIN_PASSWORD = "adminpass"
DB_PATH = f"{E2E_DATA_DIR}/fossor.db"
MACHINE_NAME = "HIST-E2E"
COIN = "TST"
WALLET_ADDRESS = "TST-fictif-192.0.2.41"

os.environ["FOSSOR_DATA_DIR"] = E2E_DATA_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)(), engine


def _seed() -> str:
    """Machine + trois points de métriques récents + un gain de pool fictif."""
    from app.models import Machine, Metric, PoolBalanceSample, Wallet

    db, engine = _session()
    try:
        machine = Machine(name=MACHINE_NAME, host="192.0.2.41", machine_kind="host")
        db.add(machine)
        db.flush()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for hours, hashrate in ((3, 5.0e6), (2, 5.4e6), (1, 5.8e6)):
            db.add(
                Metric(
                    machine_uuid=machine.uuid,
                    ts=(now - timedelta(hours=hours)).isoformat(),
                    hashrate_hs=hashrate,
                    power_w=640.0 + hours,
                    temp_c=52.0,
                )
            )
        wallet = Wallet(
            coin_ticker=COIN,
            address=WALLET_ADDRESS,
            label="Wallet fictif 63",
            pool="luckypool",
        )
        db.add(wallet)
        db.flush()
        db.add(
            PoolBalanceSample(
                wallet_id=wallet.id,
                ts=(now - timedelta(hours=2)).isoformat(),
                paid=1.0,
            )
        )
        db.add(
            PoolBalanceSample(
                wallet_id=wallet.id,
                ts=(now - timedelta(hours=1)).isoformat(),
                paid=1.25,
            )
        )
        db.commit()
        return machine.uuid
    finally:
        db.close()
        engine.dispose()


def _cleanup() -> None:
    """La base e2e est partagée par la session : on retire strictement le semis."""
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{DB_PATH}")
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT id, uuid FROM machines WHERE name = :n"), {"n": MACHINE_NAME}
            ).first()
            if row:
                conn.execute(text("DELETE FROM metrics WHERE machine_uuid = :u"), {"u": row[1]})
                conn.execute(text("DELETE FROM machines WHERE id = :i"), {"i": row[0]})
            wallet = conn.execute(
                text("SELECT id FROM wallets WHERE coin_ticker = :c AND address = :a"),
                {"c": COIN, "a": WALLET_ADDRESS},
            ).first()
            if wallet:
                conn.execute(
                    text("DELETE FROM pool_balance_samples WHERE wallet_id = :i"), {"i": wallet[0]}
                )
                conn.execute(text("DELETE FROM pool_balances WHERE wallet_id = :i"), {"i": wallet[0]})
                conn.execute(text("DELETE FROM wallets WHERE id = :i"), {"i": wallet[0]})
    finally:
        engine.dispose()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_history_chart_and_csv_export(e2e_server, tmp_path):
    _cleanup()
    machine_uuid = _seed()
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

            await page.goto(e2e_server + "/history", wait_until="networkidle", timeout=15000)
            await page.wait_for_selector("#target option", state="attached", timeout=15000)
            await page.select_option("#target", value=machine_uuid)

            # Le graphique du hashrate : un <svg> et au moins une série tracée.
            await page.wait_for_selector("#chart-hashrate svg", state="attached", timeout=15000)
            series = await page.eval_on_selector_all(
                "#chart-hashrate svg path[fill='none']",
                "els => els.map(e => e.getAttribute('d') || '')",
            )
            assert series, "aucune série dans le conteneur du hashrate"
            assert any("L" in path for path in series), series

            # L'export CSV se télécharge vraiment, BOM + ';' + note fiscale.
            async with page.expect_download(timeout=15000) as info:
                await page.click("#exportBtn")
            download = await info.value
            assert download.suggested_filename.endswith(".csv"), download.suggested_filename
            target = tmp_path / "export.csv"
            await download.save_as(str(target))
            raw = target.read_bytes()

            assert raw.startswith(b"\xef\xbb\xbf"), raw[:8]
            text = raw.decode("utf-8-sig")
            assert "coin;quantite;date_reception" in text, text[:200]
            assert "pas de conseil fiscal" in text, text[:200]
            assert "TST" in text and "1,25" not in text.splitlines()[0]

            await browser.close()
    finally:
        _cleanup()
