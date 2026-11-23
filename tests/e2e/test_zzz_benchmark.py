"""e2e — benchmark à la demande : validation, simulation, résultats, mode."""

from __future__ import annotations

import os
import sqlite3
import uuid as uuid_lib

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
WALLET = "prl1testwalletfossor0000000000000000000000000000000000"
SHA = "c" * 64
SHOTS = os.environ.get("FOSSOR_E2E_SHOTS", "")


async def shot(page, name: str) -> None:
    if SHOTS:
        await page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def seed_device(machine_id: int, profile_id: int) -> str:
    """Un GPU dans le parc, en SQL direct (aucune machine réelle)."""
    device_uuid = str(uuid_lib.uuid4())
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        conn.execute(
            "INSERT INTO devices (uuid, machine_id, kind, \"index\", model, present,"
            " enabled_for_mining, integrated, occupied_external, role,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_uuid, machine_id, "gpu", 0, "RTX 5060 Ti", 1, 1, 0, 0,
             "default_and_spec", "2026-11-23T00:00:00+00:00", "2026-11-23T00:00:00+00:00"),
        )
        conn.execute("UPDATE machines SET default_miner_profile_id=? WHERE id=?",
                     (profile_id, machine_id))
        conn.commit()
    finally:
        conn.close()
    return device_uuid


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_benchmark_validation_simulation_resultats_mode(e2e_server):
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

        machine_id = await page.evaluate(
            """async () => {
                const res = await fetch('/api/machines', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: 'e2e-bench', host: '192.0.2.31', ssh_port: 1,
                        ssh_user: 'tester', auth_type: 'password',
                        usage: 'gpu', enabled: true
                    })
                });
                if (!res.ok) throw new Error('machine ' + res.status + ' ' + await res.text());
                return (await res.json()).id;
            }"""
        )
        profile_id = await page.evaluate(
            """async () => {
                const res = await fetch('/api/miner/profiles', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: 'e2e-bench-prl', coin: 'PRL',
                        wallet: 'prl1testwalletfossor0000000000000000000000000000000000',
                        pool: 'stratum+tcp://pearl-eu2.luckypool.io:3360',
                        software: 'srbminer', api_port: 0,
                        cmdline: '/opt/fossor/miners/srbminer/2.0.0/opt/fossor/miners/srbminer -a pearlhash',
                        execution_mode: 'host', threads: 1,
                        binary_url: 'http://192.0.2.1/srbminer.tar.gz',
                        sha256: 'c'.repeat(64), version: '2.0.0',
                        binary_path: '/opt/fossor/miners/srbminer'
                    })
                });
                const body = await res.json();
                if (!res.ok) throw new Error('profil ' + res.status + ' ' + JSON.stringify(body));
                return body.id;
            }"""
        )
        seed_device(machine_id, profile_id)

        await page.goto(E2E_URL + f"/benchmark?machine={machine_id}",
                        wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_function(
            "() => document.getElementById('benchMachine').options.length > 0",
            timeout=15000,
        )
        await page.select_option("#benchMachine", str(machine_id))
        await page.select_option("#benchAlgo", "PRL")
        await page.click("#benchPreview")
        await page.wait_for_selector("#benchValidation:not(.hidden)", timeout=15000)

        duration = await page.inner_text("#benchDuration")
        lost = await page.inner_text("#benchLost")
        power = await page.inner_text("#benchPower")
        assert duration and duration != "—", duration
        assert lost, lost
        assert power, power
        levels = await page.locator("#benchLevels tbody tr").count()
        assert levels == 5, levels
        await shot(page, "6.6-validation")

        await page.click("#benchLaunch")
        await page.wait_for_function(
            "() => document.querySelectorAll('#benchResults tbody tr').length === 5",
            timeout=30000,
        )
        status = await page.inner_text("#benchRunStatus")
        assert status == "done", status
        badges = await page.locator("#benchResults .badge.perf").count()
        assert badges == 1, badges
        assert "vérifié" in (await page.inner_text("#benchRestore"))
        await shot(page, "6.6-resultats")

        await page.select_option("#benchApplyMode", "efficiency")
        await page.click("#benchApply")
        await page.wait_for_function(
            "() => (document.getElementById('benchApplied').textContent || '').includes('108')",
            timeout=15000,
        )
        applied = await page.inner_text("#benchApplied")
        assert "108 W" in applied, applied
        assert "Rendement" in applied, applied
        await shot(page, "6.6-mode-applique")

        assert not page_errors, page_errors
        assert not console_errors, console_errors
        await browser.close()
