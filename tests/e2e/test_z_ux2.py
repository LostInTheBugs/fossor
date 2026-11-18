"""E2E UX-2 : sous-onglet « Token par défaut » et ajout d'un ASIC AxeOS.

Le classement lit WhatToMine en direct (réseau autorisé, lecture seule) ; l'ASIC
est servi par un **faux serveur HTTP** qui renvoie la fixture AxeOS réelle —
aucun ASIC de Fred n'est contacté.

Nommé ``test_z_ux2`` pour être collecté en dernier : il écrit dans la base e2e.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL
from tests.e2e.factory import create_machine, insert_device

ADMIN_PASSWORD = "adminpass"
ASIC_PORT = 18771
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "axeos-system-info.json"
SHOTS = Path(os.environ.get("FOSSOR_SHOTS_DIR", "/tmp/fossor-shots"))


class _AxeOSHandler(BaseHTTPRequestHandler):
    """Sert ``/api/system/info`` avec la fixture réelle ; tout le reste en 404."""

    def do_GET(self):  # noqa: N802 - API BaseHTTPRequestHandler
        if self.path.split("?")[0] != "/api/system/info":
            self.send_response(404)
            self.end_headers()
            return
        body = FIXTURE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        return


@pytest.fixture(scope="module")
def fake_axeos():
    server = ThreadingHTTPServer(("127.0.0.1", ASIC_PORT), _AxeOSHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{ASIC_PORT}"
    server.shutdown()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_tokens_tab_and_axeos_asic(e2e_server, fake_axeos):
    SHOTS.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page = await context.new_page()
        await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        machine = await create_machine(page, "ux2-rig")
        insert_device(machine["id"], kind="gpu", model="NVIDIA GeForce RTX 3070")
        insert_device(machine["id"], kind="cpu", index=None, vendor="AMD",
                      model="AMD Ryzen 5 3600")
        await page.evaluate(
            """async () => {
                await fetch('/api/coins', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ticker: 'ZZZ', name: 'Coin hors liste', kind: 'both'})});
            }"""
        )

        # 1. Sous-onglet « Token par défaut » : liste classée + source datée.
        await page.goto(E2E_URL + "/mined", wait_until="domcontentloaded")
        await page.click('button[data-panel="default"]')
        await page.wait_for_selector("#rentDevices table", timeout=45000)
        body = await page.inner_text("#panel-default")
        assert "NVIDIA GeForce RTX 3070" in body
        assert "whattomine" in body.lower()
        sources = await page.inner_text("#rentSources")
        assert "whattomine" in sources.lower()
        assert "BTC" in sources  # prix BTC + tarif affichés
        first_row = await page.inner_text("#rentDevices tbody tr:first-child")
        assert "Pearl" in first_row or "PRL" in first_row
        await page.screenshot(path=str(SHOTS / "ux2-tokens.png"))

        # 2. Choix hors liste : un coin du catalogue appliqué au modèle.
        await page.select_option("#rentOther", "ZZZ")
        await page.fill("#rentOtherModel", "NVIDIA GeForce RTX 3070")
        await page.select_option("#rentOtherKind", "gpu")
        await page.click("#rentOtherSave")
        await page.wait_for_timeout(800)
        stored = await page.evaluate(
            """async () => (await (await fetch('/api/tokens/default')).json()).by_model"""
        )
        assert stored.get("gpu:NVIDIA GeForce RTX 3070") == "ZZZ", stored

        # 3. ASIC AxeOS ajouté par IP, servi par le faux serveur.
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.fill("#aName", "bitaxe-test")
        await page.fill("#aHost", fake_axeos)
        await page.select_option("#aDriver", "auto")
        await page.click("#asicForm button[type=submit]")
        await page.wait_for_selector("[data-asic]", timeout=20000)
        row = await page.inner_text("[data-asic]")
        assert "bitaxe-test" in row
        assert "axeos" in row
        assert "domaine de hachage inactif" in row
        assert "75 %" in row
        await page.screenshot(path=str(SHOTS / "ux2-asic.png"))

        await browser.close()

    assert console_errors == [], console_errors
