"""E2E UX-1 (§4.19) : tableau de bord « état de santé », page Suivi,
GPU intégré absent, rôles des devices, sous-onglets Log et Mise à jour.

Aucune machine n'est contactée : les états sont semés dans la base du serveur
e2e par la fabrique commune (``tests/e2e/factory.py``), exactement comme le
collecteur les écrirait.
"""

import json
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL
from tests.e2e.factory import create_machine, insert_device, insert_status

ADMIN_PASSWORD = "adminpass"
SHOTS = Path(os.environ.get("FOSSOR_SHOTS_DIR") or "/runs")


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
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        context.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()
        await _login(page)
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded", timeout=15000)

        green = await create_machine(page, "e2e-vert")
        orange = await create_machine(page, "e2e-orange")
        red = await create_machine(page, "e2e-rouge")
        insert_status(green["uuid"], online=1, miner="x", algo="pearlhash", coin="PRL",
                      total_hashrate_hs=1000.0)
        insert_status(orange["uuid"], online=1, miner="x", algo="pearlhash", coin="PRL",
                      api_error="API injoignable")
        insert_status(red["uuid"], online=0, miner="", algo="", coin="", error="timeout SSH")
        insert_device(green["id"], kind="gpu", vendor="Intel",
                      model="Intel UHD Graphics 630", integrated=True, enabled=False)

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
        if SHOTS.is_dir():
            await page.screenshot(path=str(SHOTS / "ux1-dashboard.png"))

        # Un clic sur la pastille verte ouvre le Suivi de cette machine.
        await page.locator('.pill[data-status="green"]').first.click()
        await page.wait_for_url("**/followup**", timeout=15000)
        await page.wait_for_load_state("networkidle")
        content = await page.content()
        assert green["name"] in content, content[:500]
        assert "Intel UHD Graphics 630" not in content
        assert await page.locator("#machineSelect").count() == 1
        assert await page.locator("#tokenCards .token").count() >= 1
        if SHOTS.is_dir():
            await page.screenshot(path=str(SHOTS / "ux1-suivi.png"))

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
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
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
        await page.wait_for_selector("#panel-update", state="visible", timeout=10000)
        text = await page.locator("#panel-update").inner_text()
        assert "Mise à jour" in text and len(text) > 40, text
        if SHOTS.is_dir():
            await page.screenshot(path=str(SHOTS / "ux1-update.png"))

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()
