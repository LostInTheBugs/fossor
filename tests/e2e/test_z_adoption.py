"""6.7 e2e — bouton « Adopter », plan et écran de validation."""

import pytest
from playwright.async_api import async_playwright

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_bouton_adopter_ouvre_le_plan_et_la_validation(e2e_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.goto(e2e_server + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)
        await page.goto(e2e_server + "/machines", wait_until="networkidle", timeout=15000)
        # le libellé a changé, et chaque ligne porte le bouton
        body = await page.inner_text("body")
        assert "non géré par Fossor" not in body
        if "Lancé hors Fossor" in body:
            assert await page.locator("button:has-text('Adopter')").count() >= 1
            await page.locator("button:has-text('Adopter')").first.click()
            await page.wait_for_selector("#adoptModal.open", timeout=10000)
            assert "Sauvegarde du wallet" in await page.inner_text("#adoptImpact") or "Pas de wallet" in await page.inner_text("#adoptImpact")
            assert await page.locator("#adoptClose").count() == 0
        await browser.close()
