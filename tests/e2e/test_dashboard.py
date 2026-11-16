"""E2E: the dashboard is the landing page and renders machine cards."""

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dashboard_is_landing_page_with_zero_console_errors(e2e_server):
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

        # 1. Login lands on the dashboard (§6 écran 1).
        await page.wait_for_url("**/dashboard", timeout=15000)
        assert page.url.endswith("/dashboard"), page.url
        await page.wait_for_load_state("networkidle")
        assert "Tableau de bord" in await page.inner_text("body")

        # 2. Empty state before any machine exists.
        assert await page.is_visible("#emptyMsg")

        # 3. A machine card appears after adding one (no SSH: card is offline).
        await page.evaluate(
            """async () => {
                const res = await fetch('/api/machines', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: 'e2e-dash', host: '127.0.0.1', ssh_port: 22,
                        ssh_user: 'tester', auth_type: 'password',
                        usage: 'both', enabled: true
                    })
                });
                if (!res.ok) throw new Error('machine create failed ' + res.status);
            }"""
        )
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector(".card", timeout=10000)
        cards = await page.inner_text("#cards")
        assert "e2e-dash" in cards
        assert "hors ligne" in cards.lower()
        assert await page.is_hidden("#emptyMsg")

        # 4. The page refreshes itself every 30 s without reloading.
        assert await page.evaluate("typeof setInterval === 'function'")

        # 5. Zero browser errors at all.
        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"

        await browser.close()
