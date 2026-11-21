"""E2E — Configuration → Watchdog et onglet Watchdog d'une fiche machine.

Le premier test vérifie les valeurs par défaut **et** l'effet réel d'un
enregistrement (seuil relu par l'API, puis remis à sa valeur).  Le second
sème un déclenchement dans la base du serveur e2e et lit le journal de la
fiche machine.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL
from tests.e2e.factory import DB_PATH


async def _login(context) -> None:
    response = await context.request.post(
        f"{E2E_URL}/api/login",
        form={"username": "admin", "password": "adminpass"},
    )
    assert response.ok, await response.text()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_configuration_watchdog_reglages_et_defauts(e2e_server):
    shots = os.environ.get("FOSSOR_E2E_SHOTS", "")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        await _login(context)
        page = await context.new_page()
        await page.goto(f"{E2E_URL}/settings", wait_until="networkidle", timeout=15000)
        await page.click("#tabWatchdog")
        await page.wait_for_selector("#panel-watchdog", state="visible", timeout=10000)
        await page.wait_for_function(
            "document.querySelector('#wdHashPct').value === '50'", timeout=10000)

        assert await page.is_checked("#wdEnabled") is True
        assert await page.is_checked("#wdActNotify") is True
        assert await page.is_checked("#wdActMiner") is True
        assert await page.is_checked("#wdActReboot") is False
        assert await page.is_checked("#wdActPower") is False
        assert await page.input_value("#wdHashPct") == "50"
        assert await page.input_value("#wdHashMin") == "10"
        assert await page.input_value("#wdSharesMin") == "20"
        assert await page.input_value("#wdTempGpu") == "85"
        assert await page.input_value("#wdTempCpu") == "90"
        assert await page.input_value("#wdEscalation") == "5"
        assert await page.input_value("#wdGrace") == "10"
        assert await page.input_value("#wdMax") == "3"
        if shots:
            await page.screenshot(path=f"{shots}/watchdog-config.png", full_page=True)

        # Effet réel : le seuil enregistré est relu par l'API.
        await page.fill("#wdHashPct", "55")
        await page.click("#wdSave")
        await page.wait_for_function(
            "document.querySelector('#wdState').textContent === 'enregistré'",
            timeout=10000)
        saved = await page.evaluate(
            "fetch('/api/watchdog',{credentials:'include'}).then(r=>r.json())")
        assert saved["config"]["rules"]["hashrate_low"]["pct"] == 55.0

        # Remise en état pour les autres tests de la session.
        await page.fill("#wdHashPct", "50")
        await page.click("#wdSave")
        await page.wait_for_function(
            "document.querySelector('#wdState').textContent === 'enregistré'",
            timeout=10000)
        restored = await page.evaluate(
            "fetch('/api/watchdog',{credentials:'include'}).then(r=>r.json())")
        assert restored["config"]["rules"]["hashrate_low"]["pct"] == 50.0
        await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_onglet_watchdog_de_la_fiche_machine_montre_le_journal(e2e_server):
    shots = os.environ.get("FOSSOR_E2E_SHOTS", "")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        await _login(context)
        created = await context.request.post(f"{E2E_URL}/api/machines", data={
            "name": "e2e-watchdog", "host": "192.0.2.77", "ssh_user": "fossor",
            "auth_type": "fossor_key", "sudo": "sudo without password",
        })
        assert created.ok, await created.text()
        machine = await created.json()
        try:
            conn = sqlite3.connect(DB_PATH, timeout=15)
            try:
                conn.execute(
                    "INSERT INTO watchdog_events (ts, machine_id, machine_name, device_id,"
                    " device_label, rule, measure, action, result, simulated, detail)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        datetime.now(timezone.utc).isoformat(), machine["id"],
                        "e2e-watchdog", 0, "gpu RTX 3070", "hashrate_low",
                        "400 H/s < 500 H/s (50 % de 1000)", "restart_miner", "ok", 1,
                        "systemctl restart fossor-miner-e2e.service (simulé)",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            page = await context.new_page()
            await page.goto(f"{E2E_URL}/machines", wait_until="networkidle", timeout=15000)
            await page.wait_for_selector("#machBody tr", timeout=15000)
            await page.locator(
                f'.mtab[data-machine="{machine["id"]}"][data-tab="watchdog"]'
            ).click()
            box = page.locator(f"#mtabWatchdog-{machine['id']}")
            await box.get_by_text("Journal du watchdog").wait_for(timeout=10000)
            await box.get_by_text("hashrate_low").wait_for(timeout=10000)
            text = await box.inner_text()
            print("\n### JOURNAL WATCHDOG FICHE:\n" + text)
            assert "400 H/s < 500 H/s" in text
            assert "restart_miner" in text
            assert "simulé" in text
            if shots:
                await page.screenshot(path=f"{shots}/watchdog-fiche.png", full_page=True)
        finally:
            await context.request.delete(f"{E2E_URL}/api/machines/{machine['id']}")
        await browser.close()
