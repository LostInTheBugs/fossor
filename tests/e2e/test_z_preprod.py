"""E2E préprod : bandeau d'auth, voyants de routine, ASIC hors campagne.

Aucune machine réelle n'est contactée : la base e2e est semée directement (ORM).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
DB_PATH = f"{E2E_DATA_DIR}/fossor.db"

os.environ["FOSSOR_DATA_DIR"] = E2E_DATA_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"  # l'image fixe /data : on force la base e2e


def _seed() -> dict[str, int | str]:
    """ASIC (jamais pilotable) + machine hôte, et deux états de routine."""
    # Session dédiée : le conftest racine redirige DATABASE_URL vers tmp_path,
    # or on veut la base du serveur e2e (même fichier, autre processus).
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import observability
    from app.ai_config import set_setting
    from app.models import Machine

    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    db = sessionmaker(bind=engine)()
    try:
        asic = Machine(
            name="ASIC-E2E",
            host="192.0.2.170",
            machine_kind="asic",
            asic_driver="iceriver",
            usage="none",
        )
        host = Machine(name="athlon-e2e", host="127.0.0.1", machine_kind="host", usage="cpu")
        db.add_all([asic, host])
        db.commit()
        now = datetime.now(timezone.utc).isoformat()
        states = {
            "backup": {
                "state": "ok",
                "last_run": now,
                "last_ok": now,
                "detail": "fossor-e2e.db",
                "interval_s": 21600,
            },
            "veille": {
                "state": "fail",
                "last_run": now,
                "detail": "appel Hermes refusé",
                "interval_s": 21600,
            },
        }
        set_setting(db, observability.ROUTINE_KEY, states)
        db.commit()
        return {"asic": asic.id, "host": host.id}
    finally:
        db.close()


async def _login(page):
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_preprod_banner_routines_asic_and_simulation(e2e_server):
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
        await _login(page)
        # Semé APRÈS le démarrage : le collecteur de démarrage a déjà écrit son
        # propre état, on veut voir un voyant rouge et un vert côte à côte.
        _seed()

        # 1. Voyants de routine : vert pour la sauvegarde, rouge pour la collecte.
        await page.goto(E2E_URL + "/dashboard", wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => document.getElementById('routineStrip') && "
            "document.getElementById('routineStrip').textContent.includes('Sauvegarde')",
            timeout=15000,
        )
        strip = await page.inner_text("#routineStrip")
        assert "Sauvegarde : OK" in strip
        assert "Veille Hermes : ÉCHEC" in strip

        # 2. Bandeau d'authentification désactivée (réseau de confiance).
        response = await page.request.put(
            E2E_URL + "/api/security/settings",
            data=json.dumps(
                {"auth_enabled": False, "trusted_networks": ["127.0.0.1"]}
            ),
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 200, await response.text()
        await page.goto(E2E_URL + "/dashboard", wait_until="domcontentloaded")
        await page.wait_for_selector("#authBanner", timeout=15000)
        banner = await page.inner_text("#authBanner")
        assert "Authentification désactivée (réseau de confiance)" in banner

        # 3. Les campagnes ne proposent jamais l'ASIC.
        options = await page.evaluate(
            "() => fetch('/api/campaigns/options',{credentials:'same-origin'})"
            ".then(r=>r.json())"
        )
        names = [row["name"] for row in options["machines"]]
        assert "ASIC-E2E" not in names
        assert "athlon-e2e" in names
        await page.goto(E2E_URL + "/campaigns", wait_until="domcontentloaded")
        body = await page.inner_text("body")
        assert "ASIC-E2E" not in body

        # 4. La fiche machine affiche « Simulation » et propose le pilotage réel.
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.wait_for_selector("#machBody tr", timeout=15000)
        await page.wait_for_function(
            "() => document.getElementById('machBody').innerText.includes('athlon-e2e')",
            timeout=15000,
        )
        machines_text = await page.inner_text("#tableWrap")
        upper = machines_text.upper()
        assert "PILOTAGE" in upper
        assert "SIMULATION" in upper
        assert "ASIC — SUIVI" in upper

        # Remise du réglage par défaut : les autres suites e2e se connectent.
        restored = await page.request.put(
            E2E_URL + "/api/security/settings",
            data=json.dumps({"auth_enabled": True}),
            headers={"Content-Type": "application/json"},
        )
        assert restored.status == 200

        await browser.close()

    assert not page_errors, page_errors
    assert not console_errors, console_errors
