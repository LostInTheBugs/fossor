"""E2E étape 4a — édition d'une règle puis proposition Hermes approuvée.

Le scénario passe par l'écran : l'édition de la règle est faite au formulaire
généré par le schéma, puis relue après rechargement ; la proposition est
déposée par l'API (jeton), visible dans Campagnes et approuvée par le bouton.
"""

import sqlite3
import uuid as uuid_lib

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL
from tests.e2e.factory import insert_device

ADMIN_PASSWORD = "adminpass"
SHA = "b" * 64
COIN = "E2E4A"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_analysis_rules_and_hermes_proposal(e2e_server):
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

        machine_id = await page.evaluate(
            """async () => {
                const res = await fetch('/api/machines', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: 'e2e-analyse-4a', host: '127.0.0.1', ssh_port: 1,
                        ssh_user: 'tester', auth_type: 'password',
                        usage: 'gpu', enabled: true
                    })
                });
                if (!res.ok) throw new Error('machine ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }"""
        )
        insert_device(machine_id)
        conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=15)
        try:
            device_uuid = conn.execute(
                "SELECT uuid FROM devices WHERE machine_id=? ORDER BY id DESC LIMIT 1",
                (machine_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        profile_id = await page.evaluate(
            """async (body) => {
                const res = await fetch('/api/miner/profiles', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (!res.ok) throw new Error('profile ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }""",
            {
                "name": "Profil e2e 4a " + uuid_lib.uuid4().hex[:6],
                "coin": COIN,
                "wallet": "prl1demo" + "q" * 30,
                "pool": "stratum+tcp://pool.exemple.tld:3333",
                "software": "custom",
                "binary_url": "http://127.0.0.1:9/faux-mineur.tar.gz",
                "sha256": SHA,
                "binary_path": "faux-mineur",
                "version": "1.0.0",
                "cmdline": "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} --pool {pool} --gpus {gpus}",
                "execution_mode": "host",
            },
        )
        token = await page.evaluate(
            """async () => {
                const res = await fetch('/api/config/ai/token', {method: 'POST'});
                if (!res.ok) throw new Error('token ' + res.status);
                return (await res.json()).token;
            }"""
        )

        # 1. La page existe et est liée depuis la navigation unique.
        await page.goto(E2E_URL + "/analysis", wait_until="domcontentloaded")
        nav_text = await page.inner_text("#nav")
        assert "Analyse des tokens" in nav_text
        await page.wait_for_selector('tr[data-rule="freshness"]', timeout=15000)

        # 2. Édition au formulaire généré par le schéma, puis relecture réelle.
        await page.click('tr[data-rule="freshness"] button[data-act="edit"]')
        await page.wait_for_selector('#ruleEditor input[data-param="max_hours"]', timeout=10000)
        await page.fill('#ruleEditor input[data-param="max_hours"]', "48")
        await page.click('#ruleEditor button[data-act="save"]')
        await page.wait_for_function(
            "document.querySelector('#rulesMsg').textContent.includes('enregistrée')",
            timeout=10000,
        )
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector('tr[data-rule="freshness"]', timeout=15000)
        row = await page.inner_text('tr[data-rule="freshness"]')
        assert "max_hours=48" in row

        history = await page.evaluate(
            """async () => {
                const res = await fetch('/api/rules/freshness/history');
                if (!res.ok) throw new Error('history ' + res.status);
                return (await res.json()).changes;
            }"""
        )
        assert history, "aucun historique après l'édition"
        assert history[0]["after"]["params"]["max_hours"] == 48
        assert history[0]["actor"] == "admin"

        # 3. Proposition déposée par l'API Hermes (aucun lancement).
        proposal = await page.evaluate(
            """async (payload) => {
                const res = await fetch('/api/hermes/proposals', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json',
                              'Authorization': 'Bearer ' + payload.token},
                    body: JSON.stringify(payload.body)
                });
                const data = await res.json();
                if (!res.ok) throw new Error('proposal ' + res.status + ' ' + JSON.stringify(data));
                return data;
            }""",
            {
                "token": token,
                "body": {
                    "coin": COIN,
                    "profile_id": profile_id,
                    "device_uuids": [device_uuid],
                    "start_at": "2026-12-01T10:00:00+00:00",
                    "end_at": "2026-12-05T10:00:00+00:00",
                    "execution_mode": "host",
                    "note": "proposition e2e 4a",
                    "candidate": {"age_hours": 4, "network_share_pct": 2},
                },
            },
        )
        assert proposal["status"] == "proposed"
        assert proposal["auto_launch"] is False
        assert proposal["stage"] == "4b"
        campaign_id = proposal["campaign"]["id"]

        # 4. Visible dans Campagnes, marquée Hermes, approuvée au bouton.
        await page.goto(E2E_URL + "/campaigns", wait_until="domcontentloaded")
        row_selector = 'tr[data-campaign-id="%s"]' % campaign_id
        await page.wait_for_selector(row_selector, timeout=15000)
        await page.wait_for_function(
            "document.querySelector('" + row_selector + "').textContent.includes('proposée')",
            timeout=10000,
        )
        row_text = await page.inner_text(row_selector)
        assert COIN in row_text
        assert "Hermes" in row_text
        await page.click(row_selector + ' button[data-act="approve"]')
        await page.wait_for_function(
            "document.querySelector('" + row_selector + "').textContent.includes('approuvée')",
            timeout=15000,
        )

        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"
        await browser.close()
        assert E2E_DATA_DIR
