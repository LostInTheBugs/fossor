"""E2E — la veille passe par Fossor (§4.8/§4.12).

Un faux Hermes (serveur HTTP local) reçoit le tick, dépose une analyse via
l'API entrante puis répond.  L'écran doit afficher l'état du tick et l'analyse,
sans erreur console.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


class HermesStub:
    """Reçoit ``POST /chat/completions`` et dépose une analyse réelle."""

    def __init__(self, token: str):
        self.token = token
        self.prompts: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.prompts.append(
                    " ".join(
                        message.get("content", "") for message in body.get("messages", [])
                    )
                )
                analysis = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "run": "veille-fossor",
                    "action": "rien",
                    "detail": "Tick e2e : un candidat de démonstration.",
                    "candidates": [
                        {
                            "ticker": "ZZZ4B",
                            "name": "Test Quatre B",
                            "algo": "cuda",
                            "age": "2 h",
                            "links": ["https://exemple.tld/ann"],
                            "verdict": "retenu",
                            "pour": ["tick déclenché par Fossor"],
                            "contre": [],
                        }
                    ],
                }
                request = urllib.request.Request(
                    f"{E2E_URL}/api/hermes/analyses",
                    data=json.dumps(analysis).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {stub.token}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()
                payload = json.dumps({"choices": [{"message": {"content": "[SILENT]"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_veille_tick_from_analysis_screen(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    stub = None

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

        token = await page.evaluate(
            """async () => {
                const res = await fetch('/api/config/ai/token', { method: 'POST' });
                if (!res.ok) throw new Error('token ' + res.status);
                return (await res.json()).token;
            }"""
        )
        stub = HermesStub(token).start()

        configured = await page.evaluate(
            """async (base) => {
                const res = await fetch('/api/config/ai', {
                    method: 'PATCH', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({primary: {
                        provider_type: 'hermes-agent', name: 'Stub e2e',
                        base_url: base, model: 'hermes-agent', timeout_s: 30,
                        api_key: 'stub-key'
                    }})
                });
                if (!res.ok) throw new Error('config ' + res.status + ' ' + await res.text());
                return await res.json();
            }""",
            f"http://127.0.0.1:{stub.port}",
        )
        assert configured["primary"]["has_api_key"] is True
        assert "stub-key" not in json.dumps(configured)

        await page.goto(E2E_URL + "/analysis", wait_until="domcontentloaded")
        assert await page.is_visible("#veilleBtn")
        await page.click("#veilleBtn")

        await page.wait_for_function(
            "document.querySelector('#veilleState').textContent.includes('Terminé')",
            timeout=20000,
        )
        state_text = await page.inner_text("#veilleState")
        assert "1 candidat(s)" in state_text
        assert "1 analyse(s)" in state_text
        await page.wait_for_function(
            "document.querySelector('#analyses').textContent.includes('ZZZ4B')",
            timeout=10000,
        )
        journal = await page.inner_text("#analyses")
        assert "tick déclenché par Fossor" in journal

        assert stub.prompts and "spec-mining-fossor" in stub.prompts[0]
        assert "Aucune commande SSH" in stub.prompts[0]
        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()

    if stub is not None:
        stub.stop()
