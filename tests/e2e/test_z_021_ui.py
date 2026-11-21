"""E2E 021 — bandeau conditionnel, Général effectif et accès au coffre.

Toutes les machines pilotables sont armées : plus aucune simulation, donc plus
de bandeau. Configuration → Général nomme le réglage comme la valeur par défaut
des machines non armées et affiche l'état effectif. Le coffre a un onglet
principal.
"""

from __future__ import annotations

import os

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL
from tests.e2e.test_z_spec_vms import ADMIN_PASSWORD

SHOTS = os.environ.get("FOSSOR_E2E_SHOTS", "/tmp")


@pytest.mark.asyncio
async def test_bandeau_absent_general_effectif_et_coffre(e2e_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context(viewport={"width": 1280, "height": 900})).new_page()
        await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        armed = await page.evaluate(
            """async () => {
                const machines = await (await fetch('/api/machines')).json();
                const done = [];
                for (const m of machines) {
                    if (!m.enabled || m.usage === 'none' || m.machine_kind === 'asic') continue;
                    const r = await fetch('/api/machines/' + m.id + '/simulation', {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({real: true, confirm: 'PILOTER'})
                    });
                    if (r.ok) done.push(m.id);
                }
                return done;
            }"""
        )
        assert armed, "aucune machine pilotable à armer"

        settings = await page.evaluate(
            "async () => await (await fetch('/api/miner/settings')).json()"
        )
        assert settings["pilotage"]["simulated"] == [], settings["pilotage"]
        assert settings["pilotage"]["real_count"] >= 1, settings["pilotage"]

        await page.goto(E2E_URL + "/dashboard", wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
        assert await page.locator("#simBanner").count() == 0, (
            await page.locator("#simBanner").inner_text()
        )
        await page.screenshot(path=f"{SHOTS}/021-bandeau-absent.png", full_page=True)

        # Configuration → Général : libellé de défaut + état effectif.
        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        await page.wait_for_selector("#panel-general h2", timeout=10000)
        assert "Simulation par défaut des nouvelles machines" in await page.inner_text(
            "#panel-general"
        )
        effective = await page.inner_text("#simEffective")
        assert "en pilotage réel" in effective, effective
        assert "en simulation" in effective, effective
        assert "ASIC en suivi" in effective, effective
        await page.screenshot(path=f"{SHOTS}/021-general.png", full_page=True)

        # Onglet principal « Coffre » : visible dans la navigation et ouvre le panneau.
        link = page.locator('nav#nav a[href="/vault"]')
        assert await link.count() == 1
        await link.click()
        await page.wait_for_selector("#panel-vault", state="visible", timeout=10000)

        # Remise en simulation pour les tests suivants.
        for machine_id in armed:
            await page.evaluate(
                """async (id) => {
                    await fetch('/api/machines/' + id + '/simulation', {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({real: false})
                    });
                }""",
                machine_id,
            )
        await browser.close()
