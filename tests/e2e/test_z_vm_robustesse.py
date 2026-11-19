"""E2E — la ligne de campagne reste retrouvable après rechargement.

Création par l'API, rechargement complet de la page, puis arrêt **au clic** sur
le bouton de la ligne repérée par ``data-campaign-id``.
"""

import uuid as uuid_lib

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL
from tests.e2e.test_z_campaigns import ADMIN_PASSWORD, SHA, WALLET, seed_device


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ligne_campagne_retrouvee_apres_rechargement(e2e_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        machine_id = await page.evaluate(
            """async () => {
                const res = await fetch('/api/machines', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: 'e2e-rechargement-' + Date.now(), host: '127.0.0.1',
                        ssh_port: 1, ssh_user: 'tester', auth_type: 'password',
                        usage: 'gpu', enabled: true
                    })
                });
                if (!res.ok) throw new Error('machine ' + res.status);
                return (await res.json()).id;
            }"""
        )
        device_uuid = seed_device(machine_id)
        profile_id = await page.evaluate(
            """async (body) => {
                const res = await fetch('/api/miner/profiles', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (!res.ok) throw new Error('profile ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }""",
            {
                "name": "Profil e2e rechargement " + uuid_lib.uuid4().hex[:6],
                "coin": "FAKE",
                "wallet": WALLET,
                "pool": "stratum+tcp://pool.exemple.tld:3333",
                "software": "custom",
                "binary_url": "http://127.0.0.1:9/faux-mineur.tar.gz",
                "sha256": SHA,
                "binary_path": "faux-mineur",
                "version": "1.0.0",
                "cmdline": (
                    "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} "
                    "--pool {pool} --worker {worker} --gpus {gpus}"
                ),
                "execution_mode": "host",
            },
        )
        campaign_id = await page.evaluate(
            """async (body) => {
                const res = await fetch('/api/campaigns', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (!res.ok) throw new Error('campagne ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }""",
            {
                "profile_id": profile_id,
                "coin": "FAKE",
                "devices": [{"device_uuid": device_uuid, "machine_id": machine_id}],
                "start_at": "2026-01-01T00:00:00+00:00",
                "end_at": None,
                "execution_mode": "host",
            },
        )
        for verb in ("approve", "start"):
            await page.evaluate(
                """async ({id, verb}) => {
                    const res = await fetch('/api/campaigns/' + id + '/' + verb, {method: 'POST'});
                    if (!res.ok) throw new Error(verb + ' ' + res.status + ' ' + await res.text());
                }""",
                {"id": campaign_id, "verb": verb},
            )

        # Rechargement complet : la ligne doit être là, avec son identifiant.
        await page.goto(E2E_URL + "/campaigns", wait_until="domcontentloaded")
        row = page.locator(f'tr[data-campaign-id="{campaign_id}"]')
        await row.wait_for(state="visible", timeout=10000)
        assert "en cours" in (await row.inner_text()).lower()

        # Arrêt au clic, depuis cette ligne.
        await row.locator('button[data-act="stop"]').click()
        await page.wait_for_function(
            "document.querySelector('#toast') && document.querySelector('#toast').textContent"
            f".includes('Campagne #{campaign_id} : stop')",
            timeout=10000,
        )
        await browser.close()
