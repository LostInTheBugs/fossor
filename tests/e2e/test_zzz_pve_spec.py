"""E2E — Configuration → Spec mining avec un hôte Proxmox.

Le modèle Proxmox est saisi puis **enregistré** (PUT ``/spec/pve/settings``),
la page est rechargée et les champs sont relus : le formulaire affiché est
celui du type d'OS de l'hôte choisi (Proxmox), et le plan complet est listé
par ``/spec/pve/plan`` sans aucune exécution. Zéro erreur console.
"""

import json
import sqlite3

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
SHA = "d" * 64
IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/trixie/latest/"
    "debian-12-genericcloud-amd64.qcow2"
)


def _machine_row(machine_id: int) -> None:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        conn.execute(
            "UPDATE machines SET os_kind='proxmox', vm_host=1 WHERE id=?",
            (machine_id,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.e2e
async def test_modele_proxmox_sauvegarde_puis_relu(e2e_server):
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        await page.request.post(
            f"{E2E_URL}/api/login",
            form={"username": "admin", "password": ADMIN_PASSWORD},
        )
        created = await page.request.post(
            f"{E2E_URL}/api/machines",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "name": "pve-e2e",
                "host": "192.0.2.90",
                "ssh_port": 22,
                "ssh_user": "root",
                "auth_type": "fossor_key",
                "os_kind": "proxmox",
                "usage": "cpu",
                "enabled": True,
            }),
        )
        assert created.ok, await created.text()
        machine = await created.json()
        _machine_row(machine["id"])

        await page.goto(f"{E2E_URL}/settings")
        await page.wait_for_selector("#tabSpec")
        await page.click("#tabSpec")
        await page.wait_for_selector("#specHostPick")
        # Attendre la fin du premier chargement avant de changer d'hôte.
        await page.wait_for_function(
            "() => document.getElementById('specHostKind').textContent.includes('libvirt')"
        )

        # L'hôte Proxmox commande le formulaire affiché.
        await page.select_option("#specHostPick", str(machine["id"]))
        await page.wait_for_function(
            "() => document.getElementById('specHostKind').textContent.includes('Proxmox')"
        )
        await page.wait_for_function(
            "() => document.getElementById('pveModelCard').style.display !== 'none'"
        )
        assert await page.eval_on_selector(
            "#vmModelCard", "el => el.style.display"
        ) == "none"

        await page.fill("#pveNode", "pve")
        await page.fill("#pveVmidMin", "9920")
        await page.fill("#pveVmidMax", "9925")
        await page.fill("#pveVcpus", "4")
        await page.fill("#pveMem", "4096")
        await page.fill("#pveDiskGb", "24")
        await page.fill("#pveImageUrl", IMAGE_URL)
        await page.fill("#pveChecksum", SHA)
        await page.select_option("#pveAlgo", "sha256")
        await page.fill("#pveBridge", "vmbr1")
        await page.fill("#pveNodePort", "19444")
        await page.click("#pveSave")
        await page.wait_for_function(
            "() => /modèle/.test(document.getElementById('pveState').textContent)"
        )


        # Rechargement : le modèle est relu depuis l'API, hôte Proxmox compris.
        await page.reload()
        await page.wait_for_selector("#tabSpec")
        await page.click("#tabSpec")
        await page.wait_for_function(
            "() => document.getElementById('pveNode').value === 'pve'"
        )
        assert await page.eval_on_selector("#pveVmidMin", "el => el.value") == "9920"
        assert await page.eval_on_selector("#pveVmidMax", "el => el.value") == "9925"
        assert await page.eval_on_selector("#pveBridge", "el => el.value") == "vmbr1"
        assert await page.eval_on_selector("#pveNodePort", "el => el.value") == "19444"
        assert await page.eval_on_selector("#pveChecksum", "el => el.value") == SHA
        assert "PILOTE PROXMOX" in (await page.inner_text("#specHostKind")).upper()
        conf = await page.request.get(f"{E2E_URL}/api/spec/settings")
        settings = await conf.json()
        assert settings["spec_host_id"] == machine["id"]
        assert settings["spec_host_kind"] == "proxmox"
        pve = await page.request.get(f"{E2E_URL}/api/spec/pve/settings")
        assert (await pve.json())["pve_model"]["host_id"] == machine["id"]

        # Plan complet listé, aucune commande exécutée.
        plan = await page.request.post(
            f"{E2E_URL}/api/spec/pve/plan",
            headers={"Content-Type": "application/json"}, data=json.dumps({}),
        )
        assert plan.ok, await plan.text()
        body = await plan.json()
        commands = [step["command"] for step in body["steps"]]
        assert body["simulated"] is True
        assert any("qm create 9920" in c for c in commands)
        assert commands[-1].startswith("qm destroy 9920")
        assert all(c.split()[0] != "pct" for c in commands)

        await browser.close()
    assert errors == []
