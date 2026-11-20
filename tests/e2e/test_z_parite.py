"""e2e — parité avec ``mining.html`` : tokens évalués, tableau de bord, aide.

Les données viennent des fixtures anonymisées ; aucune machine réelle n'est
contactée. Les captures sont écrites seulement si ``CAPTURE_DIR`` est posé.
"""

import json
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "")
ADMIN_PASSWORD = "adminpass"


async def _login(page, server: str) -> None:
    await page.goto(server + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


async def _shot(page, name: str):
    if CAPTURE_DIR:
        Path(CAPTURE_DIR).mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(Path(CAPTURE_DIR) / name), full_page=True)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_tokens_evalues_filtres(e2e_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        errors: list[str] = []
        context.on("pageerror", lambda err: errors.append(str(err)))
        await _login(page, e2e_server)

        await page.goto(e2e_server + "/analysis", wait_until="networkidle", timeout=20000)
        await page.click('#subTabs [data-tab="import"]')
        await page.set_input_files("#importFile", str(FIXTURES / "parite-analyses.json"))
        await page.click("#importBtn")
        await page.click('#subTabs [data-tab="tokens"]')
        await page.wait_for_selector("#tokRows [data-hist]", timeout=20000)

        rows = page.locator("#tokRows tr:not(.histbox)")
        assert await rows.count() == 2, await page.inner_text("#tokRows")

        await page.click('[data-tokfilter="retenu"]')
        assert await page.locator("#tokRows tr:not(.histbox)").count() == 1
        filtered = await page.inner_text("#tokRows")
        assert "NOVA" in filtered and "BRK" not in filtered
        await _shot(page, "parite-tokens-filtres.png")

        await page.click('[data-tokfilter="retenu"]')
        await page.click('[data-tokfilter=""]')
        await page.fill("#tokSearch", "bri")
        assert await page.locator("#tokRows tr:not(.histbox)").count() == 1
        assert "BRK" in await page.inner_text("#tokRows")

        await page.fill("#tokSearch", "")
        await page.locator("#tokRows [data-hist]").first.click()
        assert await page.locator('[data-histrow]:visible').count() == 1

        assert errors == [], errors
        await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dashboard_badge_dernier_evenement(e2e_server):
    status = json.loads((FIXTURES / "parite-status.json").read_text(encoding="utf-8"))
    # La base e2e est partagée : les campagnes des autres tests ont des
    # événements plus récents. On date l'import pour que « dernier événement »
    # désigne bien celui du scénario, sans toucher au code.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    status["updated"] = now.isoformat()
    for index, event in enumerate(status.get("events") or []):
        event["ts"] = (now - timedelta(minutes=index)).isoformat()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        errors: list[str] = []
        context.on("pageerror", lambda err: errors.append(str(err)))
        await _login(page, e2e_server)

        response = await page.request.post(
            e2e_server + "/api/import/legacy",
            data={"status": status, "live": {}, "dry_run": False},
        )
        assert response.ok, await response.text()

        await page.goto(e2e_server + "/dashboard", wait_until="networkidle", timeout=20000)
        await page.wait_for_selector("#pariteCampaigns .parite-card", timeout=15000)
        assert (await page.inner_text("#modeBadge")).strip() == "spec BIG"
        event = await page.inner_text("#lastEventCard")
        assert "BigCoin" in event and "la sauce prend" in event
        campaigns = await page.inner_text("#pariteCampaigns")
        assert "WAM" in campaigns and "sans limite" in campaigns
        assert "Prolongations : 1" in campaigns
        await _shot(page, "parite-tableau-de-bord.png")
        assert errors == [], errors
        await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_page_comment_ca_marche(e2e_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        errors: list[str] = []
        context.on("pageerror", lambda err: errors.append(str(err)))
        await _login(page, e2e_server)

        response = await page.goto(e2e_server + "/help", wait_until="networkidle", timeout=15000)
        assert response.status == 200
        body = await page.inner_text("body")
        assert "Comment ça marche" in body
        assert "La veille propose" in body
        assert "Lancement automatique" in body
        assert "getbalance" in body
        await _shot(page, "parite-aide.png")
        assert errors == [], errors
        await browser.close()
