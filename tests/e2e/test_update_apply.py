"""E2E : les deux boutons « Appliquer » de Configuration agissent vraiment.

Le serveur de ce module tourne avec un faux GitHub (une release plus récente) et un
``status.json`` de compagnon vivant, ce qui rend le bouton « Mettre à jour vers X »
cliquable sans conteneur Docker.  On vérifie la requête HTTP **et** le fichier
``request.json`` déposé pour le compagnon, puis l'import de configuration avec et
sans aperçu.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

ADMIN_PASSWORD = "adminpass"
E2E_PORT = 18766
MOCK_GITHUB_PORT = 18767
DATA_DIR = Path("/tmp/fossor_e2e_update")
NEW_VERSION = "2026.11.099"

RELEASE = {
    "tag_name": NEW_VERSION,
    "name": f"Release {NEW_VERSION}",
    "body": "Notes de test.",
    "draft": False,
    "prerelease": False,
    "html_url": f"https://example.invalid/fossor/{NEW_VERSION}",
    "published_at": "2026-11-17T00:00:00Z",
}


class _MockGithub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        body = json.dumps([RELEASE]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", '"fossor-c3"')
        self.send_header("X-RateLimit-Remaining", "4999")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence stderr
        return


@pytest.fixture(scope="module")
def update_server():
    """Faux GitHub + uvicorn avec compagnon vivant, sur des ports dédiés."""
    sources = Path(__file__).resolve().parent.parent.parent
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_GITHUB_PORT), _MockGithub)
    thread = threading.Thread(target=mock.serve_forever, daemon=True)
    thread.start()

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    (DATA_DIR / "update").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "update" / "status.json").write_text(
        json.dumps(
            {
                "state": "idle",
                "from": "2026.11.003-c2",
                "to": None,
                "step": None,
                "message": "prêt",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "FOSSOR_DATA_DIR": str(DATA_DIR),
        "DATABASE_URL": f"sqlite:///{DATA_DIR}/fossor.db",
        "FOSSOR_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "FOSSOR_MASTER_KEY": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v=",
        "FOSSOR_UPDATE_CHECK_INTERVAL": "0",
        "FOSSOR_RELEASES_URL": f"http://127.0.0.1:{MOCK_GITHUB_PORT}/releases",
        "FOSSOR_IMAGE": "ghcr.io/lostinthebugs/fossor",
    }
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(E2E_PORT)],
        cwd=str(sources),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    import urllib.request

    for _ in range(60):
        time.sleep(0.3)
        if proc.poll() is not None:
            out = proc.stdout.read().decode()
            err = proc.stderr.read().decode()
            mock.shutdown()
            raise RuntimeError(f"Server failed to start:\nstdout={out}\nstderr={err}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{E2E_PORT}/health", timeout=1)
            break
        except Exception:
            pass
    else:
        proc.kill()
        mock.shutdown()
        raise RuntimeError("Server did not become ready in time")

    yield f"http://127.0.0.1:{E2E_PORT}"

    proc.terminate()
    proc.wait()
    mock.shutdown()


async def _login(page, base_url: str) -> None:
    await page.goto(base_url + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)
    await page.goto(base_url + "/settings", wait_until="domcontentloaded")


@pytest.mark.e2e
async def test_update_button_really_posts_and_writes_request(update_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    posts: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()
        page.on(
            "request",
            lambda req: posts.append({"url": req.url, "body": req.post_data})
            if req.method == "POST" and req.url.endswith("/api/update/apply")
            else None,
        )
        # Le confirm() de la mise à jour doit être accepté, sinon rien ne part.
        page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))

        await _login(page, update_server)

        # Le bouton de mise à jour est un élément distinct de celui de l'import.
        assert await page.locator("#updateApplyBtn").count() == 1
        assert await page.locator("#importApplyBtn").count() == 1

        # Vérification réelle contre le faux GitHub, puis clic sur le bouton.
        await page.click("#checkBtn")
        await page.wait_for_function(
            "() => (document.querySelector('#updateApplyBtn').textContent || '')"
            f".includes('Mettre à jour vers {NEW_VERSION}')",
            timeout=15000,
        )
        assert await page.is_enabled("#updateApplyBtn")
        await page.click("#updateApplyBtn")
        await page.wait_for_function(
            "() => (document.querySelector('#toast').textContent || '')"
            f".includes('Mise à jour vers {NEW_VERSION} demandée')",
            timeout=15000,
        )

        request_file = DATA_DIR / "update" / "request.json"
        assert request_file.exists(), "request.json non écrit"
        request = json.loads(request_file.read_text(encoding="utf-8"))
        assert request["target"] == NEW_VERSION
        assert request["image"] == "ghcr.io/lostinthebugs/fossor"
        assert Path(request["backup"]).is_dir(), "sauvegarde pré-mise à jour absente"

        await context.close()
        await browser.close()

    assert len(posts) == 1, posts
    assert json.loads(posts[0]["body"])["target"] == NEW_VERSION
    assert console_errors == []
    assert page_errors == []


@pytest.mark.e2e
async def test_import_apply_requires_preview_then_applies(update_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    imports: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()
        page.on(
            "request",
            lambda req: imports.append({"url": req.url})
            if req.method == "POST" and "/api/config/import" in req.url
            else None,
        )
        page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))

        await _login(page, update_server)

        document = await page.evaluate(
            """async () => (await fetch('/api/config/export', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({include_secrets: false, passphrase: ''})
            })).json()"""
        )
        export_path = Path("/tmp/fossor_e2e_import.json")
        export_path.write_text(json.dumps(document), encoding="utf-8")
        await page.set_input_files("#impFile", str(export_path))

        # Sans aperçu : le bouton est normalement grisé, on force son état pour
        # éprouver le garde-fou du gestionnaire (« Aperçu requis ») sans écrire.
        await page.evaluate("document.querySelector('#importApplyBtn').disabled = false")
        await page.click("#importApplyBtn")
        await page.wait_for_function(
            "() => (document.querySelector('#toast').textContent || '')"
            ".includes('Aperçu requis avant d\\'appliquer')",
            timeout=10000,
        )
        assert imports == []

        await page.click("#previewBtn")
        await page.wait_for_function(
            "() => (document.querySelector('#toast').textContent || '')"
            ".includes('Aperçu calculé')",
            timeout=15000,
        )
        assert await page.is_enabled("#importApplyBtn")
        await page.click("#importApplyBtn")
        await page.wait_for_function(
            "() => (document.querySelector('#toast').textContent || '')"
            ".includes('Import appliqué')",
            timeout=15000,
        )

        await context.close()
        await browser.close()

    urls = [item["url"] for item in imports]
    assert any("dry_run=true" in url for url in urls), urls
    assert any("dry_run=false" in url for url in urls), urls
    assert console_errors == []
    assert page_errors == []
