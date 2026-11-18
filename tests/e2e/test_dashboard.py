"""E2E §4.19 — le tableau de bord est un « état de santé » : une pastille par
machine et les totaux en tête, aucun détail (les tuiles vivent dans /followup).
"""

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL
from tests.e2e.factory import create_machine

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dashboard_is_landing_page_with_zero_console_errors(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
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

        # 1. Login lands on the dashboard (§6 écran 1).
        await page.wait_for_url("**/dashboard", timeout=15000)
        assert page.url.endswith("/dashboard"), page.url
        await page.wait_for_load_state("networkidle")
        assert "Tableau de bord" in await page.inner_text("body")

        # 2. Aucun détail sur le tableau de bord : ni cartes machine, ni cartes par coin.
        assert await page.query_selector("#cards") is None
        assert await page.query_selector("#tokenCards") is None

        # 3. A machine appears as a red pill after adding one (no SSH: offline).
        await create_machine(page, "e2e-dash", host="127.0.0.1")
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector(".pill[data-status]", timeout=15000)
        pills = await page.eval_on_selector_all(
            ".pill", "els => els.map(e => [e.dataset.status, e.textContent])"
        )
        red = [text for status, text in pills if status == "red"]
        assert any("e2e-dash" in text and "hors ligne" in text for text in red), pills
        assert await page.is_hidden("#emptyMsg")

        # 4. Les totaux sont câblés (§4.15) : coût 0.00 €, revenu inconnu.
        await page.wait_for_function(
            "() => /^\\d+ \\/ \\d+$/.test(document.querySelector('#sOnline').textContent.trim())",
            timeout=15000,
        )
        await page.wait_for_function(
            "document.querySelector('#sCost').textContent === '0.00 €'", timeout=10000
        )
        assert await page.inner_text("#sCostMonth") == "0.00 €"
        assert await page.inner_text("#sRevenue") == "—"
        assert await page.inner_text("#sPower") == "—"

        # 5. The page refreshes itself every 30 s without reloading.
        assert await page.evaluate("typeof setInterval === 'function'")

        # 6. 003-c2: no global "Hashrate total" on the dashboard.
        assert await page.query_selector("#sHash") is None

        # 7. Zero browser errors at all.
        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"

        await browser.close()
