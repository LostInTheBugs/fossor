"""e2e — fiche de l'athlon factice : hashrate GPU d'une unité lancée par Fossor.

Aucun SSH : la collecte part d'un ``ps`` réel (sans ``--api-port``) et l'API du
mineur est le corps JSON figé de l'athlon. Le port vient donc **du profil**,
puis le hashrate par GPU est écrit par ``_store_metrics`` et affiché par la
fiche machine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.e2e.conftest import E2E_DATA_DIR, E2E_URL

DB_PATH = f"{E2E_DATA_DIR}/fossor.db"
FIX = Path(__file__).resolve().parent.parent / "fixtures"
ADMIN_PASSWORD = "adminpass"
NAME = "athlon-postmigration-e2e"
PROFILE = "athlon-postmigration"
SHA = "b" * 64
PS = (
    "### NVSMI\n"
    "0, 0000:08:00.0, 61, 148.32, 55, 100\n"
    "1, 0000:09:00.0, 63, 141.10, 54, 99\n"
    "2, 0000:0a:00.0, 64, 139.80, 53, 98\n"
    "### PS_MINERS\n"
    "root 1187 6.4 160630 /opt/fossor/miners/srbminer/3.8.3/SRBMiner-MULTI "
    "--algorithm pearlhash --wallet testwallet000000000000 "
    "--pool pearl-eu2.luckypool.io:3360 --worker athlon --api-enable\n"
    "### UPTIME\n"
    "160630.00 999999.00\n"
)


def _session():
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    return engine, sessionmaker(bind=engine)()


def _cleanup() -> None:
    engine, db = _session()
    try:
        row = db.execute(text("SELECT id, uuid FROM machines WHERE name = :name"),
                         {"name": NAME}).first()
        if row:
            db.execute(text("DELETE FROM devices WHERE machine_id = :id"), {"id": row[0]})
            db.execute(text("DELETE FROM metrics WHERE machine_uuid = :uuid"), {"uuid": row[1]})
            db.execute(text("DELETE FROM machine_status WHERE machine_uuid = :uuid"),
                       {"uuid": row[1]})
            db.execute(text("DELETE FROM machines WHERE id = :id"), {"id": row[0]})
        db.execute(text("DELETE FROM miner_profiles WHERE name = :name"), {"name": PROFILE})
        db.commit()
    finally:
        db.close()
        engine.dispose()


def _seed() -> tuple[int, list[float]]:
    from app import collector as C
    from app.models import Device, Machine, MinerProfile
    from app.srbminer_api import merge_srbminer, parse_srbminer_api

    _cleanup()
    engine, db = _session()
    try:
        now = datetime.now(timezone.utc).isoformat()
        profile = MinerProfile(
            uuid=str(uuid.uuid4()),
            name=PROFILE,
            coin="KSH",
            wallet="Ktestaddress000000000000000000",
            pool="stratum+tcp://pool.exemple.tld:3333",
            software="srbminer",
            binary_url="",
            sha256=SHA,
            binary_path="SRBMiner-MULTI",
            version="3.8.3",
            cmdline=(
                "/opt/fossor/miners/srbminer/3.8.3/SRBMiner-MULTI --algorithm pearlhash "
                "--pool {pool} --wallet {wallet} --gpus {gpus} --api-port {api_port}"
            ),
            api_port=21550,
            api_kind="srbminer",
            binary_source="fossor",
            execution_mode="host",
            created_at=now,
            updated_at=now,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)

        machine = Machine(
            name=NAME,
            host="192.0.2.31",
            ssh_port=22,
            ssh_user="fossor",
            auth_type="fossor_key",
            machine_kind="host",
            enabled=True,
            default_miner_profile_id=profile.id,
            created_at=now,
            updated_at=now,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)

        for index, bus in enumerate(["0000:08:00.0", "0000:09:00.0", "0000:0a:00.0"]):
            db.add(Device(
                uuid=str(uuid.uuid4()), machine_id=machine.id, kind="gpu", index=index,
                vendor="nvidia", model="NVIDIA GeForce RTX 3060 Ti", vram_mb=8192,
                pci_bus=bus, present=True, enabled_for_mining=True,
                created_at=now, updated_at=now,
            ))
        db.commit()

        stats = C.parse_collect_stats(PS)
        applied = C.launched_profile(db, machine, PS)
        assert applied is not None, "l'unité Fossor doit être reconnue dans le ps"
        stats = C._apply_profile_api(stats, applied)
        assert stats["api_port"] == 21550, stats
        api = parse_srbminer_api((FIX / "srbminer_383_athlon.json").read_text(encoding="utf-8"))
        stats = merge_srbminer(stats, api)
        C._store_metrics(db, machine, stats, now)
        return machine.id, [gpu["hashrate_hs"] for gpu in stats["devices"]
                            if gpu.get("kind") == "gpu"]
    finally:
        db.close()
        engine.dispose()


async def _login(page):
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_fiche_athlon_montre_le_hashrate_par_gpu(e2e_server):
    machine_id, gpu_hashrates = _seed()
    assert len(gpu_hashrates) == 3 and all(gpu_hashrates), gpu_hashrates
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await _login(page)

            response = await page.request.get(f"{E2E_URL}/api/machines/{machine_id}/devices")
            devices = await response.json()
            gpus = [item for item in devices if item["kind"] == "gpu"]
            assert len(gpus) == 3, devices
            assert all(item.get("hashrate_hs") for item in gpus), devices

            await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
            await page.wait_for_selector("#machBody tr", timeout=15000)
            await page.wait_for_function(
                "() => document.getElementById('machBody').innerText.includes("
                "'athlon-postmigration-e2e')",
                timeout=15000,
            )
            upper = (await page.inner_text("#tableWrap")).upper()
            assert "HASHRATE" in upper
            assert "TH/S" in upper
            await browser.close()
    finally:
        _cleanup()
