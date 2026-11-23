"""6.7 e2e — bouton « Adopter », plan et écran de validation.

La base e2e est partagée : le test crée sa propre machine et son propre mineur
externe (colonne ``external_miners``), puis supprime tout.  Il ne dépend donc
ni d'une collecte réelle ni des machines laissées par les tests précédents.
"""

import json
import sqlite3
import uuid as uuid_lib

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
NAME = "e2e-adoption"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_bouton_adopter_ouvre_le_plan_et_la_validation(e2e_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        machine_id = None
        try:
            await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
            await page.fill("#username", "admin")
            await page.fill("#password", ADMIN_PASSWORD)
            await page.locator("button[type=submit]").click()
            await page.wait_for_url("**/dashboard", timeout=15000)

            machine_id = await page.evaluate(
                """async (name) => {
                    const res = await fetch('/api/machines', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            name: name + '-' + Date.now(), host: '127.0.0.1',
                            ssh_port: 1, ssh_user: 'tester', auth_type: 'password',
                            usage: "gpu", enabled: true
                        })
                    });
                    if (!res.ok) throw new Error('machine ' + res.status);
                    return (await res.json()).id;
                }""",
                NAME,
            )
            # Un GPU occupé par un mineur hors Fossor, vu par la collecte.
            external = [{
                "name": "srbminer", "binary": "/opt/srbminer/srbminer",
                "cmd": "/opt/srbminer/srbminer --algorithm pearlhash",
                "kind": "gpu", "coin": "PRL", "cpu_pct": 0, "api_port": 21555,
            }]
            conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
            try:
                now = "2026-11-23T00:00:00+00:00"
                conn.execute(
                    'INSERT INTO devices (uuid, machine_id, kind, "index", model, present,'
                    " enabled_for_mining, integrated, occupied_external, role,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid_lib.uuid4()), machine_id, "gpu", 0, "RTX 5060 Ti", 1, 1,
                     0, 1, "external", now, now),
                )
                conn.execute("UPDATE machines SET external_miners=? WHERE id=?",
                             (json.dumps(external), machine_id))
                conn.commit()
            finally:
                conn.close()

            await page.goto(E2E_URL + "/machines", wait_until="networkidle", timeout=15000)
            row = page.locator(f'tr:has-text("{NAME}-")').first
            await row.wait_for(state="visible", timeout=15000)
            assert "Lancé hors Fossor" in await page.inner_text("body")
            plan = await page.evaluate(
                """async (id) => {
                    const r = await fetch('/api/machines/' + id + '/adoption-plan');
                    return [r.status, await r.json().catch(() => ({}))];
                }""",
                machine_id,
            )
            assert plan[0] == 200 and plan[1].get("miners"), plan
            # L'alerte vit dans la ligne de détail qui suit la machine.
            detail = row.locator("xpath=following-sibling::tr[1]")
            await detail.locator('.miner-alert button:has-text("Adopter")').first.click()
            try:
                await page.wait_for_selector("#adoptModal.open", timeout=10000)
            except Exception:
                state = await page.evaluate(
                    """() => [typeof window.openAdoption,
                        (document.getElementById('adoptModal') || {}).className,
                        [...document.querySelectorAll('.miner-alert button')].map(b => b.getAttribute('onclick'))]"""
                )
                raise AssertionError(
                    f"modal non ouverte — plan={plan} etat={state} erreurs={page_errors}"
                ) from None
            impact = await page.inner_text("#adoptImpact")
            assert "Impact" in impact, impact
            assert "wallet" in impact, impact
            body = await page.inner_text("#adoptBody")
            assert "srbminer" in body, body
            assert "pearlhash" in body, body
            await page.click("#adoptModal button:has-text('Annuler')")
            assert await page.locator("#adoptModal.open").count() == 0
        finally:
            if machine_id is not None:
                conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
                try:
                    conn.execute("DELETE FROM devices WHERE machine_id=?", (machine_id,))
                    conn.execute("DELETE FROM machines WHERE id=?", (machine_id,))
                    conn.commit()
                finally:
                    conn.close()
            await browser.close()
