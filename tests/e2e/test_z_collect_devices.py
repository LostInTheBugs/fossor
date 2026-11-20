"""e2e — fiche de l'athlon factice : hashrates GPU **et** CPU, deux profils.

Machine sans profil sélectionné, trois GPU rattachés au profil Pearl (token par
device) et un CPU rattaché au profil KSH.  La collecte est rejouée en process
(API SRBMiner figée + journal CPU), puis la fiche est relue dans Chromium.
"""

from __future__ import annotations

import json
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
NAME = "athlon-devices-e2e"
PROFILE_GPU = "pearl-gpu-e2e"
PROFILE_CPU = "ksh-cpu-e2e"
CPU_HASHRATE = 1_250_000.0
PS = (
    "### NVSMI\n"
    "0, 0000:08:00.0, 61, 148.32, 55, 100\n"
    "1, 0000:09:00.0, 63, 141.10, 54, 99\n"
    "2, 0000:0a:00.0, 64, 139.80, 53, 98\n"
    "### PS_MINERS\n"
    "root 1187 6.4 160630 /opt/fossor/miners/srbminer/3.8.3/SRBMiner-MULTI "
    "--algorithm pearlhash --wallet testwallet000000000000 "
    "--pool pearl-eu2.luckypool.io:3360 --worker athlon\n"
    "root 1188 2.1 86400 /opt/fossor/miners/cpuminer-opt/1.0.0/cpuminer-opt "
    "--algo yespower --url stratum+tcp://ksh.example.tld:3333 --threads 4\n"
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
        db.execute(text("DELETE FROM miner_profiles WHERE name IN (:a, :b)"),
                   {"a": PROFILE_GPU, "b": PROFILE_CPU})
        db.commit()
    finally:
        db.close()
        engine.dispose()


def _profile(db, name: str, coin: str, software: str, *, api_port: int = 0,
             api_kind: str = "", cmdline: str = "") -> int:
    from app.models import MinerProfile

    now = datetime.now(timezone.utc).isoformat()
    profile = MinerProfile(
        uuid=str(uuid.uuid4()), name=name, coin=coin,
        wallet=f"wallet-{name}", pool="stratum+tcp://pool.exemple.tld:3333",
        software=software, binary_url="", sha256="b" * 64,
        binary_path=f"{software}-bin", version="1.0.0",
        cmdline=cmdline or f"/opt/fossor/miners/{software}/1.0.0/{software}-bin",
        api_port=api_port, api_kind=api_kind, binary_source="fossor",
        execution_mode="host", threads=4, created_at=now, updated_at=now,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile.id


def _seed() -> tuple[int, list[float], float]:
    from app import collector as C
    from app.models import Device, Machine

    _cleanup()
    engine, db = _session()
    try:
        now = datetime.now(timezone.utc).isoformat()
        pearl = _profile(db, PROFILE_GPU, "PRL", "srbminer", api_port=21550,
                         api_kind="srbminer",
                         cmdline="/opt/fossor/miners/srbminer/3.8.3/SRBMiner-MULTI "
                                 "--algorithm pearlhash --api-port {api_port}")
        ksh = _profile(db, PROFILE_CPU, "KSH", "cpuminer",
                       cmdline="/opt/fossor/miners/cpuminer-opt/1.0.0/cpuminer-opt "
                               "--algo yespower --threads 4")
        machine = Machine(
            name=NAME, host="192.0.2.31", ssh_port=22, ssh_user="fossor",
            auth_type="fossor_key", machine_kind="host", enabled=True,
            default_miner_profile_id=None, created_at=now, updated_at=now,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        for index, bus in enumerate(["0000:08:00.0", "0000:09:00.0", "0000:0a:00.0"]):
            db.add(Device(
                uuid=str(uuid.uuid4()), machine_id=machine.id, kind="gpu", index=index,
                vendor="nvidia", model="NVIDIA GeForce RTX 3060 Ti", vram_mb=8192,
                pci_bus=bus, present=True, enabled_for_mining=True,
                default_token_id=pearl, created_at=now, updated_at=now,
            ))
        db.add(Device(
            uuid=str(uuid.uuid4()), machine_id=machine.id, kind="cpu", index=None,
            vendor="AMD", model="Athlon 3000G", cores=2, threads=4, present=True,
            enabled_for_mining=True, default_token_id=ksh,
            created_at=now, updated_at=now,
        ))
        db.commit()

        stats = C.parse_collect_stats(PS)
        api = C._parse_api_payload(
            (FIX / "srbminer_383_athlon.json").read_text(encoding="utf-8"), "srbminer"
        )
        active = C.active_profiles(db, machine, PS)
        assert [p.id for p in active] == [pearl, ksh], active
        entries = []
        for profile in active:
            entries.append(C._merge_profile_stats(
                stats, profile, api if profile.api_port else None,
                C.profile_devices(db, machine, profile),
            ))
        for item in stats.get("devices") or []:
            if item.get("kind") == "cpu":
                item["hashrate_hs"] = CPU_HASHRATE
        # Ce que la sonde CPU écrit : état + mineur du journal KSH.
        stats["cpu"] = {
            "hashrate_hs": CPU_HASHRATE,
            "miners": [{
                "coin": "KSH", "hashrate_hs": CPU_HASHRATE,
                "source": "log /var/log/athlon-korsh-miner.log", "threads": "4",
            }],
        }
        for entry in entries:
            if entry["profile_id"] == ksh:
                entry["hashrate_hs"] = CPU_HASHRATE
        stats["profiles"] = entries
        stats["total_hashrate_hs"] = sum(
            item.get("hashrate_hs") for item in stats["devices"] if item.get("hashrate_hs")
        )
        C._store_metrics(db, machine, stats, now)
        gpu_hashrates = [
            item["hashrate_hs"] for item in stats["devices"] if item.get("kind") == "gpu"
        ]
        return machine.id, gpu_hashrates, stats["total_hashrate_hs"]
    finally:
        db.close()
        engine.dispose()


def _dev_hs(value: float) -> str:
    units = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s"]
    index, number = 0, float(value)
    while number >= 1000 and index < len(units) - 1:
        number /= 1000
        index += 1
    text = f"{number:,.2f}".rstrip("0").rstrip(".")
    text = text.replace(",", "\u202f").replace(".", ",")
    return text + " " + units[index]


async def _login(page):
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_fiche_athlon_montre_hashrates_gpu_et_cpu(e2e_server):
    machine_id, gpu_hashrates, total = _seed()
    assert len(gpu_hashrates) == 3 and all(gpu_hashrates), gpu_hashrates
    assert total == pytest.approx(sum(gpu_hashrates) + CPU_HASHRATE)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await _login(page)

            response = await page.request.get(f"{E2E_URL}/api/machines/{machine_id}/devices")
            devices = await response.json()
            gpus = [item for item in devices if item["kind"] == "gpu"]
            cpu = next(item for item in devices if item["kind"] == "cpu")
            assert len(gpus) == 3, devices
            assert all(item.get("hashrate_hs") for item in gpus), devices
            assert cpu.get("hashrate_hs") == pytest.approx(CPU_HASHRATE), cpu

            await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
            await page.wait_for_selector("#machBody tr", timeout=15000)
            await page.wait_for_function(
                "() => document.getElementById('machBody').innerText.includes("
                f"'{NAME}')",
                timeout=15000,
            )
            text_page = await page.inner_text("#tableWrap")
            # Les deux hashrates : un par GPU (fiche) et celui du CPU (journal).
            for value in gpu_hashrates:
                assert _dev_hs(value) in text_page, (value, text_page)
            assert "KSH" in text_page and "1.3 MH/s" in text_page, text_page
            await browser.close()
    finally:
        _cleanup()
