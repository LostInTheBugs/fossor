"""E2E — « Pilotage HiveOS » (§3e) : écran de la fiche machine et journal de campagne.

Le serveur e2e tourne en **simulation** : la campagne journalise les commandes
HiveOS sans les exécuter.  Aucun 4xx n'est provoqué volontairement (Chromium les
journalise comme erreurs console) : le refus de la campagne partielle est
affiché par l'écran, pas déclenché par un POST refusé.
"""

import json
import sqlite3
import uuid as uuid_lib
from datetime import datetime, timezone

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

ADMIN_PASSWORD = "adminpass"
WALLET = "prl1demo" + "q" * 30
SHA = "b" * 64


def probe_json(gpu_total: int, watchdog: bool) -> str:
    return json.dumps(
        {
            "detected": True,
            "miner": "hivefake",
            "miner_bin": "/hive/bin/miner",
            "coin": "PEARL",
            "gpu_total": gpu_total,
            "mining_gpus": gpu_total,
            "hashrate_hs": 328947000000.0,
            "hashrate_source": "last_stat.json",
            "processes": [],
            "status": {"running": True, "text": "miner is running"},
            "wd_status": "wd is running",
            "custom_dir": True,
            "custom_miners": ["hivefake"],
            "miner_dirs": ["hivefake"],
            "cur_miner": "hivefake",
            "busids": [],
            "wallet": {"miner": "hivefake", "coin": "PEARL", "has_wallet": True, "wallet": "•••"},
            "rig": {
                "watchdog": watchdog,
                "maintenance": False,
                "keys": ["RIG_ID", "RIG_PASSWD", "WD_ENABLED"],
                "values": {"RIG_ID": "1", "RIG_PASSWD": "•••", "WD_ENABLED": "1" if watchdog else "0"},
            },
        }
    )


def seed_machine(name: str, devices: int, enabled: int, gpu_total: int, watchdog: bool) -> int:
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
    try:
        cursor = conn.execute(
            "INSERT INTO machines (uuid, name, host, ssh_port, ssh_user, auth_type, sudo,"
            " os_kind, usage, enabled, vm_host, platform_power_w, machine_kind,"
            " created_at, updated_at, fossor_host)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid_lib.uuid4()), name, "127.0.0.1", 1, "tester", "fossor_key", "sudo",
             "hiveos", "gpu", 1, 0, 50.0, "host",
             "2026-11-18T00:00:00+00:00", "2026-11-18T00:00:00+00:00", 0),
        )
        machine_id = cursor.lastrowid
        for index in range(devices):
            conn.execute(
                "INSERT INTO devices (uuid, machine_id, kind, \"index\", model, present,"
                " enabled_for_mining, integrated, occupied_external, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid_lib.uuid4()), machine_id, "gpu", index, f"GPU e2e {index}", 1,
                 1 if index < enabled else 0, 0, 0,
                 "2026-11-18T00:00:00+00:00", "2026-11-18T00:00:00+00:00"),
            )
        conn.execute("UPDATE machines SET hiveos_json=? WHERE id=?", (probe_json(gpu_total, watchdog), machine_id))
        conn.commit()
    finally:
        conn.close()
    return machine_id


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_hiveos_screen_and_campaign(e2e_server):
    console_errors: list[str] = []
    page_errors: list[str] = []

    partial_id = seed_machine("rig-hiveos-partiel", devices=5, enabled=2, gpu_total=5, watchdog=True)
    full_id = seed_machine("rig-hiveos-complet", devices=3, enabled=3, gpu_total=3, watchdog=True)

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

        # 1. Fiche machine : le refus de campagne partielle est visible.
        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.locator(f"tr:has-text('rig-hiveos-partiel') button[onclick='openHiveos({partial_id})']").click()
        await page.wait_for_selector("#hiveosPanel", state="visible", timeout=10000)
        summary = await page.inner_text("#hiveosSummary")
        refusal = await page.inner_text("#hiveosRefusal")
        assert "Pilotage HiveOS" in await page.inner_text("#hiveosPanel")
        assert "hivefake" in summary
        assert "Watchdog HiveOS" in summary and "actif" in summary
        assert "Devices pris en charge" in summary
        assert "campagne partielle impossible" in refusal
        assert "tous les GPU du mineur HiveOS seraient arrêtés" in refusal

        # 2. Option explicite : la même machine redevient pilotable (watchdog + tous les GPU).
        ack = await page.evaluate(
            """async () => {
                const res = await fetch('/api/hiveos/watchdog-ack', {
                    method: 'PUT', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ack: true, confirm: 'VALIDER'})
                });
                if (!res.ok) throw new Error('ack ' + res.status);
                return await res.json();
            }"""
        )
        assert ack["ack"] is True
        await page.evaluate(f"openHiveos({full_id})")
        await page.wait_for_function(
            "document.querySelector('#hiveosSummary').textContent.includes('reprise par HiveOS (miner start)')",
            timeout=10000,
        )
        full_summary = await page.inner_text("#hiveosSummary")
        assert "wd stop" in full_summary and "miner start" in full_summary

        # 3. Campagne simulée sur la machine HiveOS : le journal nomme la reprise.
        profile_id = await page.evaluate(
            """async (body) => {
                const res = await fetch('/api/miner/profiles', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (!res.ok) throw new Error('profile ' + res.status);
                return (await res.json()).id;
            }""",
            {
                "name": "Profil HiveOS e2e",
                "coin": "FAKE",
                "wallet": WALLET,
                "pool": "stratum+tcp://pool.exemple.tld:3333",
                "software": "custom",
                "binary_url": "http://127.0.0.1:9/faux-mineur.tar.gz",
                "sha256": SHA,
                "binary_path": "faux-mineur",
                "version": "1.0.0",
                "cmdline": (
                    "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} "
                    "--pool {pool} --worker {worker} --gpus {gpus}"
                ),
                "execution_mode": "host",
            },
        )
        uuids = []
        conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db")
        try:
            uuids = [row[0] for row in conn.execute(
                "SELECT uuid FROM devices WHERE machine_id=? ORDER BY \"index\"", (full_id,)
            ).fetchall()]
        finally:
            conn.close()
        campaign_id = await page.evaluate(
            """async (payload) => {
                const post = async (url, body) => {
                    const res = await fetch(url, {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body || {})
                    });
                    if (!res.ok) throw new Error(url + ' ' + res.status + ' ' + await res.text());
                    return await res.json();
                };
                const created = await post('/api/campaigns', payload);
                await post('/api/campaigns/' + created.id + '/approve', {});
                await post('/api/campaigns/' + created.id + '/start', {});
                return created.id;
            }""",
            {
                "coin": "FAKE",
                "profile_id": profile_id,
                "start_at": datetime.now(timezone.utc).isoformat(),
                "devices": [{"machine_id": full_id, "device_uuid": u} for u in uuids],
            },
        )
        assert campaign_id

        await page.goto(E2E_URL + "/campaigns", wait_until="domcontentloaded")
        await page.wait_for_selector("#rows tr", timeout=10000)
        await page.locator('button[data-act="journal"]').first.click()
        await page.wait_for_selector("#journal.open li", timeout=10000)
        journal = await page.inner_text("#journal")
        assert "hiveos-stopped" in journal
        assert "reprise par HiveOS (miner start)" in journal
        assert "miner stop" in journal and "miner start" in journal
        assert "simulé" in journal

        assert console_errors == [], console_errors
        assert page_errors == [], page_errors
        await browser.close()
