"""E2E: hardware detection UI (button present, zero console error)."""

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_detect_hardware_button_visible(e2e_server):
    """A machine row exposes « Détecter le matériel » and the page stays clean."""
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
        await page.wait_for_url("**/machines", timeout=15000)

        # Create a machine from the authenticated page (unique name per run).
        created = await page.evaluate(
            """async () => {
                const name = 'e2e-hw-' + Date.now();
                const r = await fetch('/api/machines', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name, host: '192.0.2.99', ssh_user: 'root'})
                });
                return [r.status, await r.json()];
            }"""
        )
        assert created[0] == 200, created
        machine_id = created[1]["id"]
        assert created[1]["uuid"], "machines must expose a cluster uuid"

        await page.reload(wait_until="networkidle")

        button = page.locator("button", has_text="Détecter le matériel").first
        await button.wait_for(state="visible", timeout=5000)
        assert await button.is_visible()

        # Device list endpoint answers with an empty list (not an error page).
        devices = await page.evaluate(
            "async (id) => (await fetch('/api/machines/' + id + '/devices', "
            "{credentials: 'include'})).json()",
            machine_id,
        )
        assert devices == []

        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"

        await browser.close()
