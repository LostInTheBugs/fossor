"""E2E — page « Campagnes » (§4.7) : création, approbation, démarrage simulé, journal.

Le serveur e2e tourne en **simulation** (défaut §5.7) : le démarrage ne lance
aucune commande et journalise celles qui le seraient.  Aucun refus 4xx n'est
provoqué volontairement : Chromium les journalise comme erreurs console.
"""

import sqlite3
import uuid as uuid_lib

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL, defer_newest_proposed_campaign

ADMIN_PASSWORD = "adminpass"
WALLET = "prl1demo" + "q" * 30
SHA = "b" * 64


def seed_device(machine_id: int) -> str:
    """Un device en SQL direct : la page lit la liste des devices de la machine."""
    device_uuid = str(uuid_lib.uuid4())
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        conn.execute(
            "INSERT INTO devices (uuid, machine_id, kind, \"index\", model, present,"
            " enabled_for_mining, integrated, occupied_external, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (device_uuid, machine_id, "gpu", 0, "GPU e2e", 1, 1, 0, 0,
             "2026-11-18T00:00:00+00:00", "2026-11-18T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return device_uuid


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_campaigns_page_cycle_simule(e2e_server):
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
                        name: 'e2e-campagne', host: '127.0.0.1', ssh_port: 1,
                        ssh_user: 'tester', auth_type: 'password',
                        usage: 'gpu', enabled: true
                    })
                });
                if (!res.ok) throw new Error('machine ' + res.status);
                return (await res.json()).id;
            }"""
        )
        seed_device(machine_id)
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
                "name": "Profil e2e campagne",
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

        # 1. La page et son onglet existent dans la navigation unique.
        await page.goto(E2E_URL + "/campaigns", wait_until="domcontentloaded")
        await page.wait_for_selector("#fProfile option", timeout=10000, state="attached")
        nav_text = await page.inner_text("#nav")
        assert "Campagnes" in nav_text
        assert "Campagnes" in await page.inner_text("body")

        # 2. Création par le formulaire (un device coché, sans limite de fin).
        await page.select_option("#fProfile", str(profile_id))
        await page.check("#fDevices input[type=checkbox]")
        await page.check("#fNoEnd")
        await page.click("#createBtn")
        await page.wait_for_selector("#rows tr", timeout=10000)
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('proposée')", timeout=10000
        )
        row = await page.inner_text("#rows")
        assert "FAKE" in row

        # 3. Approbation puis démarrage immédiat (simulation). Le planificateur
        #    (tick 10 s) peut prendre la campagne approuvée avant le clic :
        #    on écarte l'échéance, seul le clic la rend due.
        defer_newest_proposed_campaign()
        await page.click('button[data-act="approve"]')
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('approuvée')", timeout=10000
        )
        await page.click('button[data-act="start"]')
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('en cours')", timeout=15000
        )

        # 4. Journal : commandes simulées, marquées comme telles.
        await page.click('button[data-act="journal"]')
        await page.wait_for_selector("#journal.open li", timeout=10000)
        journal = await page.inner_text("#journal")
        assert "campaign-started" in journal
        assert "simulé" in journal
        assert "faux-mineur" in journal

        # 5. La carte du tableau de bord montre la campagne en cours.
        await page.goto(E2E_URL + "/dashboard", wait_until="domcontentloaded")
        await page.wait_for_function(
            "document.querySelector('#campaignCard').textContent.includes('FAKE')", timeout=15000
        )

        assert console_errors == [], f"console errors: {console_errors}"
        assert page_errors == [], f"page errors: {page_errors}"
        await browser.close()
