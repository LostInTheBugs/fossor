"""E2E: every action button of the Machines screen, and the three form modes.

Clicking each button must produce zero console/page errors.  The machine uses
127.0.0.1:1 (connection refused instantly) so no real host is ever contacted.
"""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


async def _login(page):
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)
    await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded", timeout=15000)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_machines_action_buttons_and_form_modes(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    dialogs = {"accept": False}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()

        async def on_dialog(dialog):
            if dialogs["accept"]:
                await dialog.accept()
            else:
                await dialog.dismiss()

        page.on("dialog", lambda d: on_dialog(d))

        await _login(page)

        # ── Add a machine (password mode) ────────────────────────────
        await page.locator("#addBtn").click()
        await page.locator("#modal.open").wait_for(state="visible", timeout=5000)
        assert await page.locator("#userField").is_visible()
        await page.fill("#fName", "jetable-e2e")
        await page.fill("#fHost", "127.0.0.1")
        await page.fill("#fPort", "1")
        await page.select_option("#fAuthType", "password")
        await page.fill("#fSecret", "secret-jetable")
        await page.locator("#saveBtn").click()
        await page.locator("#toast.ok", has_text="Machine ajoutée").wait_for(timeout=10000)
        row = page.locator("tr", has_text="jetable-e2e").first
        await row.wait_for(timeout=10000)

        # ── Mode visibility: provision hides the generic login ───────
        await page.locator("#addBtn").click()
        await page.select_option("#fAuthType", "provision")
        assert not await page.locator("#userField").is_visible()
        assert await page.locator("#fTempUser").is_visible()
        assert await page.locator("#fTempPassword").is_visible()
        assert await page.locator("#fAccount").is_visible()
        await page.select_option("#fAuthType", "fossor_key")
        assert await page.locator("#userField").is_visible()
        assert (await page.text_content("#userField label")).strip() == "Compte sur la machine"
        assert await page.locator("#fossorKeyPanel").is_visible()

        # Empty host: refused client-side, French message, no request.
        await page.select_option("#fAuthType", "password")
        await page.fill("#fName", "verif-champs")
        await page.fill("#fHost", "")
        await page.locator("#saveBtn").click()
        await page.locator("#toast.err", has_text="Hôte").wait_for(timeout=3000)
        assert await page.locator("#modal.open").is_visible()
        await page.locator("#cancelBtn").click()

        # ── ✏️ edit and save ─────────────────────────────────────────
        await row.locator("button[title='Tester la connexion SSH']").wait_for()
        await row.locator("button:has-text('✏️')").click()
        await page.locator("#modal.open").wait_for(state="visible", timeout=5000)
        await page.wait_for_function(
            "() => document.querySelector('#fName').value === 'jetable-e2e'"
        )
        assert (await page.text_content("#modalTitle")).strip() == "Modifier"
        placeholder = await page.get_attribute("#fSecret", "placeholder")
        assert "inchangé" in placeholder
        await page.fill("#fName", "jetable-e2e-mod")
        await page.locator("#saveBtn").click()
        await page.locator("#toast.ok", has_text="Machine mise à jour").wait_for(timeout=10000)
        row = page.locator("tr", has_text="jetable-e2e-mod").first
        await row.wait_for(timeout=10000)

        # ── 🔌 test (connection refused, no crash) ───────────────────
        await row.locator("button[title='Tester la connexion SSH']").click()
        await page.locator("#toast.err").first.wait_for(timeout=15000)
        await expect(row.locator("button[title='Tester la connexion SSH']")).to_be_enabled()

        # ── 🔍 detect ────────────────────────────────────────────────
        await row.locator("button:has-text('🔍 Détecter le matériel')").click()
        await page.locator("#toast.err", has_text="Détection refusée").wait_for(timeout=20000)

        # ── 🗑️ delete: cancel, then confirm ──────────────────────────
        await row.locator("button:has-text('🗑️')").click()
        await page.wait_for_timeout(300)
        assert await page.locator("tr", has_text="jetable-e2e-mod").count() >= 1

        dialogs["accept"] = True
        await row.locator("button:has-text('🗑️')").click()
        await page.locator("#toast.ok", has_text="Machine supprimée").wait_for(timeout=10000)
        await page.wait_for_timeout(500)
        assert not await page.locator("tr", has_text="jetable-e2e-mod").first.is_visible()

        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"
        await browser.close()
