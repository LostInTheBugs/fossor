"""E2E 2026.11.003-c4 — une carte par coin, comptée depuis ``/api/metrics/live``.

Le test crée trois machines par l'API, écrit leur dernier état (mineurs
externes + machine_status) dans la base du serveur e2e, puis vérifie que le
tableau de bord affiche **une carte par coin** avec le bon nombre de machines.

Aucun SSH, aucun rig : les données sont celles des fixtures c4.
"""

import json
import sqlite3

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"

# machine → mineurs (nom, coin, hashrate, workers)
MINERS = {
    "e2e-rig": [
        # 2026.11.003-c6 : un SRBMiner multi-algos = une ligne par algo.
        {"name": "SRBMiner-MULTI", "coin": "PRL", "algorithm": "pearlhash",
         "hashrate_hs": 6.5e10, "count": 1},
        {"name": "SRBMiner-MULTI", "coin": "WAM", "algorithm": "yespower",
         "hashrate_hs": 2.1e9, "count": 1},
        {"name": "SRBMiner-MULTI", "coin": "KSH", "algorithm": "yespower",
         "hashrate_hs": 1.4e9, "count": 1},
        {"name": "wam-miner", "coin": "WAM", "count": 1},
        {"name": "Minage solo (nœud)", "coin": "KSH", "count": 1, "workers": 1},
    ],
    "e2e-ryzen": [
        {"name": "SRBMiner", "coin": "PRL", "hashrate_hs": 2.1535e14, "count": 1},
        {"name": "Minage solo (nœud)", "coin": "BIG", "count": 1, "workers": 11},
    ],
    "e2e-athlon": [
        {"name": "SRBMiner", "coin": "PRL", "hashrate_hs": 7.1389e13, "count": 1},
        {"name": "cpuminer", "coin": "KSH", "count": 1},
    ],
}


def seed_state() -> None:
    """Write the last known state of the three machines (not a collection)."""
    path = f"{E2E_DATA_DIR}/fossor.db"
    db = sqlite3.connect(path, timeout=10)
    try:
        # 003-c6 : le vrai serveur (démarrage) a créé les coins de base.
        tickers = {row[0] for row in db.execute("SELECT ticker FROM coins").fetchall()}
        assert {"PRL", "KSH", "WAM", "BIG"} <= tickers, tickers
        rows = db.execute("SELECT id, name FROM machines").fetchall()
        by_name = {name: machine_id for machine_id, name in rows}
        for name, miners in MINERS.items():
            machine_id = by_name[name]
            uuid = db.execute(
                "SELECT uuid FROM machines WHERE id = ?", (machine_id,)
            ).fetchone()[0]
            total = sum(m.get("hashrate_hs") or 0 for m in miners) or None
            db.execute(
                "UPDATE machines SET uuid = ?, external_miners = ? WHERE id = ?",
                (uuid, json.dumps(miners, ensure_ascii=False), machine_id),
            )
            db.execute(
                "INSERT OR REPLACE INTO machine_status "
                "(machine_uuid, ts, online, error, miner, algo, coin, uptime_s, "
                " accepted, rejected, api_port, total_hashrate_hs, api_error) "
                "VALUES (?, datetime('now'), 1, NULL, ?, '', ?, 3600, 10, 0, "
                "        NULL, ?, NULL)",
                (
                    uuid,
                    miners[0]["name"],
                    miners[0]["coin"],
                    total,
                ),
            )
        db.commit()
    finally:
        db.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dashboard_one_card_per_coin(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()

        await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        for name in MINERS:
            await page.evaluate(
                """async (name) => {
                    const res = await fetch('/api/machines', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            name: name, host: '192.0.2.50', ssh_port: 22,
                            ssh_user: 'tester', auth_type: 'password',
                            usage: 'both', enabled: true
                        })
                    });
                    if (!res.ok) throw new Error('machine create failed ' + res.status);
                }""",
                name,
            )

        seed_state()
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector(".token", timeout=10000)

        cards = await page.evaluate(
            """() => Array.from(document.querySelectorAll('.token')).map((el) => ({
                ticker: el.querySelector('.ticker').textContent.trim(),
                meta: el.querySelector('.meta').textContent.trim(),
                hs: el.querySelector('.hs').textContent.trim()
            }))"""
        )
        by_ticker = {card["ticker"]: card for card in cards}

        # Une carte par coin, jamais une carte pour le rig multi-coins.
        assert set(by_ticker) == {"PRL", "KSH", "WAM", "BIG"}, by_ticker
        assert "3 machines" in by_ticker["PRL"]["meta"]      # rig + ryzen + athlon
        assert "2 machines" in by_ticker["KSH"]["meta"]      # rig + athlon
        assert by_ticker["WAM"]["meta"].startswith("1 machine ")  # rig seul
        assert "11 workers" in by_ticker["BIG"]["meta"]       # minage solo du Ryzen
        assert by_ticker["PRL"]["hs"] != "—"                  # hashrate de l'API

        # Le tableau de bord ne titre plus le hashrate par machine.
        assert await page.query_selector("#sHash") is None
        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"

        await browser.close()
