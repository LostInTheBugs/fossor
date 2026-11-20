"""e2e — machine armée avec le global ON : la fiche dit « Pilotage réel ».

Le serveur e2e tourne avec la simulation globale ON (défaut). La machine semée
est armée par l'API (« PILOTER ») : le démarrage doit partir en réel. Aucun SSH
n'est ouvert : ``FOSSOR_E2E_FAKE_DRIVER=1`` (conftest e2e) fait répondre un
driver factice, qui prouve le chemin réel sans toucher à une machine.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
DB_PATH = f"{E2E_DATA_DIR}/fossor.db"
NAME = "rig-pilotage-e2e"
PROFILE = "profil-pilotage-e2e"

os.environ["FOSSOR_DATA_DIR"] = E2E_DATA_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"


def _seed() -> tuple[int, int]:
    from app.models import Machine, MinerProfile

    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    db = sessionmaker(bind=engine)()
    try:
        now = datetime.now(timezone.utc).isoformat()
        machine = Machine(
            name=NAME,
            host="192.0.2.77",
            ssh_port=22,
            ssh_user="fossor",
            auth_type="fossor_key",
            machine_kind="host",
            usage="cpu",
            enabled=True,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        profile = MinerProfile(
            uuid=str(uuid.uuid4()),
            name=PROFILE,
            coin="PRL",
            wallet="prl1" + "q" * 100 + "+" + "A" * 9 + "/" + "B" * 3 + "=",
            pool="stratum+tcp://pool.invalid:3333",
            worker="",
            software="custom",
            binary_path="miner",
            binary_url="",
            version="1.0.0",
            sha256="",
            cmdline="/opt/fossor/miners/custom/1.0.0/miner --wallet {wallet} --pool {pool}",
            api_port=3333,
            execution_mode="host",
            threads=1,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        machine.default_miner_profile_id = profile.id
        db.commit()
        return machine.id, profile.id
    finally:
        db.close()


def _cleanup() -> None:
    engine = create_engine(f"sqlite:///{DB_PATH}")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM devices WHERE machine_id IN "
                          "(SELECT id FROM machines WHERE name = :name)"), {"name": NAME})
        conn.execute(text("DELETE FROM miner_profiles WHERE name = :profile"), {"profile": PROFILE})
        conn.execute(text("DELETE FROM machines WHERE name = :name"), {"name": NAME})
    engine.dispose()


async def _login(page):
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_machine_armee_pilote_en_reel_malgre_le_global(e2e_server):
    machine_id, _ = _seed()
    page_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()
        await _login(page)

        armed = await page.request.put(
            E2E_URL + f"/api/machines/{machine_id}/simulation",
            data=json.dumps({"real": True, "confirm": "PILOTER"}),
            headers={"Content-Type": "application/json"},
        )
        assert armed.status == 200, await armed.text()
        body = await armed.json()
        assert body["simulation_disabled"] is True
        assert body["simulated"] is False
        assert body["pilotage"] == "Pilotage réel"

        # Le global est resté ON : la fiche doit quand même dire « Pilotage réel ».
        settings = await page.request.get(E2E_URL + "/api/miner/settings")
        assert (await settings.json())["simulation"] is True

        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.wait_for_selector("#machBody tr", timeout=15000)
        await page.wait_for_function(
            "() => document.getElementById('machBody').innerText.includes('rig-pilotage-e2e')",
            timeout=15000,
        )
        upper = (await page.inner_text("#tableWrap")).upper()
        assert "PILOTAGE RÉEL" in upper

        started = await page.evaluate(
            "(id) => fetch('/api/machines/'+id+'/miner/start',{method:'POST',"
            "credentials:'same-origin',headers:{'Content-Type':'application/json'},"
            "body:'{}'}).then(r=>r.json())",
            machine_id,
        )
        assert started.get("simulated") is False, started
        assert started.get("fake_driver") is True, started
        assert started.get("mode") == "fake", started

        await browser.close()

    _cleanup()
    assert not page_errors, page_errors
