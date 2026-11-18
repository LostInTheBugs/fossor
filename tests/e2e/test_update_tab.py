"""E2E: l'écran Configuration → Mise à jour (§4.18) ne produit aucune erreur.

Le serveur e2e n'a pas de conteneur compagnon : c'est exactement le cas
« installation sans fossor-updater » — bandeau explicite, bouton grisé — et un
réglage de mise à jour automatique qui doit être accepté puis relu.
"""

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_update_screen_without_updater(e2e_server):
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

        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        # §4.19 : la carte de mise à jour vit dans le sous-onglet « Mise à jour ».
        await page.locator("#tabUpdate").click()
        await page.wait_for_selector("#panel-update", state="visible", timeout=10000)
        await page.wait_for_selector("#updBanner", state="visible", timeout=10000)

        # 1. Sans compagnon : bandeau explicite et bouton désactivé.
        banner = await page.inner_text("#updBanner")
        assert "fossor-updater" in banner
        assert await page.is_disabled("#updateApplyBtn")

        # 2. La plage horaire est lue depuis les réglages, pas codée en dur.
        assert await page.input_value("#autoStart") == "03:00"
        assert await page.input_value("#autoEnd") == "05:00"
        assert await page.is_checked("#autoEnabled") is False

        # 3. Enregistrement d'une plage qui traverse minuit.
        await page.fill("#autoStart", "23:30")
        await page.fill("#autoEnd", "01:15")
        await page.check("#autoEnabled")
        await page.click("#autoSave")
        await page.wait_for_selector("#toast", state="visible", timeout=10000)
        assert "enregistrée" in await page.inner_text("#toast")

        saved = await page.evaluate(
            "async () => (await fetch('/api/update/auto', {credentials: 'include'})).json()"
        )
        assert saved["enabled"] is True
        assert saved["window"]["start"] == "23:30"
        assert saved["window"]["end"] == "01:15"

        # 4. Les notes de version et l'historique sont rendus (vides ici).
        assert await page.is_visible("#notesWrap")
        assert await page.is_visible("#updHistory")

        # 5. Le refus de mise à jour (400) est couvert par les tests unitaires :
        #    un appel volontairement en erreur salirait la console du navigateur.
        await context.close()
        await browser.close()

    assert console_errors == []
    assert page_errors == []
