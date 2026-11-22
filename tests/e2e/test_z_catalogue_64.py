"""E2E 6.4 — la fiche d'un token affiche les mineurs compatibles.

Le token fictif « RCAT » annonce yespower : le catalogue doit afficher
cpuminer-opt, signaler la contradiction « standard » / « aucun profil » et la
configuration doit être en mode ``auto``.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

pytestmark = pytest.mark.e2e

ADMIN_PASSWORD = "adminpass"
FINGERPRINT = "e2e-catalogue-64"

CANDIDATES = [
    {
        "ticker": "RCAT",
        "name": "RCAT",
        "algo": "yespower",
        "verdict": "retenu",
        "reason": "yespower standard, mineur connu ; à confirmer par un pool.",
        "criteria": [{"key": "fenetre", "verdict": "ok", "note": "1 j"}],
        "pour": ["yespower standard (cpuminer-opt)"],
        "contre": ["Aucun profil yespower dans Fossor"],
        "links": ["https://exemple.tld/rcat"],
    }
]


def _seed() -> None:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
    try:
        conn.execute("DELETE FROM analyses WHERE fingerprint=?", (FINGERPRINT,))
        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO analyses (ts, run, action, detail, candidates, delivered, source, fingerprint, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ts,
                "veille-64",
                "rien",
                "",
                json.dumps(CANDIDATES, ensure_ascii=False),
                0,
                "hermes",
                FINGERPRINT,
                ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_analysis():
    """RCAT ne doit pas rester dans la base partagée : les autres e2e comptent
    leurs propres tokens (test_z_parite attend exactement deux lignes)."""
    yield
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
    try:
        conn.execute("DELETE FROM analyses WHERE fingerprint=?", (FINGERPRINT,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fiche_token_affiche_mineurs_et_mode_auto(e2e_server):
    _seed()
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

        settings = await page.evaluate(
            "async () => await (await fetch('/api/spec/settings')).json()"
        )
        assert settings["default_mode"] == "auto"
        assert any(item["value"] == "auto" for item in settings["modes"])

        catalogue = await page.evaluate(
            """async () => {
                const res = await fetch('/api/spec/catalogue');
                const data = await res.json();
                return {
                    count: data.effective.softwares.length,
                    algos: data.algorithm_count,
                    sources: data.effective.softwares.map(s => s.source || ''),
                };
            }"""
        )
        assert catalogue["count"] >= 8
        assert catalogue["algos"] >= 100
        assert all(source.startswith("http") for source in catalogue["sources"])

        await page.goto(E2E_URL + "/analysis#tokens", wait_until="domcontentloaded")
        await page.wait_for_selector("#tokRows tr[data-tokrow]", timeout=15000)
        row = page.locator("#tokRows tr[data-tokrow]").filter(has_text="RCAT").first
        await row.click()
        await page.wait_for_selector("#tokDetail", state="visible", timeout=10000)
        sheet = await page.inner_text("#tokDetail")
        assert "Mineurs compatibles" in sheet
        assert "cpuminer-opt" in sheet.lower()
        assert "profil type" in sheet.lower()
        assert "contradiction" in sheet.lower()

        assert console_errors == []
        assert page_errors == []
        await browser.close()
