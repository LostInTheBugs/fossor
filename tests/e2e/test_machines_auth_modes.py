"""E2E: the three SSH authentication modes on the add-machine screen."""

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_three_auth_modes_and_fossor_key_panel(e2e_server):
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

        await page.locator("#addBtn").click()
        await page.locator("#modal.open").wait_for(state="visible", timeout=5000)

        values = await page.eval_on_selector_all(
            "#fAuthType option", "opts => opts.map(o => o.value)"
        )
        assert values == ["password", "fossor_key", "provision"]

        # Mode 2: public key + ready-to-paste command, login included.
        await page.select_option("#fAuthType", "fossor_key")
        await page.fill("#fUser", "miner")
        await page.wait_for_function(
            "() => document.querySelector('#fossorPublicKey').value.startsWith('ssh-ed25519 ')"
        )
        public_key = await page.input_value("#fossorPublicKey")
        assert "fossor@" in public_key
        command = await page.text_content("#fossorKeyCmd")
        assert command.startswith("mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo 'ssh-ed25519 ")
        assert ">> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" in command
        assert public_key.split()[1] in command

        # Mode 3: temporary access form.
        await page.select_option("#fAuthType", "provision")
        await page.locator("#provisionPanel").wait_for(state="visible", timeout=3000)
        assert await page.locator("#fTempUser").is_visible()
        assert await page.locator("#fTempPassword").is_visible()
        assert await page.input_value("#fAccount") == "fossor"
        assert not await page.locator("#fossorKeyPanel").is_visible()

        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"

        await browser.close()
