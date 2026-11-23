"""e2e — une seule barre de navigation, identique sur toutes les pages (§2f).

Chaque page connectée doit afficher la même liste de liens, dans le même ordre,
avec son propre onglet actif et zéro erreur console.
"""

import pytest
from playwright.async_api import async_playwright

ADMIN_PASSWORD = "adminpass"
PAGES = ["/dashboard", "/machines", "/profiles", "/campaigns", "/benchmark", "/analysis", "/mined", "/followup", "/history", "/vault", "/help", "/settings", "/audit"]
SECTIONS = {
    "Pilotage": [("Tableau de bord", "/dashboard"), ("Machines", "/machines"),
                 ("Profils", "/profiles"), ("Campagnes", "/campaigns"),
                 ("Benchmark", "/benchmark")],
    "Suivi": [("Suivi", "/followup"), ("Historique", "/history"),
              ("Tokens minés", "/mined"), ("Log", "/audit")],
    "Analyse": [("Analyse des tokens", "/analysis"), ("Comment ça marche", "/help")],
    "Réglages": [("Configuration", "/settings"), ("Coffre", "/vault")],
}
EXPECTED = [href for items in SECTIONS.values() for _label, href in items]


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
            hrefs = [link["href"] for link in links if link["href"] in EXPECTED]
            assert sorted(hrefs) == sorted(EXPECTED), f"{path}: {hrefs}"
            assert [link["href"] for link in links if link["active"]] == [path], path
            # DOM identique à part la classe active
            dom = await page.eval_on_selector(
                "#nav",
                "nav => nav.innerHTML.replace(/ ?active/g, '').replace(/ aria-current=\"page\"/g, '')",
            )
            seen[path] = dom

        # aucun menu codé en dur : tout lien de navigation vit dans #nav
        outside = await page.evaluate(
            "() => [...document.querySelectorAll('a')].filter(a => "
            "['/dashboard','/machines','/profiles','/campaigns','/benchmark','/followup',"
            "'/history','/mined','/audit','/analysis','/help','/settings','/vault']"
            ".includes(a.getAttribute('href')) && !a.closest('#nav')).length"
        )
        await browser.close()

    assert len(set(seen.values())) == 1, "menu différent d'une page à l'autre"
    assert outside == 0, f"{outside} lien(s) de menu hors #nav"
    assert console_errors == [], console_errors
    assert page_errors == [], page_errors
