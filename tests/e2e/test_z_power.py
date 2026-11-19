"""E2E §4.15 / §4.17 : écran Électricité (tarifs, règle) et contrôleur d'alimentation.

Un faux Shelly Gen1 local sert de prise : aucune machine de Fred n'est contactée.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"


class FakeShelly:
    def __init__(self):
        self.requests: list[str] = []
        self.on = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                owner.requests.append(self.path)
                if self.path.startswith("/relay/0?turn=on"):
                    owner.on = True
                elif self.path.startswith("/relay/0?turn=off"):
                    owner.on = False
                if self.path.startswith("/status"):
                    body = {"relays": [{"ison": owner.on}], "meters": [{"power": 123.4}]}
                else:
                    body = {"ison": owner.on}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_electricity_tariffs_rule_and_power_controller(e2e_server):
    fake = FakeShelly()
    api = httpx.Client(base_url=E2E_URL, timeout=20)
    login = api.post("/api/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    assert login.status_code == 200, login.text
    created = api.post("/api/machines", json={"name": "rig-e2e-prise", "host": "192.0.2.90"})
    assert created.status_code in (200, 201), created.text
    machine_id = created.json()["id"]
    saved = api.put(f"/api/machines/{machine_id}/power-controller", json={
        "kind": "shelly_gen1", "url": fake.url, "max_per_day": 3, "min_delay_s": 5,
    })
    assert saved.status_code == 200, saved.text

    console_errors: list[str] = []
    page_errors: list[str] = []

    try:
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

            # ── Électricité : plages HC/HP avec jours ouvrés et week-end ──
            await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
            await page.locator("#tabEnergy").click()
            await page.wait_for_selector("#panel-energy", state="visible", timeout=10000)
            await page.wait_for_selector("#enPriceNow", state="visible", timeout=10000)
            # Le texte est posé par loadEnergy() : attendre le CONTENU attendu,
            # pas un délai fixe (lecture de « … » avant remplissage).
            await page.wait_for_function(
                "document.querySelector('#enPriceNow').textContent.includes('€/kWh')",
                timeout=15000,
            )
            assert "€/kWh" in await page.inner_text("#enPriceNow")

            await page.select_option("#enMode", "ranges")
            await page.wait_for_selector("#enRanges", state="visible")
            first = page.locator("#enRangeBody tr").nth(0)
            await first.locator(".enStart").fill("00:00")
            await first.locator(".enEnd").fill("24:00")
            await first.locator(".enPrice").fill("0.25")
            await first.locator(".enDays").select_option("weekday")
            await page.locator("#enAddRange").click()
            second = page.locator("#enRangeBody tr").nth(1)
            await second.locator(".enStart").fill("00:00")
            await second.locator(".enEnd").fill("24:00")
            await second.locator(".enPrice").fill("0.15")
            await second.locator(".enDays").select_option("weekend")
            await page.locator("#enSave").click()
            await page.wait_for_selector("#toast", state="visible", timeout=10000)
            assert "Tarif enregistré" in await page.inner_text("#toast")

            tariff = api.get("/api/energy/tariff").json()
            assert tariff["tariff"]["mode"] == "ranges"
            assert {r["days"] for r in tariff["tariff"]["ranges"]} == {"weekday", "weekend"}
            assert tariff["current"]["price_eur_kwh"] > 0

            # ── Règle « heures chères » ──
            await page.check("#enRuleEnabled")
            await page.fill("#enRuleThreshold", "0.35")
            await page.select_option("#enRuleMode", "stop")
            await page.locator("#enRuleSave").click()
            await page.wait_for_function(
                "() => (document.querySelector('#toast').textContent||'').includes('Règle enregistrée')",
                timeout=10000,
            )
            rule = api.get("/api/energy/rule").json()["rule"]
            assert rule["enabled"] is True and rule["threshold_eur_kwh"] == pytest.approx(0.35)

            # ── Contrôleur d'alimentation : boutons protégés ──
            await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            await page.evaluate(f"openPower({machine_id})")
            await page.wait_for_selector(f"#pcRead-{machine_id}", state="visible", timeout=10000)

            await page.locator(f"#pcRead-{machine_id}").click()
            await page.wait_for_function(
                f"() => (document.querySelector('#pcState-{machine_id}').textContent||'').includes('123.4 W')",
                timeout=10000,
            )
            state_text = await page.inner_text(f"#pcState-{machine_id}")
            assert "mesurée à la prise" in state_text

            await page.locator(f"#pcOn-{machine_id}").click()
            await page.wait_for_function(
                "() => (document.querySelector('#toast').textContent||'').includes('Action envoyée : on')",
                timeout=10000,
            )
            history = api.get(f"/api/machines/{machine_id}/power/history").json()
            assert history[0]["action"] == "on" and history[0]["result"] == "ok"
            assert "/relay/0?turn=on" in fake.requests

            await browser.close()
    finally:
        api.close()
        fake.close()

    assert console_errors == []
    assert page_errors == []
