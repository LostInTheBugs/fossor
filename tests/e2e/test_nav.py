"""e2e — une seule barre de navigation, identique sur toutes les pages (§2f).

Chaque page connectée doit afficher la même liste de liens, dans le même ordre,
avec son propre onglet actif et zéro erreur console.
"""

import pytest
from playwright.async_api import async_playwright

ADMIN_PASSWORD = "adminpass"
PAGES = ["/dashboard", "/machines", "/profiles", "/campaigns", "/analysis", "/mined", "/followup", "/help", "/settings", "/audit"]
EXPECTED = [
    "📊 Tableau de bord",
    "🖥️ Machines",
    "⛏️ Profils",
    "🎯 Campagnes",
    "🔎 Analyse des tokens",
    "🪙 Tokens minés",
    "📈 Suivi",
    "📖 Comment ça marche",
    "⚙️ Configuration",
    "📋 Log",
]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_same_navigation_on_every_page(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    seen: dict[str, list[str]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()

        await page.goto(e2e_server + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        for path in PAGES:
            response = await page.goto(e2e_server + path, wait_until="networkidle", timeout=15000)
            assert response.status == 200, path
            links = await page.eval_on_selector_all(
                "#nav a",
                "els => els.map(e => ({href: new URL(e.href).pathname,"
                " text: e.textContent.trim(), active: e.classList.contains('active')}))",
            )
            texts = [link["text"] for link in links]
            assert texts == EXPECTED, f"{path}: {texts}"
            assert [link["href"] for link in links] == PAGES, f"{path}: {links}"
            assert [link["href"] for link in links if link["active"]] == [path], path
            seen[path] = texts

        await browser.close()

    assert len({tuple(texts) for texts in seen.values()}) == 1, seen
    assert console_errors == [], console_errors
    assert page_errors == [], page_errors
