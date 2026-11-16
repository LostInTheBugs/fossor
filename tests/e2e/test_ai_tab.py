"""E2E: « Analyse IA » tab (visible, presets, zero console error)."""

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ai_tab_visible_presets_and_globals(e2e_server):
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

        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded", timeout=15000)

        tab = page.locator("#tabAi")
        await tab.wait_for(state="visible", timeout=5000)
        assert await tab.is_visible()
        await tab.click()

        panel = page.locator("#panel-ai")
        await panel.wait_for(state="visible", timeout=5000)
        assert await panel.is_visible()
        assert not await page.locator("#panel-general").is_visible()

        # The type menu pre-fills Hermes, local and cloud examples.
        await page.select_option("#primary_type", "hermes-agent")
        assert ":8642/v1" in await page.input_value("#primary_base_url")
        assert await page.input_value("#primary_model") == "hermes-agent"

        await page.select_option("#primary_type", "openai-compatible-local")
        assert ":8080/v1" in await page.input_value("#primary_base_url")

        await page.select_option("#primary_type", "cloud-api")
        assert "api.deepseek.com" in await page.input_value("#primary_base_url")
        assert await page.input_value("#primary_model") == "deepseek-flash"

        # Token block: no token yet, button says « Créer le jeton ».
        assert await page.locator("#tokBtn").is_visible()
        assert "Créer le jeton" in (await page.locator("#tokBtn").inner_text())

        # Global switches are persisted.
        await page.fill("#cadence", "8")
        await page.locator("#saveGlobals").click()
        await page.wait_for_function(
            "async () => { const r = await fetch('/api/config/ai', {credentials:'include'});"
            " const d = await r.json(); return d.analysis_cadence_hours === 8; }",
            timeout=5000,
        )

        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"

        await browser.close()
