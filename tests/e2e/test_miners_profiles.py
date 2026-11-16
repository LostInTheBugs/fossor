"""e2e — écran Profils (§4.5), section « Mineur » par machine, bandeau simulation (§5.7).

Aucune connexion SSH réelle n'est faite : le mode simulation est actif par
défaut, et le test vérifie justement que le démarrage renvoie des commandes
sans rien exécuter.
"""

import sqlite3
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
SHA = "a" * 64
CMDLINE = (
    "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} --pool {pool} "
    "--worker {worker} --gpus {gpus} --api-port {api_port}"
)


def _seed_gpu(machine_id: int) -> None:
    """Seed one checked GPU directly: detection needs SSH, the panel only needs a device."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        conn.execute(
            "INSERT INTO devices (uuid, machine_id, kind, \"index\", model, present,"
            " enabled_for_mining, integrated, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "44444444-4444-4444-4444-444444444444",
                machine_id,
                "gpu",
                0,
                "GPU e2e",
                1,
                1,
                0,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_profiles_screen_and_miner_panel(e2e_server):
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

        # 1. Bandeau simulation permanent, dès le tableau de bord.
        await page.wait_for_selector("#simBanner", state="visible", timeout=10000)
        assert "Simulation : aucune commande n'est exécutée" in await page.inner_text("#simBanner")

        # 2. Écran Profils : création par le formulaire, refus d'un wallet injecté.
        await page.goto(E2E_URL + "/profiles", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("#simBanner", state="visible", timeout=10000)
        await page.click("#addBtn")
        await page.select_option("#fSoftware", "custom")
        await page.fill("#fName", "E2E faux mineur")
        await page.fill("#fCoin", "FKE")
        await page.fill("#fWallet", "prl1demo" + "q" * 30)
        await page.fill("#fPool", "stratum+tcp://pool.exemple.tld:3333")
        await page.fill("#fUrl", "http://exemple.tld/faux-mineur.tar.gz")
        await page.fill("#fVersion", "1.0.0")
        await page.fill("#fSha", SHA)
        await page.fill("#fBinaryPath", "faux-mineur")
        await page.fill("#fCmdline", CMDLINE)
        await page.fill("#fApiPort", "21399")
        await page.click("#saveBtn")
        await page.wait_for_selector("#body tr", timeout=10000)
        assert "E2E faux mineur" in await page.inner_text("#body")
        await page.wait_for_selector("#defaultChip", timeout=10000)
        await page.click("#body tr button.btn.outline")
        await page.wait_for_function(
            "() => document.querySelector('#defaultChip').textContent.includes('E2E faux mineur')",
            timeout=10000,
        )
        print("\n### TOKEN PAR DÉFAUT:", await page.inner_text("#defaultChip"))

        # 3. Machine en clé Fossor + section Mineur.
        machine = await page.evaluate(
            """async () => {
                const r = await fetch('/api/machines', {method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'e2e-miner', host: 'cible.test', ssh_user: 'fossor',
                        auth_type: 'fossor_key', sudo: 'sudo without password'})});
                return r.json();
            }"""
        )
        _seed_gpu(machine["id"])

        await page.goto(E2E_URL + "/machines", wait_until="networkidle", timeout=15000)
        await page.wait_for_selector("#machBody tr", timeout=10000)
        await page.locator("#machBody tr", has_text="e2e-miner").locator(
            "button:has-text('⛏️ Mineur')"
        ).first.click()
        await page.wait_for_selector("#minerPanel", state="visible", timeout=10000)
        hint = await page.inner_text("#minerHint")
        print("\n### PANNEAU MINEUR:", hint)
        assert "GPU utilisées : 0" in hint
        assert "active" in hint

        options = await page.eval_on_selector_all("#minerProfile option", "els => els.map(e => e.textContent)")
        assert any("E2E faux mineur" in text for text in options), options

        await page.click("#minerStart")
        await page.wait_for_function(
            "() => document.querySelector('#minerOut').textContent.includes('simulated')",
            timeout=15000,
        )
        out = await page.inner_text("#minerOut")
        print("\n### START DEPUIS L'UI (extrait):", out[:400])
        assert '"simulated": true' in out
        assert "sha256sum" in out
        assert (await page.inner_text("#minerState")).lower() == "simulé"

        await browser.close()

    assert console_errors == [], console_errors
    assert page_errors == [], page_errors
