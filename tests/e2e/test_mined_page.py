"""e2e — écran « Tokens minés » (§6 écran 3).

Le serveur est lancé par ``tests/e2e/conftest.py`` ; l'import se fait avec les
deux vrais fichiers de ``tests/fixtures`` (copies anonymisées) et aucun appel
réseau n'est fait (``FOSSOR_OFFLINE=1`` passé au conteneur).
"""

import pytest
from playwright.async_api import async_playwright

ADMIN_PASSWORD = "adminpass"
FIX = "/app/tests/fixtures"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mined_screen_renders_without_console_error(e2e_server):
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

        await page.goto(e2e_server + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        resp = await page.goto(e2e_server + "/mined", wait_until="domcontentloaded", timeout=15000)
        assert resp.status == 200
        await page.wait_for_load_state("networkidle")
        assert "Tokens minés" in await page.inner_text("body")

        # Import the legacy files through the UI (real multipart upload).
        await page.click("button[data-panel=import]")
        await page.set_input_files("#fStatus", FIX + "/mining-status.json")
        await page.set_input_files("#fLive", FIX + "/mining-live.json")
        await page.uncheck("#dryRun")
        await page.click("#importForm button[type=submit]")
        await page.wait_for_selector("#impOut", state="visible", timeout=20000)
        assert '"applied"' in await page.inner_text("#impOut")

        await page.reload(wait_until="networkidle")

        cards = await page.locator("#campaigns .card").count()
        assert cards >= 3, cards
        assert "PRL" in await page.inner_text("#campaigns")
        assert (await page.inner_text("#totalEur")).endswith("€")

        rows = await page.locator("#historyBody tr").count()
        assert rows >= 5, rows

        # « Bilan » unfolds the verdict row.
        await page.locator("#historyBody [data-verdict]").first.click()
        assert await page.locator("#historyBody tr[id^=verdict-]").count() >= 1

        # Catalogue and wallets tabs switch and list the imported rows.
        await page.click("button[data-panel=coins]")
        assert await page.locator("#panel-coins").is_visible()
        assert await page.locator("#coinBody tr").count() >= 4
        await page.click("button[data-panel=wallets]")
        assert await page.locator("#panel-wallets").is_visible()
        assert await page.locator("#walletBody tr").count() >= 4

        assert console_errors == []
        assert page_errors == []
        await context.close()
        await browser.close()
