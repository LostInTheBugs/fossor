"""E2E — Spec mining (§4.6, étape 3c) : modèle de VM, campagne en mode ``vm``, destruction protégée.

Le serveur e2e tourne en simulation : le démarrage de la campagne journalise les
commandes ``virsh`` / ``qemu-img`` / ``cloud-localds`` sans rien exécuter.
Aucun 4xx n'est provoqué volontairement (Chromium les journalise comme erreurs).
"""

import sqlite3
import uuid as uuid_lib

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL, defer_newest_proposed_campaign

ADMIN_PASSWORD = "adminpass"
WALLET = "prl1demo" + "q" * 30
SHA = "c" * 64
IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/trixie/latest/"
    "debian-12-genericcloud-amd64.qcow2"
)


def seed_cpu(machine_id: int) -> str:
    device_uuid = str(uuid_lib.uuid4())
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        conn.execute(
            "INSERT INTO devices (uuid, machine_id, kind, \"index\", model, threads, present,"
            " enabled_for_mining, integrated, occupied_external, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_uuid, machine_id, "cpu", 0, "CPU e2e", 4, 1, 1, 0, 0,
             "2026-11-18T00:00:00+00:00", "2026-11-18T00:00:00+00:00"),
        )
        conn.execute("UPDATE machines SET vm_host=1 WHERE id=?", (machine_id,))
        conn.commit()
    finally:
        conn.close()
    return device_uuid


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_spec_mining_modele_vm_et_destruction_protegee(e2e_server):
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
                        name: 'e2e-hote-vm', host: '127.0.0.1', ssh_port: 1,
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
                if (!res.ok) throw new Error('profile ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }""",
            {
                "name": "Profil e2e vm",
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

        # 1. Écran Configuration → Spec mining : modèle de VM + mode par défaut.
        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        await page.click("#tabSpec")
        await page.wait_for_selector("#panel-spec", state="visible", timeout=10000)
        await page.wait_for_selector("#vmHost option", timeout=10000, state="attached")
        await page.select_option("#vmHost", str(machine_id))
        await page.fill("#vmImageUrl", IMAGE_URL)
        await page.fill("#vmImageSha", SHA)
        await page.fill("#vmVcpus", "2")
        await page.fill("#vmMem", "2048")
        await page.fill("#vmDisk", "20")
        await page.fill("#vmNodePort", "19333")
        await page.fill("#vmBackupPath", "/home/specminer/data/wallet.dat")
        await page.click("#vmSave")
        await page.wait_for_function(
            "document.querySelector('#vmState').textContent.includes('debian-12-genericcloud')",
            timeout=10000,
        )
        await page.select_option("#specMode", "vm")
        await page.click("#specModeSave")
        await page.wait_for_function(
            "document.querySelector('#specModeState').textContent.includes('vm')", timeout=10000
        )

        # Preuve par l'API : le réglage est bien relu, pas seulement affiché.
        stored = await page.evaluate(
            """async () => (await fetch('/api/spec/settings', {credentials: 'include'})).json()"""
        )
        assert stored["default_mode"] == "vm"
        assert stored["vm_model"]["base_image_sha256"] == SHA
        assert stored["vm_model"]["host_id"] == machine_id

        # 2. Campagne en mode vm : le mode par défaut est proposé, et la campagne
        #    démarre en simulation en journalisant les commandes libvirt.
        await page.goto(E2E_URL + "/campaigns", wait_until="domcontentloaded")
        await page.wait_for_selector("#fProfile option", timeout=10000, state="attached")
        await page.select_option("#fProfile", str(profile_id))
        assert await page.input_value("#fMode") == "vm"
        await page.check(f'#fDevices input[data-machine="{machine_id}"]')
        await page.check("#fNoEnd")
        await page.click("#createBtn")
        await page.wait_for_selector("#rows tr", timeout=10000)
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('proposée')", timeout=10000
        )
        assert "mode vm" in await page.inner_text("#rows")
        # Même course que test_zz_hiveos : le planificateur (tick 10 s) peut
        # démarrer la campagne approuvée avant le clic « Démarrer maintenant ».
        # On écarte l'échéance : seul le clic la rend due.
        defer_newest_proposed_campaign()
        await page.click('button[data-act="approve"]')
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('approuvée')", timeout=10000
        )
        await page.click('button[data-act="start"]')
        await page.wait_for_function(
            "document.querySelector('#rows').textContent.includes('en cours')", timeout=15000
        )
        await page.click('button[data-act="journal"]')
        await page.wait_for_selector("#journal.open li", timeout=10000)
        journal = await page.inner_text("#journal")
        assert "vm-created" in journal
        assert "vm-provisioned" in journal
        assert "virt-install" in journal
        assert "qemu-img create" in journal
        assert "cloud-localds" in journal
        assert "fossor-c" in journal

        # 3. Suivi : la VM de la campagne est rattachée à sa campagne.
        await page.goto(E2E_URL + "/followup", wait_until="domcontentloaded")
        await page.wait_for_selector("[data-vm-card]", timeout=10000)
        vm_text = await page.inner_text("#vmCards")
        assert "fossor-c" in vm_text
        assert "campagne #" in vm_text
        assert "sans IP" not in vm_text

        # 4. Destruction protégée : bouton inactif tant que « DETRUIRE » n'est pas saisi.
        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        await page.click("#tabSpec")
        await page.wait_for_selector("[data-vmact='destroy']", timeout=10000)
        button = page.locator("[data-vmact='destroy']").first
        assert await button.is_disabled() is True
        confirm = page.locator("input[id^='vmConfirm']").first
        await confirm.fill("DETRUIRE")
        assert await button.is_disabled() is False
        await confirm.fill("")
        assert await button.is_disabled() is True

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()
