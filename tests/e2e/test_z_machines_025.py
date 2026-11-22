"""E2E 025 — la page Machines n'affiche plus « externe » sur un device Fossor.

Données écrites de zéro, adresses RFC 5737 (192.0.2.x), aucun accès machine.
L'athlon fictif porte une ancienne erreur d'API dans ``raw_collections`` et une
liste de mineurs externes périmée : l'écran ne doit afficher ni « injoignable »
ni « externe ».  Le Ryzen fictif a un CPU BigCoin externe, un GPU Fossor et un
ancien hashrate CPU de 216 TH/s : l'écran doit dire « inconnu ».
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

pytestmark = pytest.mark.e2e

ADMIN_PASSWORD = "adminpass"


def _db():
    return sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_device(machine_id: int, uuid: str, kind: str, index: int, model: str,
                   occupied_by: str | None, external: bool = False) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO devices (uuid, machine_id, kind, \"index\", vendor, model, "
            "present, enabled_for_mining, integrated, created_at, updated_at, "
            "occupied_external, occupied_by, role) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid, machine_id, kind, index, "fictif", model, 1, 1, 0,
             _now(), _now(), 1 if external else 0, occupied_by, "default_and_spec"),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_status(machine_uuid: str, api_error: str | None) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO machine_status "
            "(machine_uuid, ts, online, error, miner, algo, coin, api_error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (machine_uuid, _now(), 1, None, "", "", "", api_error),
        )
        conn.commit()
    finally:
        conn.close()


def _set_external_miners(machine_id: int, entries: list[dict]) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE machines SET external_miners=? WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), machine_id),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_metric(machine_uuid: str, device_uuid: str, hashrate: float | None) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO metrics (machine_uuid, device_uuid, ts, hashrate_hs) VALUES (?,?,?,?)",
            (machine_uuid, device_uuid, _now(), hashrate),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_profile() -> int:
    """Profil Fossor fictif, comme le profil Pearl du Ryzen en prod."""
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO miner_profiles (uuid, name, coin, wallet, pool, software, "
            "binary_url, sha256, binary_path, version, cmdline, api_port, api_kind, "
            "binary_source, binary_stored, binary_size, binary_kind, wallet_mode, "
            "execution_mode, threads, worker, cpu_governor, tty_wrapper, enabled, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("00000000-0000-4000-8000-000000000099", "Profil fictif Pearl 025", "PRL",
             "prl1fictif025", "stratum+tcp://pool.invalid:3333", "srbminer",
             "https://192.0.2.90/srbminer.tar.gz", "0" * 64, "srbminer_compute", "3.8.3",
             "{binary} --algorithm pearlhash --pool {pool} --wallet {wallet}", 21373, "srbminer",
             "url", "", 0, "tar.gz", "pool_address", "host", 0, "", 0, 1, 1,
             _now(), _now()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _set_default_token(device_uuid: str, profile_id: int) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE devices SET default_token_id=? WHERE uuid=?",
            (profile_id, device_uuid),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_machines_page_uses_current_state(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []
    stamp = int(datetime.now().timestamp() * 1000)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        context.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        context.on("pageerror", lambda e: page_errors.append(str(e)))
        page = await context.new_page()

        await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        async def create(name: str, host: str) -> tuple[int, str]:
            result = await page.evaluate(
                """async ([name, host]) => {
                    const r = await fetch('/api/machines', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: name, host: host, ssh_user: 'root'})
                    });
                    return [r.status, await r.json()];
                }""",
                [name, host],
            )
            assert result[0] == 200, result
            return int(result[1]["id"]), result[1]["uuid"]

        athlon, athlon_uuid = await create(f"e2e-025-athlon-{stamp}", "192.0.2.77")
        ryzen, ryzen_uuid = await create(f"e2e-025-ryzen-{stamp}", "192.0.2.78")

        # Athlon fictif : deux unités Fossor, aucune erreur courante, mais une
        # ancienne liste d'externes et une ancienne erreur laissées en base.
        _insert_device(athlon, "00000000-0000-4000-8000-000000002501", "cpu", 0,
                       "CPU fictif Athlon", "fossor_default")
        _insert_device(athlon, "00000000-0000-4000-8000-000000002502", "gpu", 0,
                       "GPU fictif Pearl", "fossor_default")
        _insert_status(athlon_uuid, None)
        _set_external_miners(athlon, [{
            "name": "srbminer",
            "cmd": "/opt/fossor/miners/srbminer/3.8.3/srbminer_compute --algorithm pearlhash",
            "kind": "gpu",
            "coin": "PRL",
            "cpu_pct": 6.2,
            "api_port": 21550,
        }])
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO raw_collections (machine_uuid, ts, stdout) VALUES (?,?,?)",
                (athlon_uuid, _now(),
                 "API du mineur injoignable (direct : port fermé ; SSH : injoignable)"),
            )
            conn.commit()
        finally:
            conn.close()

        # Ryzen fictif : GPU Fossor, CPU BigCoin externe, ancien 216 TH/s sur le
        # CPU puis un relevé récent sans hashrate -> « inconnu ».
        _insert_device(ryzen, "00000000-0000-4000-8000-000000002601", "cpu", 0,
                       "CPU fictif Ryzen", "external", external=True)
        _insert_device(ryzen, "00000000-0000-4000-8000-000000002602", "gpu", 0,
                       "GPU fictif Pearl", "fossor_default")
        profile_id = _insert_profile()
        _set_default_token("00000000-0000-4000-8000-000000002602", profile_id)
        _insert_status(ryzen_uuid, None)
        _insert_metric(ryzen_uuid, "00000000-0000-4000-8000-000000002601", 216162333806722.47)
        _insert_metric(ryzen_uuid, "00000000-0000-4000-8000-000000002601", None)
        _insert_metric(ryzen_uuid, "00000000-0000-4000-8000-000000002602", 101285108074696.55)
        _set_external_miners(ryzen, [{
            "name": "srbminer", "binary": "bitcoind", "kind": "solo",
            "coin": "PRL", "api_port": 21373,
        }])

        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("#machBody tr.detail", timeout=15000)

        detail = """(name) => {
            const row = [...document.querySelectorAll('#machBody tr')]
                .find(r => !r.classList.contains('detail') && r.innerText.includes(name));
            return row ? row.nextElementSibling.innerText : '';
        }"""

        athlon_text = await page.evaluate(detail, f"e2e-025-athlon-{stamp}")
        ryzen_text = await page.evaluate(detail, f"e2e-025-ryzen-{stamp}")

        # `.chip` applique text-transform:uppercase — innerText est en capitales.
        assert "injoignable" not in athlon_text.lower(), athlon_text
        assert "externe" not in athlon_text.lower(), athlon_text
        assert "mineur fossor (token par défaut)" in athlon_text.lower(), athlon_text

        assert "occupé (externe)" in ryzen_text.lower(), ryzen_text
        assert ryzen_text.lower().count("occupé (externe)") == 1, ryzen_text
        assert "mineur fossor (token par défaut)" in ryzen_text.lower(), ryzen_text
        assert "hashrate inconnu" in ryzen_text.lower(), ryzen_text
        assert "216" not in ryzen_text, ryzen_text
        assert "mineur externe détecté" not in ryzen_text.lower(), ryzen_text

        # Cohérence : la fiche, Campagnes et /api/metrics/live lisent la même
        # occupation et le même api_error.
        consistency = await page.evaluate(
            """async ([ids]) => {
                const live = await (await fetch('/api/metrics/live', {credentials:'include'})).json();
                const options = await (await fetch('/api/campaigns/options', {credentials:'include'})).json();
                const byUuid = {};
                const walk = (o) => {
                    if (Array.isArray(o)) return o.forEach(walk);
                    if (o && typeof o === 'object') {
                        if (o.uuid && o.occupied_by !== undefined) byUuid[o.uuid] = o.occupied_by;
                        Object.values(o).forEach(walk);
                    }
                };
                walk(options);
                const out = [];
                for (const id of ids) {
                    const m = (await (await fetch('/api/machines', {credentials:'include'})).json())
                        .find(x => x.id === id);
                    const devices = await (await fetch('/api/machines/'+id+'/devices', {credentials:'include'})).json();
                    const l = live.find(x => x.uuid === m.uuid) || {};
                    out.push({
                        id: id,
                        machine_api_error: m.api_error,
                        live_api_error: l.api_error,
                        devices: devices.map(d => [d.uuid, d.occupied_by, options && byUuid[d.uuid]]),
                        live_devices: (l.devices || []).map(d => [d.uuid, d.occupied_by]),
                    });
                }
                return out;
            }""",
            [[athlon, ryzen]],
        )

        for row in consistency:
            assert row["machine_api_error"] == row["live_api_error"], row
            for uuid, occupied_by, campaign_by in row["devices"]:
                assert occupied_by == campaign_by, (uuid, occupied_by, campaign_by)
                live_by = dict((u, b) for u, b in row["live_devices"]).get(uuid)
                assert occupied_by == live_by, (uuid, occupied_by, live_by)

        assert not page_errors, page_errors
        await browser.close()
