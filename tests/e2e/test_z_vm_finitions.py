"""E2E — fiche VM : contrôle de sauvegarde puis destruction au clic (5e essai).

Le bouton « Contrôle sauvegarde » appelait ``/api/vms/{id}/backup`` (inexistant,
404) : il doit appeler la route réelle ``/api/vms/{id}/backup-check``. La
destruction est une action séparée, confirmée par le mot ``DETRUIRE`` et
déclenchée **au clic** depuis la fiche.

Le serveur e2e tourne en simulation : la destruction ne touche aucun
hyperviseur, mais le clic, la route et la confirmation sont réels — l'effet
serveur de la destruction est prouvé par l'intégration (VM réelle détruite).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid as uuid_lib
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL, defer_newest_proposed_campaign
from tests.e2e.test_z_spec_vms import (  # noqa: F401 — mêmes graines que l'écran
    ADMIN_PASSWORD,
    IMAGE_URL,
    SHA,
    WALLET,
    seed_cpu,
)


def _vm_id() -> int:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        row = conn.execute("SELECT id FROM spec_vms ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None, "aucune VM enregistrée par la campagne"
        return int(row[0])
    finally:
        conn.close()


def _verify_backup_state(vm_id: int) -> None:
    """Verdict de sauvegarde vérifié, comme après un vrai contrôle réussi."""
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        conn.execute(
            "UPDATE spec_vms SET backup_state=?, backup_checked_at=?, backup_path=? WHERE id=?",
            (
                json.dumps(
                    {
                        "verified": True,
                        "size": 4096,
                        "reason": "sauvegarde vérifiée (e2e)",
                        "checked_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                ),
                datetime.now(timezone.utc).isoformat(),
                "/home/specminer/data/wallet.dat",
                vm_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_fiche_vm_controle_puis_destruction_au_clic(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    dialogs: list[str] = []
    responses: list[tuple[str, int]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))

        page = await context.new_page()
        page.on("response", lambda r: responses.append((r.url, r.status)))
        page.on(
            "dialog",
            lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.accept())),
        )

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
                        name: 'e2e-fiche-vm', host: '127.0.0.1', ssh_port: 1,
                        ssh_user: 'tester', auth_type: 'password',
                        usage: 'cpu', enabled: true
                    })
                });
                if (!res.ok) throw new Error('machine ' + res.status);
                return (await res.json()).id;
            }"""
        )
        seed_cpu(machine_id)
        profile_id = await page.evaluate(
            """async (body) => {
                const res = await fetch('/api/miner/profiles', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (!res.ok) throw new Error('profile ' + res.status);
                return (await res.json()).id;
            }""",
            {
                "name": "Profil e2e fiche",
                "coin": "NNN",
                "wallet": WALLET,
                "pool": "solo",
                "software": "custom",
                "binary_url": "http://127.0.0.1:9/faux-mineur.tar.gz",
                "sha256": SHA,
                "binary_path": "faux-mineur",
                "version": "1.0.0",
                "cmdline": (
                    "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} "
                    "--pool {pool} --threads {threads}"
                ),
                "execution_mode": "vm",
                "threads": 2,
            },
        )

        # 1. Modèle de VM et campagne en mode vm (simulation).
        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        await page.click("#tabSpec")
        await page.wait_for_selector("#vmHost option", timeout=10000, state="attached")
        await page.select_option("#vmHost", str(machine_id))
        await page.fill("#vmImageUrl", IMAGE_URL)
        await page.fill("#vmImageSha", SHA)
        await page.fill("#vmNodePort", "19333")
        await page.fill("#vmBackupPath", "/home/specminer/data/wallet.dat")
        await page.click("#vmSave")
        await page.wait_for_function(
            "document.querySelector('#vmState').textContent.includes('debian-12-genericcloud')",
            timeout=10000,
        )

        await page.goto(E2E_URL + "/campaigns", wait_until="domcontentloaded")
        await page.wait_for_selector("#fProfile option", timeout=10000, state="attached")
        await page.select_option("#fProfile", str(profile_id))
        await page.check(f'#fDevices input[data-machine="{machine_id}"]')
        await page.check("#fNoEnd")
        await page.click("#createBtn")
        await page.wait_for_selector("#rows tr", timeout=10000)
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('proposée')", timeout=10000
        )
        # Le planificateur (tick 10 s) peut prendre la campagne approuvée
        # avant le clic : on écarte l'échéance, seul le clic la rend due.
        defer_newest_proposed_campaign()
        await page.click('button[data-act="approve"]')
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('approuvée')", timeout=10000
        )
        await page.click('button[data-act="start"]')
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('en cours')", timeout=15000
        )

        # 2. Fiche VM : la liste est chargée avec l'écran (pas après un autre appel).
        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        await page.click("#tabSpec")
        await page.wait_for_selector("[data-vmact='backup']", timeout=10000)
        vm_id = _vm_id()
        assert await page.locator("[data-vmact='backup']").first.get_attribute("data-vm") == str(vm_id)

        # 3. Contrôle de sauvegarde AU CLIC : la route réelle répond 200.
        await page.locator("[data-vmact='backup']").first.click()
        for _ in range(60):
            if any(url.endswith(f"/api/vms/{vm_id}/backup-check") for url, _ in responses):
                break
            await page.wait_for_timeout(100)
        checks = [
            (url, status)
            for url, status in responses
            if url.endswith(f"/api/vms/{vm_id}/backup-check")
        ]
        print("\n### contrôle sauvegarde :", checks)
        print("dialogue :", dialogs[-1] if dialogs else "(aucun)")
        assert checks, "le bouton n'a pas appelé /backup-check"
        assert checks[0][1] == 200, checks
        assert not [1 for url, _ in responses if url.endswith(f"/api/vms/{vm_id}/backup")], (
            "route inexistante /backup encore appelée"
        )
        assert dialogs and "Sauvegarde" in dialogs[-1]

        # 4. Destruction au clic depuis la fiche (confirmation DETRUIRE).
        _verify_backup_state(vm_id)
        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        await page.click("#tabSpec")
        await page.wait_for_selector(f"#vmDestroy{vm_id}", timeout=10000)
        await page.locator(f"#vmConfirm{vm_id}").fill("DETRUIRE")
        await expect(page.locator(f"#vmDestroy{vm_id}")).to_be_enabled()
        await page.locator(f"#vmDestroy{vm_id}").click()
        for _ in range(60):
            if any(url.endswith(f"/api/vms/{vm_id}/destroy") for url, _ in responses):
                break
            await page.wait_for_timeout(100)
        destroy = [
            (url, status) for url, status in responses if url.endswith(f"/api/vms/{vm_id}/destroy")
        ]
        print("### destruction :", destroy)
        assert destroy, "le bouton Détruire n'a pas appelé la route de destruction"
        assert destroy[0][1] == 200, destroy
        assert not [1 for _, status in responses if status == 404]

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()
