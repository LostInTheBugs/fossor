"""E2E — fiche « Alimentations » (§4.2 bis) : deux PSU, GPU réparti, alerte orange."""

import json

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL
from tests.e2e.factory import create_machine, insert_device

ADMIN_PASSWORD = "adminpass"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_psu_section_two_psus_split_gpu_and_orange_alert(e2e_server):
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
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

        machine = await create_machine(page, "e2e-psu", host="127.0.0.1")
        machine_id = machine["id"]
        # No SSH: the GPU is seeded in the e2e database, like the collector would.
        device_id = insert_device(machine_id, kind="gpu", vendor="NVIDIA",
                                  model="NVIDIA GeForce RTX 3070")

        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector(f"#psuName-{machine_id}", timeout=15000)

        # 1. Deux alimentations : 650 W Gold (défaut) et 170 W Gold.
        for name, watts in (("PSU 1", "650"), ("PSU 2", "170")):
            await page.fill(f"#psuName-{machine_id}", name)
            await page.fill(f"#psuW-{machine_id}", watts)
            await page.select_option(f"#psuCert-{machine_id}", "gold")
            # `.first` visait le formulaire d'une autre machine quand la suite
            # a déjà créé des machines : rester dans le panneau de CELLE-CI.
            scope = page.locator(f"#psuName-{machine_id}").locator(
                "xpath=ancestor::div[contains(@class,'dev-panel')][1]"
            )
            if await scope.count() == 0:
                scope = page.locator(f"#psuName-{machine_id}").locator(
                    "xpath=ancestor::div[contains(@class,'card')][1]"
                )
            await scope.locator("button:has-text('+ Alimentation')").first.click()
            await page.wait_for_function(
                "n => document.querySelectorAll('[data-psu-row]').length === n",
                arg=1 if name == "PSU 1" else 2,
                timeout=15000,
            )

        psus = await page.evaluate(
            """async (id) => (await (await fetch('/api/machines/' + id + '/psus')).json()).map(p => p.id)""",
            machine_id,
        )
        assert len(psus) == 2

        # 2. GPU réparti : 75 W slot/riser sur PSU 1, le reste sur PSU 2.
        #
        # 004-a/c1 : chaque ajout d'alimentation relance ``loadMachines()``, qui
        # reconstruit la liste. Un select rempli pendant ce rendu est recréé
        # sans valeur, et « Affecter » envoyait alors ``psu_id: null`` (le toast
        # disait « enregistrée » et le GPU restait sans PSU). On repart donc d'un
        # document stable, puis on vérifie que les deux selects portent bien
        # leur valeur avant de cliquer.
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector(f'[data-assign-primary="{device_id}"]', timeout=15000)
        await page.select_option(f'[data-assign-primary="{device_id}"]', str(psus[0]))
        await page.select_option(f'[data-assign-second="{device_id}"]', str(psus[1]))
        await page.fill(f'[data-assign-split="{device_id}"]', "75")
        await page.wait_for_function(
            """(ids) => {
                const first = document.querySelector('[data-assign-primary="' + ids[0] + '"]');
                const second = document.querySelector('[data-assign-second="' + ids[0] + '"]');
                return first && second && first.value === ids[1] && second.value === ids[2];
            }""",
            arg=[device_id, str(psus[0]), str(psus[1])],
            timeout=15000,
        )
        await page.locator(f'[data-gpu="{device_id}"] button:has-text("Affecter")').click()
        # Attente explicite du retour d'écran, puis de l'état réel côté API.
        await page.wait_for_function(
            "() => /Affectation/.test((document.querySelector('#toast') || {}).textContent || '')",
            timeout=15000,
        )
        power = await page.evaluate(
            "async (id) => (await (await fetch('/api/machines/' + id + '/power')).json())",
            machine_id,
        )
        print("PSU_TOAST:", await page.inner_text("#toast"))
        print("PSU_POWER:", json.dumps(power, ensure_ascii=False))
        assert power["gpus_without_psu"] == [], power["gpus_without_psu"]
        assert power["psus"][1]["load_dc_w"] == 145.0, power["psus"]
        await page.wait_for_selector('[data-psu-status="orange"]', timeout=15000)

        # 3. Les barres existent, l'alerte orange est visible.
        # La page liste TOUTES les machines : cibler nos deux PSU, sinon une
        # alerte « GPU sans alimentation » d'une machine créée par un test
        # précédent est lue à la place (échec en suite complète seulement).
        await page.wait_for_selector(f'[data-psu-row="{psus[1]}"][data-psu-status="orange"]',
                                     timeout=15000)
        assert await page.locator(
            f'[data-psu-row="{psus[0]}"], [data-psu-row="{psus[1]}"]'
        ).count() == 2
        assert await page.locator(f'[data-psu-row="{psus[1]}"]').count() == 1
        alerts = await page.evaluate(
            """() => Array.from(document.querySelectorAll('[data-psu-alert="orange"]'))
                 .map(e => e.textContent)"""
        )
        alert = [text for text in alerts if "PSU 2" in text]
        assert alert, alerts
        assert "85.3 %" in alert[0] or "85,3" in alert[0], alert

        assert page_errors == []
        assert console_errors == []
        await browser.close()
