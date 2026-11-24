"""E2E — section ASIC du watchdog et bouton de redémarrage manuel (§6.8)."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL
from tests.e2e.factory import DB_PATH


def _seed_asic() -> int:
    now = datetime.now(timezone.utc).isoformat()
    db = sqlite3.connect(DB_PATH)
    try:
        cursor = db.execute(
            "INSERT INTO machines (uuid, name, host, ssh_port, ssh_user, auth_type, sudo,"
            " os_kind, machine_kind, usage, enabled, simulation_disabled, vm_host,"
            " fossor_host, watchdog_reboot_ok, watchdog_power_ok, platform_power_w,"
            " asic_driver, asic_model, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, '', 'provision_pending', 'none', 'other', 'asic', 'none',"
            " 1, 0, 0, 0, 0, 0, 50.0, 'axeos', 'Bitaxe BM1370', ?, ?)",
            (str(uuid.uuid4()), "bitaxe-e2e-68", "192.0.2.68", now, now),
        )
        db.commit()
        return int(cursor.lastrowid)
    finally:
        db.close()


def _disarm(machine_id: int) -> None:
    """Un test qui arme un ASIC le désarme : la base e2e est partagée."""
    db = sqlite3.connect(DB_PATH)
    try:
        db.execute("UPDATE machines SET simulation_disabled=0 WHERE id=?", (machine_id,))
        db.commit()
    finally:
        db.close()


def _cleanup(machine_id: int) -> None:
    db = sqlite3.connect(DB_PATH)
    try:
        db.execute("DELETE FROM watchdog_events WHERE machine_id=?", (machine_id,))
        db.execute("DELETE FROM watchdog_states WHERE machine_id=?", (machine_id,))
        db.execute("DELETE FROM machine_status WHERE machine_uuid IN"
                   " (SELECT uuid FROM machines WHERE id=?)", (machine_id,))
        db.execute("DELETE FROM machines WHERE id=?", (machine_id,))
        db.commit()
    finally:
        db.close()


async def _login(context) -> None:
    response = await context.request.post(
        f"{E2E_URL}/api/login", form={"username": "admin", "password": "adminpass"})
    assert response.ok, await response.text()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_section_asic_watchdog_et_redemarrage_manuel(e2e_server):
    machine_id = _seed_asic()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            await _login(context)
            page = await context.new_page()
            await page.goto(f"{E2E_URL}/settings", wait_until="networkidle", timeout=15000)
            await page.click("#tabWatchdog")
            await page.wait_for_selector("#panel-watchdog", state="visible", timeout=10000)
            await page.wait_for_selector(f"#wdAsicRow-{machine_id}", timeout=10000)
            row = page.locator(f"#wdAsicRow-{machine_id}")
            assert "bitaxe-e2e-68" in await row.inner_text()
            assert "0 / 3" in await row.inner_text()
            assert await page.is_checked("#wdAsicEnabled") is True
            assert await page.is_checked("#wdAsicRestart") is False

            async with page.expect_response(
                lambda r: f"/api/watchdog/asics/{machine_id}/restart" in r.url
                and r.request.method == "POST"
            ) as info:
                page.once("dialog", lambda dialog: dialog.accept("VALIDER"))
                await row.locator("button").click()
            response = await info.value
            assert response.ok, await response.text()
            payload = await response.json()
            assert payload["outcome"]["action"] == "restart_asic"
            await page.wait_for_selector("#wdAsics", timeout=5000)
            await browser.close()
    finally:
        _disarm(machine_id)
        _cleanup(machine_id)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_interrupteur_de_pilotage_sur_une_carte_asic(e2e_server):
    """L'ASIC reçoit le même interrupteur ; l'arme en réel, sans campagne."""
    machine_id = _seed_asic()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            await _login(context)
            page = await context.new_page()
            await page.goto(f"{E2E_URL}/machines", wait_until="networkidle", timeout=15000)
            row = page.locator("#machBody tr", has_text="bitaxe-e2e-68").first
            await row.wait_for(timeout=10000)
            text = await row.inner_text()
            assert "Simulation" in text and "redémarrage seulement" in text

            page.once("dialog", lambda dialog: dialog.accept("PILOTER"))
            async with page.expect_response(
                lambda r: f"/api/machines/{machine_id}/simulation" in r.url
                and r.request.method == "PUT"
            ) as info:
                await row.get_by_role("button", name="⚡ Réel").click()
            response = await info.value
            assert response.ok, await response.text()
            payload = await response.json()
            assert payload["simulation_disabled"] is True
            assert payload["pilotage"] == "Pilotage réel"

            await page.reload(wait_until="networkidle")
            row = page.locator("#machBody tr", has_text="bitaxe-e2e-68").first
            await row.wait_for(timeout=10000)
            text = await row.inner_text()
            assert "Pilotage réel" in text and "redémarrage seul" in text
            await browser.close()
    finally:
        _disarm(machine_id)
        _cleanup(machine_id)
