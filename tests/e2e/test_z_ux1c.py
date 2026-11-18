"""E2E UX-1c : l'écran Configuration → Mise à jour (§4.19) et le tableau de bord.

Ce module tourne avec un faux GitHub (une release plus récente) et un
``status.json`` de compagnon **vivant** qui conserve une version précédente :
les quatre actions (revenir, oublier, annuler, mettre à jour vers X) sont donc
cliquables sans conteneur Docker, et chacune est vérifiée par son effet réel
(fichier ``request.json`` déposé ou supprimé, réglage relu par l'API).

Nommé ``test_z_ux1c`` pour être collecté en dernier : il écrit dans la base e2e.
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
E2E_PORT = 18768
MOCK_GITHUB_PORT = 18769
DATA_DIR = Path("/tmp/fossor_e2e_ux1c")
NEW_VERSION = "2026.11.099"
PREVIOUS_VERSION = "2026.11.004"
SHOTS = Path(os.environ.get("FOSSOR_SHOTS_DIR", "/tmp/fossor-shots"))

RELEASE = {
    "tag_name": NEW_VERSION,
    "name": f"Release {NEW_VERSION}",
    "body": "Notes de test UX-1c.",
    "draft": False,
    "prerelease": False,
    "html_url": f"https://example.invalid/fossor/{NEW_VERSION}",
    "published_at": "2026-11-18T00:00:00Z",
}


class _MockGithub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        body = json.dumps([RELEASE]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", '"fossor-ux1c"')
        self.send_header("X-RateLimit-Remaining", "4999")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence stderr
        return


@pytest.fixture(scope="module")
def ux1c_server():
    sources = Path(__file__).resolve().parent.parent.parent
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_GITHUB_PORT), _MockGithub)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    (DATA_DIR / "update").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "update" / "status.json").write_text(
        json.dumps(
            {
                "state": "done",
                "updater_version": "2026.11.005",
                "from": PREVIOUS_VERSION,
                "to": "2026.11.005",
                "step": "done",
                "message": "bascule terminée",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "precedente": {
                    "version": PREVIOUS_VERSION,
                    "image": f"ghcr.io/lostinthebugs/fossor:{PREVIOUS_VERSION}",
                },
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


def _request_path() -> Path:
    return DATA_DIR / "update" / "request.json"


def _read_request() -> dict:
    return json.loads(_request_path().read_text(encoding="utf-8"))


async def _login(page, base_url: str, target: str = "/settings") -> None:
    await page.goto(base_url + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)
    if target != "/dashboard":
        await page.goto(base_url + target, wait_until="domcontentloaded")


@pytest.mark.e2e
async def test_update_screen_controls_and_effects(ux1c_server):
    console_errors: list[str] = []
    page_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()
        page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))

        await _login(page, ux1c_server)
        await page.locator("#tabUpdate").click()
        await page.wait_for_selector("#panel-update", state="visible", timeout=10000)
        await page.wait_for_function(
            "() => (document.querySelector('#prevVersion').textContent || '')"
            f".includes('{PREVIOUS_VERSION}')",
            timeout=15000,
        )

        # 1. Présence de chaque contrôle demandé par §4.19.
        intervals = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#updInterval option'))"
            ".map(o => o.value)"
        )
        assert intervals == ["0", "3600", "21600", "43200", "86400"], intervals
        assert await page.input_value("#updInterval") == "3600"
        assert await page.is_checked("#autoEnabled") is False  # on/off, off par défaut
        assert "présent" in (await page.inner_text("#updPresence")).lower()
        assert (await page.inner_text("#updLive")).find("Dernier battement") >= 0
        assert await page.is_disabled("#cancelBtn")  # aucune demande en attente
        assert await page.is_enabled("#rollbackBtn")
        assert await page.is_enabled("#forgetBtn")
        # L'historique n'est plus ici : lien vers Log → Historique des mises à jour.
        assert await page.locator("#updHistory").count() == 0
        assert await page.locator('a[href="/log#update"]').count() >= 1

        # 2. Fréquence : le changement persiste en base.
        await page.select_option("#updInterval", "21600")
        await page.wait_for_function(
            "() => (document.querySelector('#toast').textContent || '')"
            ".includes('Fréquence')",
            timeout=10000,
        )
        settings = await page.evaluate(
            "async () => (await (await fetch('/api/update/status', {credentials: 'include'})).json()).settings"
        )
        assert settings["check_interval_s"] == 21600, settings
        assert settings["auto_apply"] is False, settings

        # 3. Vérifier maintenant : la cible publiée entre dans la liste.
        await page.click("#checkBtn")
        await page.wait_for_function(
            "() => (document.querySelector('#updateApplyBtn').textContent || '')"
            f".includes('Mettre à jour vers {NEW_VERSION}')",
            timeout=15000,
        )
        options = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#targetVersion option')).map(o => o.value)"
        )
        assert NEW_VERSION in options, options

        # 4. Mettre à jour vers X → demande `update` avec la bonne cible.
        await page.click("#updateApplyBtn")
        for _ in range(40):
            if _request_path().exists():
                break
            await asyncio.sleep(0.25)
        assert _request_path().exists(), "request.json non écrit"
        request = _read_request()
        assert request["target"] == NEW_VERSION, request
        assert (request.get("action") or "update") == "update", request

        # 5. Annuler la demande en attente → le fichier disparaît.
        await page.wait_for_function(
            "() => !document.querySelector('#cancelBtn').disabled", timeout=15000
        )
        await page.click("#cancelBtn")
        for _ in range(40):
            if not _request_path().exists():
                break
            await asyncio.sleep(0.25)
        assert not _request_path().exists(), "request.json toujours présent après annulation"

        # 6. Revenir à la version précédente → demande `rollback` vers 004.
        await page.click("#rollbackBtn")
        for _ in range(40):
            if _request_path().exists():
                break
            await asyncio.sleep(0.25)
        assert _request_path().exists(), "request.json non écrit pour le retour arrière"
        request = _read_request()
        assert request["action"] == "rollback", request
        assert request["target"] == PREVIOUS_VERSION, request

        # 7. Oublier la version précédente → demande `forget_previous`.
        await page.click("#cancelBtn")
        for _ in range(40):
            if not _request_path().exists():
                break
            await asyncio.sleep(0.25)
        await page.click("#forgetBtn")
        for _ in range(40):
            if _request_path().exists():
                break
            await asyncio.sleep(0.25)
        assert _request_path().exists(), "request.json non écrit pour l'oubli"
        request = _read_request()
        assert request["action"] == "forget_previous", request
        assert request["target"] == PREVIOUS_VERSION, request
        await page.click("#cancelBtn")
        for _ in range(40):
            if not _request_path().exists():
                break
            await asyncio.sleep(0.25)

        # 8. Version ignorée : affichée, puis « Ne plus l'ignorer » la retire.
        await page.evaluate(
            "async () => fetch('/api/update/settings', {method: 'PUT', credentials: 'include',"
            "headers: {'Content-Type': 'application/json'},"
            f"body: JSON.stringify({{ignorer_auto: '{PREVIOUS_VERSION}'}})}})"
        )
        await page.reload(wait_until="domcontentloaded")
        await page.locator("#tabUpdate").click()
        await page.wait_for_selector("#ignoredWrap", state="visible", timeout=10000)
        assert await page.inner_text("#ignoredVersion") == PREVIOUS_VERSION
        await page.click("#unignoreBtn")
        await page.wait_for_function(
            "async () => (await (await fetch('/api/update/status', {credentials: 'include'})).json())"
            ".settings.ignorer_auto === ''",
            timeout=10000,
        )
        await page.wait_for_selector("#ignoredWrap", state="hidden", timeout=10000)

        SHOTS.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(SHOTS / "ux1c-update.png"))

        await context.close()
        await browser.close()

    assert console_errors == []
    assert page_errors == []


@pytest.mark.e2e
async def test_dashboard_campaign_and_reasons(ux1c_server):
    console_errors: list[str] = []
    page_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()

        await _login(page, ux1c_server, target="/dashboard")
        # La carte ne reste plus sur « Chargement… » : elle dit l'absence.
        await page.wait_for_function(
            "() => !(document.querySelector('#campaignCard').textContent || '')"
            ".includes('Chargement')",
            timeout=15000,
        )
        card = await page.inner_text("#campaignCard")
        assert "Aucune campagne" in card, card
        assert await page.inner_text("#sCampaign") == "aucune"
        # §4.19 : les tuiles muettes portent leur raison.
        cost_why = await page.inner_text("#sCostWhy")
        revenue_why = await page.inner_text("#sRevenueWhy")
        assert cost_why.strip(), "Coût / jour sans raison"
        assert revenue_why.strip(), "Revenu / jour sans raison"
        assert "machine" in cost_why, cost_why
        assert "prix" in revenue_why or "pool" in revenue_why, revenue_why

        SHOTS.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(SHOTS / "ux1c-dashboard.png"))

        await context.close()
        await browser.close()

    assert console_errors == []
    assert page_errors == []
