"""E2E 025-b — Suivi → Alimentations suit tout le parc ; Machines n'a plus le détail.

Machine fictive (192.0.2.63) avec un GPU dont la puissance est collectée :
le cadre Alimentations de Suivi doit afficher l'alimentation, le total du parc
et son coût ; la fiche Machines ne garde qu'une ligne de résumé et un lien.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL
from tests.e2e.factory import create_machine, insert_device

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_alim_suivi_fleet_total_and_machines_summary(e2e_server):
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
        machine = await create_machine(page, "rig-alim-025b", host="192.0.2.63")
        machine_id = machine["id"]
        device_id = insert_device(
            machine_id, kind="gpu", vendor="NVIDIA", model="NVIDIA GeForce RTX 3060"
        )

        status = await page.evaluate(
            """async (id) => {
                const r = await fetch('/api/machines/' + id + '/psus', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'PSU fictive 025b', rated_w: 750,
                                          certification: 'gold'})
                });
                return r.status;
            }""",
            machine_id,
        )
        assert status == 201, status

        # Une mesure de puissance device, comme le collecteur l'écrit.
        conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
        try:
            machine_uuid = conn.execute(
                "SELECT uuid FROM machines WHERE id=?", (machine_id,)
            ).fetchone()[0]
            device_uuid = conn.execute(
                "SELECT uuid FROM devices WHERE id=?", (device_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO metrics (machine_uuid, device_uuid, ts, power_w) VALUES (?,?,?,?)",
                (machine_uuid, device_uuid, datetime.now(timezone.utc).isoformat(), 210.0),
            )
            conn.commit()
        finally:
            conn.close()

        # ── Suivi → Alimentations : tout le parc ────────────────────────
        await page.goto(E2E_URL + "/followup", wait_until="domcontentloaded")
        await page.wait_for_selector("#psuTotal", timeout=15000)
        await page.wait_for_function(
            "() => document.querySelector('#psu').textContent.includes('PSU fictive 025b')",
            timeout=15000,
        )
        psu_text = (await page.inner_text("#psu")).lower()
        assert "rig-alim-025b" in psu_text, psu_text
        assert "à la prise" in psu_text, psu_text
        assert "rendement" in psu_text, psu_text
        total_text = await page.inner_text("#psuTotal")
        assert "€" in total_text, total_text
        assert await page.locator('[data-psu-machine]').count() >= 1
        assert await page.locator('[data-psu-alim]').count() >= 1

        # ── Machines : une ligne de résumé, plus de détail ─────────────
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.wait_for_selector(f"#psuSummary-{machine_id}", timeout=15000)
        summary = await page.inner_text(f"#psuSummary-{machine_id}")
        assert "à la prise" in summary, summary
        assert await page.locator(
            f'#psuSummary-{machine_id} a[href="/followup#psu"]'
        ).count() == 1
        panel = page.locator(f"#psuSummary-{machine_id}").locator(
            "xpath=ancestor::div[contains(@class,'card')][1]"
        )
        panel_text = (await panel.inner_text()).lower()
        assert "conso totale" not in panel_text, panel_text
        assert "rendement" not in panel_text, panel_text

        assert page_errors == []
        assert console_errors == []
        await browser.close()
