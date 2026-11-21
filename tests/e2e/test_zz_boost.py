"""e2e — Campagnes → Boost : pool, écran de validation, lancement, annulation."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from tests.e2e.conftest import E2E_DATA_DIR, defer_newest_proposed_campaign

pytestmark = pytest.mark.e2e

WALLET = "prl1testwalletfossor0000000000000000000000000000000000"


def _seed_pool() -> tuple[int, str]:
    """Machine non HiveOS + un GPU dans le pool (jamais une machine réelle)."""
    conn = sqlite3.connect(f"{E2E_DATA_DIR}/fossor.db", timeout=20)
    try:
        machine_id = int(uuid.uuid4().int % 10**6) + 900000
        device_uuid = str(uuid.uuid4())
        name = "boost-e2e-" + uuid.uuid4().hex[:6]
        conn.execute(
            "INSERT INTO machines (uuid,name,host,ssh_port,ssh_user,auth_type,usage,enabled,"
            "simulation_disabled,vm_host) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), name, "192.0.2.77", 22, "tester", "password", "both", 1, 0, 0),
        )
        machine_id = int(conn.execute("SELECT id FROM machines WHERE name=?", (name,)).fetchone()[0])
        conn.execute(
            "INSERT INTO devices (uuid,machine_id,kind,\"index\",model,present,enabled_for_mining,"
            "integrated,occupied_external,role,boost_pool) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (device_uuid, machine_id, "gpu", 0, "RTX e2e", 1, 1, 0, 0, "default_and_spec", 1),
        )
        conn.commit()
        return machine_id, device_uuid
    finally:
        conn.close()


async def test_boost_pool_validation_launch_and_cancel(page, e2e_server):
    machine_id, device_uuid = _seed_pool()
    profile_id = await page.evaluate(
        """async (payload) => {
            const res = await fetch('/api/campaigns/options');
            const options = await res.json();
            const existing = (options.profiles || [])[0];
            if (existing) { return existing.id; }
            const created = await fetch('/api/profiles', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            return (await created.json()).id;
        }""",
        {
            "name": "Profil boost e2e",
            "coin": "NEWC",
            "wallet": WALLET,
            "pool": "stratum+tcp://pool.exemple.tld:3333",
            "software": "srbminer",
            "binary_url": "http://192.0.2.1/srbminer.tar.gz",
            "sha256": "0" * 64,
            "binary_path": "/opt/fossor/miners/srbminer",
            "version": "1.0.0",
            "cmdline": "/bin/true",
            "execution_mode": "host",
        },
    )
    campaign_id = await page.evaluate(
        """async (payload) => {
            const created = await fetch('/api/campaigns', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            const row = await created.json();
            return row.id;
        }""",
        {
            "coin": "NEWC",
            "profile_id": profile_id,
            "start_at": datetime.now(timezone.utc).isoformat(),
            "devices": [{"machine_id": machine_id, "device_uuid": device_uuid}],
        },
    )
    defer_newest_proposed_campaign(60)
    await page.evaluate(
        """async (id) => {
            const res = await fetch('/api/campaigns/' + id + '/approve', {method: 'POST'});
            if (!res.ok) { throw new Error('approve ' + res.status); }
        }""",
        campaign_id,
    )

    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    await page.goto(f"{e2e_server}/campaigns", wait_until="networkidle")
    await page.wait_for_selector("#boostPool input[data-boost-machine]", timeout=15000)

    label = await page.inner_text("#boostPool")
    assert "RTX e2e" in label
    assert "exclu" in await page.inner_text("#boostPoolNote")

    await page.check(f'#boostPool input[value="{device_uuid}"]')
    await page.select_option("#boostCampaign", str(campaign_id))
    await page.click("#boostPreview")
    await page.wait_for_selector("#boostPanel table", timeout=10000)
    panel = await page.inner_text("#boostPanel")
    assert "Hashrate Pearl perdu" in panel
    assert "Coût électrique estimé" in panel
    assert "Revenu estimé du coin boosté" in panel

    await page.click("#boostLaunch")
    await page.wait_for_function(
        """() => {
            const rows = Array.from(document.querySelectorAll('#boostRows tr'));
            return rows.some((row) => row.textContent.includes('en cours'));
        }""",
        timeout=15000,
    )
    row_text = await page.inner_text("#boostRows")
    assert "boost" in row_text.lower()

    await page.click('#boostRows button[data-boost-act="cancel"]')
    await page.wait_for_function(
        """() => {
            const rows = Array.from(document.querySelectorAll('#boostRows tr'));
            return rows.some((row) => row.textContent.includes('terminé'));
        }""",
        timeout=15000,
    )
    assert errors == [], errors
