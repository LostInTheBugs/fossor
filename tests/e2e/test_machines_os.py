"""E2E §4.1 ter — le type d'OS pré-remplit le formulaire et l'écart s'affiche.

Aucune machine n'est contactée : l'écart de détection est écrit dans la base du
serveur e2e, exactement comme le fait ``apply_detection`` après une vraie
détection.
"""

import sqlite3

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_os_type_preset_and_detection_banner(e2e_server):
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

        # 1. Choisir HiveOS pré-remplit port, mode, login temporaire, pas le mot de passe.
        await page.click("#addBtn")
        await page.wait_for_selector("#modal.open", timeout=5000)
        await page.select_option("#fOs", "hiveos")
        await page.wait_for_function(
            "() => document.querySelector('#fTempUser').value === 'user'", timeout=5000
        )
        assert await page.input_value("#fPort") == "22"
        assert await page.input_value("#fAuthType") == "provision"
        assert await page.input_value("#fTempUser") == "user"
        assert await page.input_value("#fAccount") == "fossor"
        assert await page.input_value("#fSecret") == ""
        assert await page.is_visible("#osHint")
        hint = await page.inner_text("#osHint")
        assert "mot de passe par défaut de HiveOS" in hint

        # 2. Une machine dont l'OS détecté diffère du type choisi affiche la bannière.
        created = await page.evaluate(
            """async () => {
                const r = await fetch('/api/machines', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'e2e-os-' + Date.now(),
                                          host: '192.0.2.99', ssh_user: 'root',
                                          os_kind: 'hiveos'})
                });
                return await r.json();
            }"""
        )
        assert created.get("id"), created

        db = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
        db.execute("UPDATE machines SET os_kind_detected = 'ubuntu' WHERE id = ?", (created["id"],))
        db.commit()
        db.close()

        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector("#osBanner", timeout=10000)
        banner = await page.inner_text("#osBanner")
        assert "OS détecté : Ubuntu/Debian (choisi : HiveOS)" in banner

        await browser.close()

    assert page_errors == []
    assert console_errors == []
