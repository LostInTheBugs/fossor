"""E2E — sous-onglets de /analysis, fiche du token justifiée, mobile 390 px.

Le cas SHEAR est semé directement en base : 19/11 « mainnet 21 déc, pas encore
lancé », 20/11 « mainnet actif 16/11 », des « pour » d'exploitation, aucun
« contre » et aucune raison. La fiche doit afficher la raison (ou son absence),
les avertissements de cohérence et l'historique complet.

Les captures vont dans ``FOSSOR_E2E_SHOTS`` s'il est défini, sinon dans le
``tmp_path`` du test : jamais dans l'arbre des sources.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"

ANALYSES = [
    {
        "ts": "2026-11-19T17:43:00+00:00",
        "run": "veille 19/11",
        "action": "rien",
        "detail": "mainnet 21 déc, pas encore lancé",
        "source": "hermes",
        "candidates": [
            {
                "ticker": "SHEAR",
                "name": "SHEAR",
                "algo": "cuda",
                "verdict": "surveillance",
                "reason": "Premier signal : ANN active, mainnet annoncé pour le 21 déc.",
                "criteria": [{"key": "fenetre", "verdict": "warn", "note": "mainnet à venir"}],
                "pour": ["ANN active"],
                "contre": [],
            }
        ],
    },
    {
        "ts": "2026-11-20T17:43:00+00:00",
        "run": "veille 20/11",
        "action": "rien",
        "detail": "mainnet actif 16/11",
        "source": "hermes",
        "candidates": [
            {
                "ticker": "SHEAR",
                "name": "SHEAR",
                "algo": "cuda",
                "verdict": "surveillance",
                "criteria": [{"key": "fenetre", "verdict": "ok", "note": "mainnet actif"}],
                "pour": [
                    "mainnet actif 16/11",
                    "cron jobs eca6896264bc/aa8e2aa870c6 vérifiés armés",
                    "réseau stable",
                ],
                "contre": [],
            }
        ],
    },
]


def _evidence_dir(tmp_path: Path) -> Path | None:
    raw = os.environ.get("FOSSOR_E2E_SHOTS", "").strip()
    base = Path(raw) if raw else tmp_path
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".ecriture"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        return None
    return base


async def _shot(page, directory: Path | None, name: str) -> None:
    if directory is None:
        return
    await page.screenshot(path=str(directory / name), full_page=True)


def _seed() -> None:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
    try:
        conn.execute("DELETE FROM analyses WHERE fingerprint LIKE 'e2e-ux3-%'")
        for index, analysis in enumerate(ANALYSES):
            conn.execute(
                "INSERT INTO analyses (ts, run, action, detail, candidates, delivered, source, fingerprint, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    analysis["ts"],
                    analysis["run"],
                    analysis["action"],
                    analysis["detail"],
                    json.dumps(analysis["candidates"], ensure_ascii=False),
                    0,
                    analysis["source"],
                    f"e2e-ux3-{index}",
                    analysis["ts"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _cleanup() -> None:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
    try:
        conn.execute("DELETE FROM analyses WHERE fingerprint LIKE 'e2e-ux3-%'")
        conn.commit()
    finally:
        conn.close()


async def _login(page) -> None:
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_analysis_subtabs_and_token_sheet(e2e_server, tmp_path):
    shots = _evidence_dir(tmp_path)
    errors: list[str] = []
    _seed()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            context.on("pageerror", lambda err: errors.append(str(err)))
            page = await context.new_page()
            await _login(page)

            # 1. Tokens est l'onglet par défaut et l'URL suit.
            await page.goto(E2E_URL + "/analysis", wait_until="domcontentloaded")
            await page.wait_for_selector("#tokRows tr", timeout=15000)
            assert await page.is_visible("#tab-tokens")
            assert await page.evaluate("location.hash") == "#tokens"
            await _shot(page, shots, "tokens.png")

            # 2. Un clic sur SHEAR ouvre la fiche : raison, avertissements, historique.
            row = page.locator("#tokRows tr[data-tokrow]").filter(has_text="SHEAR").first
            await row.click()
            await page.wait_for_selector("#tokDetail", state="visible", timeout=10000)
            sheet = await page.inner_text("#tokDetail")
            assert "mainnet actif 16/11" in sheet
            assert "sans aucun argument « contre » ni raison" in sheet
            assert "contredit l'évaluation précédente" in sheet
            assert "Historique complet (2)" in sheet
            assert "Premier signal : ANN active" in sheet
            assert await page.evaluate("location.hash") == "#tokens/shear"
            await _shot(page, shots, "token-shear.png")
            await page.click("#detailClose")
            await page.wait_for_selector("#tokDetail", state="hidden", timeout=5000)

            # 3. Chaque sous-onglet a son URL et son panneau.
            for tab in ("proposals", "scans", "criteria", "import"):
                await page.click(f'#subTabs [data-tab="{tab}"]')
                await page.wait_for_selector(f"#tab-{tab}", state="visible", timeout=5000)
                assert await page.evaluate("location.hash") == "#" + tab
                assert not await page.is_visible("#tab-tokens")
                await _shot(page, shots, f"{tab}.png")
            criteria_text = await page.inner_text("#criteriaSummary")
            assert "Fenêtre de sortie" in criteria_text

            # 4. L'onglet est mémorisé : /analysis sans hash rouvre le dernier onglet.
            await page.goto(E2E_URL + "/analysis", wait_until="domcontentloaded")
            await page.wait_for_selector("#tab-import", state="visible", timeout=10000)
            assert await page.evaluate("location.hash") == "#import"

            # 5. Mobile 390 px : colonnes secondaires repliées, fiche pleine largeur.
            mobile = await browser.new_context(viewport={"width": 390, "height": 844})
            mobile.on("pageerror", lambda err: errors.append(str(err)))
            small = await mobile.new_page()
            await _login(small)
            await small.goto(E2E_URL + "/analysis#tokens", wait_until="domcontentloaded")
            await small.wait_for_selector("#tokRows tr", timeout=15000)
            display = await small.evaluate(
                """() => {
                    const el = document.querySelector('.toktab td.secondary');
                    return el ? getComputedStyle(el).display : 'absent';
                }"""
            )
            assert display == "none"
            await small.locator("#tokRows tr[data-tokrow]").filter(has_text="SHEAR").first.click()
            await small.wait_for_selector("#tokDetail", state="visible", timeout=10000)
            box = await small.locator("#tokDetail").bounding_box()
            assert box is not None and box["width"] <= 391
            await _shot(small, shots, "mobile-390.png")
            await mobile.close()
            await browser.close()
    finally:
        _cleanup()

    assert not errors, errors
