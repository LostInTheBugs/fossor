"""E2E : ASIC IceRiver KS0 Ultra, faux firmware ``ICM168`` en HTTP local.

Le faux serveur ne répond **que** sur ``POST /user/userpanel`` (corps
``post=4``), comme le firmware réel ; tout ``GET`` renvoie 405 pour prouver que
Fossor interroge bien le panneau et pas ``/api/overview`` de l'apidoc.  Aucun
ASIC de Fred n'est contacté.
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
ICERIVER_PORT = 18774
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "iceriver-userpanel-post4.json"


class _IceRiverHandler(BaseHTTPRequestHandler):
    """Faux panneau IceRiver : ``POST /user/userpanel`` + ``post=4``."""

    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 — API BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        type(self).seen.append({"method": "POST", "path": self.path, "body": body})
        if self.path != "/user/userpanel":
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

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append({"method": "GET", "path": self.path, "body": ""})
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # bruit des tests
        pass


@pytest.fixture()
def fake_iceriver():
    _IceRiverHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", ICERIVER_PORT), _IceRiverHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{ICERIVER_PORT}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _login(page) -> None:
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


async def test_iceriver_asic_added_through_the_panel(e2e_server, fake_iceriver):
    console_errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page = await context.new_page()
        await _login(page)

        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.fill("#aName", "ks0-ultra-test")
        await page.fill("#aHost", fake_iceriver)
        await page.select_option("#aDriver", "auto")
        async with page.expect_response(
            lambda r: r.url.endswith("/api/asics") and r.request.method == "POST"
        ) as caught:
            await page.click("#asicForm button[type=submit]")
        response = await caught.value
        print("ASIC_POST", response.status, (await response.text())[:400])
        row = page.locator("[data-asic]", has_text="ks0-ultra-test")
        await row.wait_for(timeout=20000)

        text = await row.inner_text()
        assert "iceriver" in text
        assert "ventilateurs à 0 tr/min" in text

        asics = await page.evaluate(
            "async () => (await (await fetch('/api/asics')).json()).asics"
        )
        entry = next(item for item in asics if item["name"] == "ks0-ultra-test")
        state = entry["asic"]
        assert entry["driver"] == "iceriver"
        assert state["model"] == "KS0 Ultra"
        assert state["hashrate_hs"] == pytest.approx(271e9)
        assert state["nominal_pct"] == pytest.approx(61.0, abs=0.5)
        assert state["coin"] == "KAS"
        assert entry["health"]["reason"] == "ventilateurs à 0 tr/min"
        # Aucun identifiant de **pool** dans l'état de l'ASIC : la connexion au
        # panneau (2026.11.008) vit sur la machine, jamais dans ``asic``.
        dumped = json.dumps(entry["asic"], ensure_ascii=False)
        assert "REDACTED" not in dumped
        assert not ({"user", "username", "pass", "password"} & set(entry["asic"]))
        assert entry["has_password"] is False
        assert entry["username"] == ""

        await browser.close()

    assert console_errors == [], console_errors
    posts = [item for item in _IceRiverHandler.seen if item["method"] == "POST"]
    assert posts, _IceRiverHandler.seen
    assert posts[0]["path"] == "/user/userpanel"
    assert posts[0]["body"] == "post=4"
    assert all("/api/overview" not in item["path"] for item in _IceRiverHandler.seen)


async def test_iceriver_password_stored_but_never_returned(e2e_server, fake_iceriver):
    """Un mot de passe enregistré est utilisé pour la sonde, jamais renvoyé par l'API."""
    secret = "s3cret-pool-pass"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await context.new_page()
        await _login(page)

        created = await page.evaluate(
            """async (host) => {
                const r = await fetch('/api/asics', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'ks0-mdp', host: host, driver: 'iceriver'}),
                });
                return {status: r.status, body: await r.json()};
            }""",
            fake_iceriver,
        )
        assert created["status"] == 201, created
        machine_id = created["body"]["id"]

        patched = await page.evaluate(
            """async ({id, secret}) => {
                const r = await fetch(`/api/asics/${id}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username: 'pooluser', password: secret}),
                });
                return {status: r.status, text: await r.text()};
            }""",
            {"id": machine_id, "secret": secret},
        )
        assert patched["status"] == 200, patched
        assert secret not in patched["text"], patched["text"]

        asics = await page.evaluate(
            "async () => (await (await fetch('/api/asics')).json()).asics"
        )
        entry = next(item for item in asics if item["name"] == "ks0-mdp")
        assert entry["has_password"] is True
        assert entry["username"] == "pooluser"
        dumped = json.dumps(entry, ensure_ascii=False)
        assert secret not in dumped
        assert "REDACTED" not in dumped

        await browser.close()
