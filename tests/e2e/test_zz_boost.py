"""e2e — Campagnes → Boost : pool, écran de validation, lancement, annulation."""

from __future__ import annotations

import os
import sqlite3
import uuid as uuid_lib
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL, defer_newest_proposed_campaign

ADMIN_PASSWORD = "adminpass"
WALLET = "prl1testwalletfossor0000000000000000000000000000000000"
SHA = "c" * 64
SHOTS = os.environ.get("FOSSOR_E2E_SHOTS", "")


async def shot(page, name: str) -> None:
    """Capture réelle pour le rapport, uniquement si le dossier est fourni."""
    if SHOTS:
        await page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def seed_device(machine_id: int) -> str:
    """Un GPU dans le pool de boost, en SQL direct (aucune machine réelle)."""
    device_uuid = str(uuid_lib.uuid4())
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        conn.execute(
            "INSERT INTO devices (uuid, machine_id, kind, \"index\", model, present,"
            " enabled_for_mining, integrated, occupied_external, role, boost_pool,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_uuid, machine_id, "gpu", 0, "GPU boost e2e", 1, 1, 0, 0,
             "default_and_spec", 1, "2026-11-21T00:00:00+00:00", "2026-11-21T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return device_uuid


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_boost_pool_validation_launch_and_cancel(e2e_server):
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
                        name: 'e2e-boost', host: '127.0.0.1', ssh_port: 1,
                        ssh_user: 'tester', auth_type: 'password',
                        usage: 'gpu', enabled: true
                    })
                });
                if (!res.ok) throw new Error('machine ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }"""
        )
        device_uuid = seed_device(machine_id)
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
                "name": "Profil boost e2e",
                "coin": "NEWC",
                "wallet": WALLET,
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
        campaign_id = await page.evaluate(
            """async (payload) => {
                const res = await fetch('/api/campaigns', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                if (!res.ok) throw new Error('campaign ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }""",
            {
                "coin": "NEWC",
                "profile_id": profile_id,
                "start_at": datetime.now(timezone.utc).isoformat(),
                "devices": [{"machine_id": machine_id, "device_uuid": device_uuid}],
            },
        )
        # `start_at` repoussé avant l'approbation : le planificateur ne peut pas
        # prendre la campagne, aucun 409 n'est toléré ensuite.
        defer_newest_proposed_campaign(60)
        await page.evaluate(
            """async (id) => {
                const res = await fetch('/api/campaigns/' + id + '/approve', {method: 'POST'});
                if (!res.ok) throw new Error('approve ' + res.status + ' ' + await res.text());
            }""",
            campaign_id,
        )

        await page.goto(E2E_URL + "/campaigns", wait_until="networkidle")
        await page.wait_for_selector("#boostPool input[data-boost-machine]", timeout=15000)
        pool_text = await page.inner_text("#boostPool")
        assert "e2e-boost" in pool_text and "GPU 0" in pool_text, pool_text
        await shot(page, "boost-pool")

        await page.check(f'#boostPool input[value="{device_uuid}"]')
        await page.select_option("#boostCampaign", str(campaign_id))
        await page.click("#boostPreview")
        await page.wait_for_selector("#boostPanel table", timeout=10000)
        panel = await page.inner_text("#boostPanel")
        assert "Hashrate Pearl perdu" in panel
        assert "Coût électrique estimé" in panel
        assert "Revenu estimé du coin boosté" in panel
        await shot(page, "boost-validation")

        async with page.expect_response(
            lambda r: r.url.endswith("/api/boosts") and r.request.method == "POST"
        ) as info:
            await page.click("#boostLaunch")
        response = await info.value
        body = await response.text()
        assert response.ok, (response.status, body)
        await page.wait_for_function(
            """() => Array.from(document.querySelectorAll('#boostRows tr'))
                .some((row) => row.textContent.includes('en cours'))""",
            timeout=15000,
        )

        await shot(page, "boost-en-cours")
        await page.click('#boostRows button[data-boost-act="cancel"]')
        await page.wait_for_function(
            """() => Array.from(document.querySelectorAll('#boostRows tr'))
                .some((row) => row.textContent.includes('terminé'))""",
            timeout=15000,
        )

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()
