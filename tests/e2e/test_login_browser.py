"""E2E browser tests — Playwright against a real uvicorn process.

Run inside the Playwright image:
    pytest tests/e2e -m e2e -v
"""

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_login_flow_zero_console_errors(e2e_server):
    """Open /, log in, land on /machines, with zero console/page errors."""
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

        # 1. Unauthenticated / serves the login page
        assert "Connexion" in (await page.title()), await page.title()

        # 2. The form is wired (no ReferenceError on $)
        await page.wait_for_selector("#loginForm", timeout=5000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)

        await page.locator("button[type=submit]").click()

        # 3. Lands on /machines
        await page.wait_for_url("**/machines", timeout=15000)
        assert page.url.endswith("/machines"), page.url
        await page.wait_for_load_state("networkidle")

        # 4. No browser error at all
        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"

        await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_unauthenticated_redirects(e2e_server):
    """/machines and /audit redirect (303) to / when not logged in."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        for path in ("/machines", "/audit"):
            resp = await page.goto(E2E_URL + path, wait_until="domcontentloaded", timeout=15000)
            assert resp is not None
            assert resp.status == 200, f"{path} final status {resp.status}"
            assert page.url.rstrip("/") == E2E_URL.rstrip("/"), f"{path} → {page.url}"
            assert "Connexion" in (await page.title())

        await browser.close()
