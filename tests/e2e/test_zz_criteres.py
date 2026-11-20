"""E2E — tokens regroupés avec grille de critères, règles éditables, bandeau réel.

Les captures vont dans ``FOSSOR_E2E_SHOTS`` s'il est défini, sinon dans le
``tmp_path`` du test : jamais à la racine, jamais dans l'arbre des sources. Un
dossier non inscriptible (clone monté en lecture seule) n'échoue pas le test.
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


def _evidence_dir(tmp_path: Path) -> Path | None:
    """Dossier des captures, ou None s'il n'est pas inscriptible."""
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

ANALYSES = [
    {
        "ts": "2026-11-18T09:00:00+00:00",
        "run": "cron 09h",
        "action": "rien",
        "detail": "BigCoin sorti, NEX Chain suivi.",
        "source": "hermes",
        "candidates": [
            {
                "ticker": "BIG",
                "name": "BigCoin",
                "algo": "Ethash",
                "verdict": "retenu",
                "criteria": [
                    {"key": "fenetre", "verdict": "ok", "note": "sorti il y a 8 h"},
                    {"key": "gpu", "verdict": "ok", "note": "RTX 4080 16 Go"},
                ],
                "pour": ["hashrate réseau au plus bas"],
                "contre": ["pas de prix garanti"],
            },
            {
                "ticker": "NEXC",
                "name": "NEX Chain",
                "algo": "NexaPow",
                "verdict": "surveillance",
                "criteria": [],
                "pour": [],
                "contre": ["pool unique au lancement"],
            },
        ],
    },
    {
        "ts": "2026-11-19T09:00:00+00:00",
        "run": "cron 09h",
        "action": "rien",
        "detail": "BigCoin confirmé.",
        "source": "hermes",
        "candidates": [
            {
                "ticker": "BIGCOIN",
                "name": "BIGCOIN",
                "algo": "Ethash",
                "verdict": "surveillance",
                "criteria": [
                    {"key": "fenetre", "verdict": "warn", "note": "3 jours"},
                    {"key": "utilite", "verdict": "na", "note": ""},
                ],
                "pour": [],
                "contre": [],
            },
            {
                "ticker": "",
                "name": "NEX Chain (NEXC)",
                "algo": "NexaPow",
                "verdict": "rejeté",
                "criteria": [],
                "pour": [],
                "contre": [],
            },
        ],
    },
]


def _seed() -> None:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
    try:
        conn.execute("DELETE FROM analyses WHERE fingerprint LIKE 'e2e-criteres-%'")
        for index, analysis in enumerate(ANALYSES):
            conn.execute(
                "INSERT INTO analyses (ts, run, action, detail, candidates, delivered, source, fingerprint, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    analysis["ts"], analysis["run"], analysis["action"], analysis["detail"],
                    json.dumps(analysis["candidates"], ensure_ascii=False), 0, analysis["source"],
                    f"e2e-criteres-{index}", analysis["ts"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _cleanup() -> None:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
    try:
        conn.execute("DELETE FROM analyses WHERE fingerprint LIKE 'e2e-criteres-%'")
        conn.execute("DELETE FROM machines WHERE name IN ('e2e-athlon', 'e2e-simulee')")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_criteres_tokens_grille_bandeau(e2e_server, tmp_path):
    shots = _evidence_dir(tmp_path)
    errors: list[str] = []
    _seed()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            context.on("pageerror", lambda err: errors.append(str(err)))
            page = await context.new_page()
            await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
            await page.fill("#username", "admin")
            await page.fill("#password", ADMIN_PASSWORD)
            await page.locator("button[type=submit]").click()
            await page.wait_for_url("**/dashboard", timeout=15000)

            machine_id = await page.evaluate(
                """async () => {
                    const res = await fetch('/api/machines', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: 'e2e-athlon', host: '127.0.0.1', ssh_port: 1,
                            ssh_user: 'tester', auth_type: 'fossor_key', usage: 'cpu', enabled: true})
                    });
                    if (!res.ok) throw new Error('machine ' + res.status + ' ' + await res.text());
                    const second = await fetch('/api/machines', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: 'e2e-simulee', host: '127.0.0.1', ssh_port: 1,
                            ssh_user: 'tester', auth_type: 'fossor_key', usage: 'gpu', enabled: true})
                    });
                    if (!second.ok) throw new Error('machine 2 ' + second.status);
                    return (await res.json()).id;
                }"""
            )
            conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
            try:
                conn.execute("UPDATE machines SET simulation_disabled=1 WHERE id=?", (machine_id,))
                conn.commit()
            finally:
                conn.close()

            await page.goto(E2E_URL + "/analysis", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_selector("#tokRows tr", timeout=15000)
            banner = await page.locator("#simBanner").inner_text()
            assert "Pilotage réel : e2e-athlon" in banner, banner
            assert "les autres machines sont simulées" in banner

            # D'autres tests ont semé des analyses : on vérifie le regroupement,
            # pas un total global. BIGCOIN (2 libellés) et NEX (2 libellés)
            # doivent chacun tenir sur une seule ligne.
            counts = await page.evaluate(
                """() => Array.from(document.querySelectorAll('#tokRows .tokname')).map(el => el.innerText)"""
            )
            assert sum(1 for name in counts if name.strip().upper().startswith("BIGCOIN")) == 1, counts
            assert sum(1 for name in counts if name.strip().upper().startswith("NEX")) == 1, counts
            big_row = page.locator("#tokRows tr").filter(has_text="BIGCOIN").first
            await big_row.locator("[data-hist]").click()
            hist = await big_row.locator("xpath=following-sibling::tr[1]").inner_text()
            assert "Fenêtre de sortie" in hist, hist
            assert "sorti il y a 8 h" in hist
            nex_row = page.locator("#tokRows tr").filter(has_text="NEX Chain").first
            await nex_row.locator("[data-hist]").click()
            assert "non évalué" in await page.locator("#tokRows").inner_text()
            # Dates sur une ligne, non coupées.
            wide = await page.evaluate(
                """() => Array.from(document.querySelectorAll('td.date')).map(
                    el => ({text: el.innerText, clipped: el.scrollWidth > el.clientWidth + 1}))"""
            )
            assert wide and all(not item["clipped"] for item in wide), wide
            await _shot(page, shots, "01-tokens-1280.png")

            # Page des règles : les critères sont éditables dans l'ordre.
            await page.click('#subTabs [data-tab="criteria"]')
            row = page.locator('[data-rule="criteria"]')
            await row.scroll_into_view_if_needed()
            await row.locator("[data-act=edit]").click()
            await page.wait_for_selector("#critRows .critrow", timeout=10000)
            rows = await page.locator("#critRows .critrow").count()
            assert rows == 5, rows
            first = await page.locator('#critRows .critrow').first.locator('textarea[data-cfield="text"]').input_value()
            assert "Coin sorti depuis quelques heures" in first
            label_inputs = page.locator('#critRows .critrow input[data-cfield="label"]')
            labels = [await label_inputs.nth(i).input_value() for i in range(rows)]
            assert labels[0] == "Fenêtre de sortie" and labels[4] == "Compatible avec les GPU de Fred"
            await _shot(page, shots, "02-regles-criteres.png")
            await page.locator('[data-rule="criteria"]').scroll_into_view_if_needed()
            await page.locator('[data-act="save"][data-id="criteria"]').click()
            await page.wait_for_selector("#rulesMsg .ok", timeout=10000)

            # Écran étroit : les cellules passent sous le nom, rien n'est coupé.
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto(E2E_URL + "/analysis", wait_until="domcontentloaded", timeout=15000)
            await page.click('#subTabs [data-tab="tokens"]')
            await page.wait_for_selector("#tokRows tr", timeout=15000)
            big_narrow = page.locator("#tokRows tr").filter(has_text="BIGCOIN").first
            await big_narrow.locator("[data-hist]").click()
            narrow = await page.evaluate(
                """() => Array.from(document.querySelectorAll('td.date')).map(
                    el => ({text: el.innerText, clipped: el.scrollWidth > el.clientWidth + 1}))"""
            )
            assert narrow and all(not item["clipped"] for item in narrow), narrow
            assert "Fenêtre de sortie" in await big_narrow.locator("xpath=following-sibling::tr[1]").inner_text()
            await _shot(page, shots, "03-tokens-390.png")
            await browser.close()
        assert not errors, errors
    finally:
        _cleanup()
