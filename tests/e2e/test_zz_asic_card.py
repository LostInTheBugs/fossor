"""E2E : carte ASIC sur le tableau de bord (2026.11.008).

Un Bitaxe est ajouté par le formulaire, servi par un faux AxeOS local qui rend
la fixture réelle (AxeOS v2.12.2, Gamma 602).  La pastille doit afficher le
modèle et le firmware, la puissance **mesurée par l'ASIC** de la fixture, et
jamais « 0 device(s) ».
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"
AXEOS_PORT = 18773
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "axeos-system-info.json"


class _AxeOSHandler(BaseHTTPRequestHandler):
    """Faux AxeOS : ``GET /api/system/info`` seulement."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/api/system/info":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = FIXTURE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        # Pas de panneau IceRiver ici : la détection doit passer à AxeOS.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def fake_axeos():
    server = ThreadingHTTPServer(("127.0.0.1", AXEOS_PORT), _AxeOSHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{AXEOS_PORT}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dashboard_asic_card_shows_model_power_and_no_devices(e2e_server, fake_axeos):
    fixture = json.loads(FIXTURE.read_text())
    watts = str(int(fixture["power"] + 0.5))
    console_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        context.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page = await context.new_page()
        await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#aName", "e2e-asic")
        await page.fill("#aHost", fake_axeos)
        await page.select_option("#aDriver", "auto")
        await page.locator("#asicForm button[type=submit]").click()
        # Attendre **notre** ligne : un autre test a déjà déclaré un ASIC, donc
        # ``tr[data-asic]`` seul rendrait la main avant la fin du POST.
        await page.wait_for_selector('tr[data-asic]:has-text("e2e-asic")', timeout=20000)

        await page.goto(E2E_URL + "/dashboard", wait_until="domcontentloaded", timeout=15000)
        pill = page.locator('.pill[data-machine="e2e-asic"]')
        await pill.wait_for(timeout=15000)
        text = await pill.inner_text()

        assert "device(s)" not in text, text
        assert "Bitaxe" in text, text
        assert fixture.get("axeOSVersion") in text, text
        assert watts + " W" in text, text
        assert "mesurée par l'ASIC" in text, text
        assert console_errors == [], console_errors

        await browser.close()
