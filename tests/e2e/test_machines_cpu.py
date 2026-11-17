"""E2E de la fiche machine : ligne CPU et hashrate des mineurs CPU (003-c7).

La collecte réelle n'est pas jouée ici (aucune machine contactée) : ``cpu_json``
est écrit dans la base du serveur e2e, exactement comme le fait le collecteur,
puis la fiche est relue dans Chromium.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_machine_sheet_shows_cpu_state_and_cpu_miner_hashrate(e2e_server):
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
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded", timeout=15000)

        created = await page.evaluate(
            """async () => {
                const name = 'e2e-cpu-' + Date.now();
                const r = await fetch('/api/machines', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name, host: '192.0.2.98', ssh_user: 'root'})
                });
                return [r.status, await r.json()];
            }"""
        )
        assert created[0] == 200, created
        machine_id = created[1]["id"]
        machine_uuid = created[1]["uuid"]

        # Ce que le collecteur écrit : cpu_json + les mineurs externes (masqués).
        cpu = {
            "load_pct": 67.5, "temp_c": 68.0, "freq_mhz": 3900.0, "cores": 6,
            "model": "AMD Ryzen 5 3600 6-Core", "power_w": 88.96,
            "power_source": "API SRBMiner package_power", "hashrate_hs": 237800000.0,
            "miners": [{
                "name": "cpuminer-opt", "coin": "KSH", "hashrate_hs": 237800000.0,
                "threads": "2", "source": "log /var/log/athlon-korsh-miner.log",
                "note": "", "node": None,
            }],
        }
        miners = [{
            "name": "cpuminer-opt", "kind": "cpu", "version": "", "algorithm": "yespower",
            "pool": "pool.example.net:3333", "coin": "KSH", "threads": "2", "count": 1,
            "hashrate_hs": 237800000.0,
            "hashrate_source": "log /var/log/athlon-korsh-miner.log",
            "hashrate_note": "", "api_port": None, "user": "fossor", "pid": 12,
        }]
        now = datetime.now(timezone.utc).isoformat()
        db = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
        try:
            db.execute(
                "UPDATE machines SET external_miners = ? WHERE id = ?",
                (json.dumps(miners), machine_id),
            )
            db.execute(
                "INSERT OR REPLACE INTO machine_status "
                "(machine_uuid, ts, online, miner, algo, coin, cpu_json) "
                "VALUES (?, ?, 1, 'cpuminer-opt', 'yespower', 'KSH', ?)",
                (machine_uuid, now, json.dumps(cpu)),
            )
            db.commit()
        finally:
            db.close()

        await page.reload(wait_until="networkidle")

        # Ligne CPU de la fiche : charge, température, fréquence, puissance.
        cpu_line = page.locator('[data-cpu-line="1"]').first
        await cpu_line.wait_for(state="attached", timeout=8000)
        text = await cpu_line.text_content()
        assert "charge 67.5 %" in text, text
        assert "68 °C" in text, text
        assert "3.90 GHz" in text, text
        assert "88.96 W" in text, text
        # Mineur CPU : coin, hashrate, threads, source.
        assert "KSH" in text and "238 MH/s" in text, text
        assert "2 threads" in text, text
        assert "source log /var/log/athlon-korsh-miner.log" in text, text

        # L'alerte « mineur externe » porte aussi le hashrate et sa source.
        alert = await page.locator(".miner-alert").first.text_content()
        assert "238 MH/s" in alert, alert
        assert "source log" in alert, alert

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors

        await browser.close()
