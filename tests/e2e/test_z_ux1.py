"""E2E UX-1 (§4.19) : tableau de bord « état de santé », page Suivi,
GPU intégré absent, rôles des devices, sous-onglets Log et Mise à jour.

Aucune machine n'est contactée : les états sont semés dans la base du serveur
e2e, exactement comme le collecteur les écrirait.
"""

import json
import os
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
DB_PATH = f"{E2E_DATA_DIR}/fossor.db"


def _insert_state(machine_uuid: str, **values) -> None:
    """Insère une ligne ``machine_status`` en ne gardant que les colonnes réelles."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(machine_status)")}
        values = {key: value for key, value in values.items() if key in columns}
        values["machine_uuid"] = machine_uuid
        values.setdefault("ts", datetime.now(timezone.utc).isoformat())
        names = ", ".join(f'"{name}"' for name in values)
        marks = ", ".join("?" for _ in values)
        conn.execute(f"INSERT OR REPLACE INTO machine_status ({names}) VALUES ({marks})",
                     list(values.values()))
        conn.commit()
    finally:
        conn.close()


async def _login(page):
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dashboard_health_pills_followup_and_integrated_gpu(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()
        await _login(page)
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded", timeout=15000)

        created = await page.evaluate(
            """async () => {
                const names = ['e2e-vert', 'e2e-orange', 'e2e-rouge'];
                const out = [];
                for (const name of names) {
                    const r = await fetch('/api/machines', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: name + '-' + Date.now(), host: '192.0.2.77', ssh_user: 'root'})
                    });
                    out.push(await r.json());
                }
                return out;
            }"""
        )
        green, orange, red = created
        _insert_state(green["uuid"], online=1, miner="x", total_hashrate_hs=1000.0)
        _insert_state(orange["uuid"], online=1, miner="x", api_error="API injoignable")
        _insert_state(red["uuid"], online=0, miner="", error="timeout SSH")

        conn = sqlite3.connect(DB_PATH, timeout=15)
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO devices (uuid, machine_id, kind, \"index\", vendor, model, present, "
                "enabled_for_mining, integrated, occupied_external, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), green["id"], "gpu", 0, "Intel", "Intel UHD Graphics 630",
                 1, 0, 1, 0, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        await page.goto(E2E_URL + "/dashboard", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_function(
            "() => document.querySelectorAll('.pill[data-status]').length >= 3", timeout=15000
        )
        states = await page.eval_on_selector_all(
            ".pill", "els => els.map(e => [e.dataset.status, e.dataset.machine, e.textContent])"
        )
        statuses = {state[0] for state in states}
        assert {"green", "orange", "red"} <= statuses, states
        by_status = {state[0]: state[2] for state in states}
        assert "API injoignable" in by_status["orange"], by_status
        assert "hors ligne" in by_status["red"], by_status
        assert "Intel UHD Graphics 630" not in await page.content()
        shots = Path(os.environ.get("FOSSOR_SHOTS_DIR") or "/runs")
        if shots.is_dir():
            await page.screenshot(path=str(shots / "ux1-dashboard.png"), full_page=True)

        # Un clic sur la pastille verte ouvre le Suivi de cette machine.
        await page.locator('.pill[data-status="green"]').first.click()
        await page.wait_for_url("**/followup**", timeout=15000)
        await page.wait_for_load_state("networkidle")
        content = await page.content()
        assert green["name"] in content, content[:500]
        assert "Intel UHD Graphics 630" not in content
        assert await page.locator("#machineSelect").count() == 1
        if shots.is_dir():
            await page.screenshot(path=str(shots / "ux1-suivi.png"), full_page=True)

        # Rôle « token par défaut uniquement » : le device disparaît des options
        # de campagne (l'API le refuse aussi, en 422 — testé en unitaire).
        role = await page.evaluate(
            """async (machineId) => {
                const devices = await (await fetch('/api/machines/' + machineId + '/devices')).json();
                const gpu = devices.find(d => d.model === 'Intel UHD Graphics 630');
                const r = await fetch('/api/devices/' + gpu.id, {
                    method: 'PATCH', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'default_only'})
                });
                return [r.status, gpu.uuid, (await r.json()).role];
            }""",
            green["id"],
        )
        assert role[0] == 200 and role[2] == "default_only", role
        options = await page.evaluate("""async () => {
            const o = await (await fetch('/api/campaigns/options')).json();
            return (o.machines || []).flatMap(m => (m.devices || []).map(d => d.uuid));
        }""")
        assert role[1] not in options, options

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_log_and_update_subtabs(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()
        await _login(page)

        await page.goto(E2E_URL + "/audit", wait_until="networkidle", timeout=15000)
        subtabs = await page.eval_on_selector_all(".subtab", "els => els.map(e => e.textContent.trim())")
        assert subtabs == ["Audit", "Historique des mises à jour"], subtabs
        await page.locator("#tabHistory").click()
        assert await page.locator("#panel-history").is_visible()

        await page.goto(E2E_URL + "/settings", wait_until="networkidle", timeout=15000)
        assert await page.locator("#tabUpdate").count() == 1
        await page.locator("#tabUpdate").click()
        assert await page.locator("#panel-update").is_visible()
        text = await page.locator("#panel-update").inner_text()
        assert "Mise à jour" in text and len(text) > 40, text

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()
