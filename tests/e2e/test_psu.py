"""E2E — fiche « Alimentations » (§4.2 bis) : deux PSU, GPU réparti, alerte orange."""

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
DB_PATH = f"{E2E_DATA_DIR}/fossor.db"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_psu_section_two_psus_split_gpu_and_orange_alert(e2e_server):
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
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

        machine_id = await page.evaluate(
            """async () => {
                const res = await fetch('/api/machines', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'e2e-psu', host: '127.0.0.1', ssh_port: 22,
                        ssh_user: 'tester', auth_type: 'password', usage: 'both', enabled: true})
                });
                if (!res.ok) throw new Error('machine create failed ' + res.status);
                return (await res.json()).id;
            }"""
        )
        # No SSH: the GPU is seeded in the e2e database, like the collector would.
        conn = sqlite3.connect(DB_PATH, timeout=15)
        try:
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "INSERT INTO devices (uuid, machine_id, kind, \"index\", vendor, model, "
                "present, enabled_for_mining, integrated, occupied_external, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), machine_id, "gpu", 0, "NVIDIA",
                 "NVIDIA GeForce RTX 3070", 1, 1, 0, 0, now, now),
            )
            device_id = int(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()

        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector(f"#psuName-{machine_id}", timeout=15000)

        # 1. Deux alimentations : 650 W Gold (défaut) et 170 W Gold.
        for name, watts in (("PSU 1", "650"), ("PSU 2", "170")):
            await page.fill(f"#psuName-{machine_id}", name)
            await page.fill(f"#psuW-{machine_id}", watts)
            await page.select_option(f"#psuCert-{machine_id}", "gold")
            # `.first` visait le formulaire d'une autre machine quand la suite
            # a déjà créé des machines : rester dans le panneau de CELLE-CI.
            scope = page.locator(f"#psuName-{machine_id}").locator(
                "xpath=ancestor::div[contains(@class,'dev-panel')][1]"
            )
            if await scope.count() == 0:
                scope = page.locator(f"#psuName-{machine_id}").locator(
                    "xpath=ancestor::div[contains(@class,'card')][1]"
                )
            await scope.locator("button:has-text('+ Alimentation')").first.click()
            await page.wait_for_function(
                "n => document.querySelectorAll('[data-psu-row]').length === n",
                arg=1 if name == "PSU 1" else 2,
                timeout=15000,
            )

        psus = await page.evaluate(
            """async (id) => (await (await fetch('/api/machines/' + id + '/psus')).json()).map(p => p.id)""",
            machine_id,
        )
        assert len(psus) == 2

        # 2. GPU réparti : 75 W slot/riser sur PSU 1, le reste sur PSU 2.
        await page.select_option(f'[data-assign-primary="{device_id}"]', str(psus[0]))
        await page.select_option(f'[data-assign-second="{device_id}"]', str(psus[1]))
        await page.fill(f'[data-assign-split="{device_id}"]', "75")
        await page.locator("button:has-text('Affecter')").first.click()
        await page.wait_for_selector('[data-psu-status="orange"]', timeout=15000)

        # 3. Les barres existent, l'alerte orange est visible.
        await page.wait_for_selector('[data-psu-alert="orange"]', timeout=15000)
        assert await page.locator('[data-psu-row]').count() == 2
        assert await page.locator('[data-psu-status="orange"]').count() == 1
        alert = await page.inner_text('[data-psu-alert="orange"]')
        assert "85.3 %" in alert or "85,3" in alert

        assert page_errors == []
        assert console_errors == []
        await browser.close()
