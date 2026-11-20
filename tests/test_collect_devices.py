"""Collecte multi-profils : tokens par device, sans profil sélectionné (athlon).

Trois GPU sur le profil A (API SRBMiner factice) et un CPU sur le profil B
(journal factice), ``machines.default_miner_profile_id`` laissé à NULL : c'est
la configuration réelle de la migration, que la 015 n'avait pas couverte.
Aucune connexion : SSH, API et sonde CPU sont remplacés.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"

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
    "--algo yespower --url stratum+tcp://ksh.example.tld:3333 "
    "--user testwallet --threads 4\n"
    "### UPTIME\n"
    "160630.00 999999.00\n"
)

CPU_HASHRATE = 1_250_000.0


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


def _seed(db) -> tuple[int, int, int]:
    """Machine athlon factice : 3 GPU → A, 1 CPU → B, aucun profil sélectionné."""
    from app.models import Device, Machine

    now = datetime.now(timezone.utc).isoformat()
    pearl = _profile(db, "pearl-gpu", "PRL", "srbminer", api_port=21550,
                     api_kind="srbminer",
                     cmdline="/opt/fossor/miners/srbminer/3.8.3/SRBMiner-MULTI "
                             "--algorithm pearlhash --api-port {api_port}")
    ksh = _profile(db, "ksh-cpu", "KSH", "cpuminer",
                   cmdline="/opt/fossor/miners/cpuminer-opt/1.0.0/cpuminer-opt "
                           "--algo yespower --threads 4")
    machine = Machine(
        name="athlon-devices", host="192.0.2.10", ssh_port=22, ssh_user="fossor",
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
    return machine.id, pearl, ksh


@pytest.fixture
def session():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _patch_collect(monkeypatch, session, seen: dict):
    from app import collector as C
    from app import ssh_client

    async def fake_collect(machine, actor="system"):
        return {"ok": True, "stdout": PS}

    async def fake_api(machine, port, api_kind=""):
        seen["ports"] = seen.get("ports", []) + [port]
        seen["kind"] = api_kind
        payload = (FIX / "srbminer_383_athlon.json").read_text(encoding="utf-8")
        return C._parse_api_payload(payload, api_kind), None

    async def fake_devices(db, machine):
        return False

    async def fake_cpu(db, machine, stdout, stats):
        # Résultat de la sonde : le journal du mineur CPU KSH.
        stats["cpu"] = {
            "hashrate_hs": CPU_HASHRATE,
            "miners": [{
                "coin": "KSH", "hashrate_hs": CPU_HASHRATE,
                "source": "log /var/log/athlon-korsh-miner.log", "threads": "4",
            }],
        }
        for item in stats.get("devices") or []:
            if item.get("kind") == "cpu":
                item["hashrate_hs"] = CPU_HASHRATE
        return "### LOG /var/log/athlon-korsh-miner.log\n" + str(CPU_HASHRATE)

    monkeypatch.setattr(ssh_client, "ssh_collect_stats", fake_collect)
    monkeypatch.setattr(C, "_fetch_miner_api", fake_api)
    monkeypatch.setattr(C, "_ensure_devices", fake_devices)
    monkeypatch.setattr(C, "_enrich_cpu_state", fake_cpu)

    import app.database as D

    class _Factory:
        def __call__(self):
            return session

    monkeypatch.setattr(D, "SessionLocal", _Factory())


class TestActiveProfiles:
    def test_union_of_device_tokens_without_selected_profile(self, session):
        from app import collector as C
        from app.models import Machine

        machine_id, pearl, ksh = _seed(session)
        machine = session.query(Machine).filter(Machine.id == machine_id).one()
        assert machine.default_miner_profile_id is None
        assert [p.id for p in C.active_profiles(session, machine, PS)] == [pearl, ksh]

    def test_unit_seen_adds_a_profile_without_token(self, session):
        from app import collector as C
        from app.models import Machine

        machine_id, pearl, _ksh = _seed(session)
        extra = _profile(session, "unite-vue", "XMR", "xmrig")
        machine = session.query(Machine).filter(Machine.id == machine_id).one()
        found = [p.id for p in C.active_profiles(session, machine, PS)]
        assert pearl in found
        assert extra not in found


class TestCollectDevices:
    async def test_multi_profiles_collect_gpu_and_cpu_hashrates(
        self, session, monkeypatch
    ):
        from app import collector as C
        from app.models import MachineStatus

        machine_id, pearl, ksh = _seed(session)
        seen: dict = {}
        _patch_collect(monkeypatch, session, seen)

        result = await C.collect_machine(machine_id)

        assert result.get("online") is True, result
        assert result["miner"] == "srbminer"
        assert result["api_port"] == 21550
        assert seen["ports"] == [21550]
        assert seen["kind"] == "srbminer"

        status = session.query(MachineStatus).one()
        profiles = json.loads(status.profiles_json)
        assert [p["profile_id"] for p in profiles] == [pearl, ksh]
        assert profiles[0]["hashrate_hs"] and profiles[0]["hashrate_hs"] > 7e13
        assert profiles[1]["hashrate_hs"] == pytest.approx(CPU_HASHRATE)
        assert status.accepted == sum(
            p["accepted"] or 0 for p in profiles
        )
        assert status.total_hashrate_hs == pytest.approx(
            profiles[0]["hashrate_hs"] + CPU_HASHRATE
        )
        assert status.api_port == 21550 and status.miner == "srbminer"


class TestMinerStatusDevices:
    async def test_status_returns_one_entry_per_profile(
        self, session, monkeypatch, client
    ):
        from app import collector as C
        from app.routers import miners as ROUTER

        machine_id, pearl, ksh = _seed(session)

        async def fake_status(machine, profile, *, simulated, user):
            running = profile["name"] != "ksh-cpu"
            return {"status": {
                "running": running, "state": "active" if running else "inactive",
                "pid": 1234 if running else None, "uptime_s": 60,
                "hashrate_hs": 24_500_000_000_000.0 if running else None,
                "service": profile["service"],
            }}

        monkeypatch.setattr(ROUTER.R, "status", fake_status)
        response = await client.get(f"/api/machines/{machine_id}/miner/status")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert [p["profile_id"] for p in payload["profiles"]] == [pearl, ksh]
        assert payload["profiles"][0]["active"] is True
        assert payload["profiles"][0]["hashrate_hs"] == pytest.approx(24_500_000_000_000.0)
        assert payload["profiles"][1]["active"] is False

    async def test_settings_and_machine_view_are_labelled(self, client, session):
        machine_id, _pearl, _ksh = _seed(session)
        settings = (await client.get("/api/miner/settings")).json()
        assert settings["meaning"] == "valeur par défaut des machines non armées"
        view = (await client.get(f"/api/machines/{machine_id}/miner")).json()
        assert "token par défaut" in view["selection_meaning"]

    async def test_status_without_any_profile_still_refuses(self, client, session):
        from app.models import Machine

        now = datetime.now(timezone.utc).isoformat()
        bare = Machine(name="sans-profil", host="192.0.2.11", ssh_port=22,
                       ssh_user="fossor", auth_type="fossor_key",
                       created_at=now, updated_at=now)
        session.add(bare)
        session.commit()
        response = await client.get(f"/api/machines/{bare.id}/miner/status")
        assert response.status_code == 400
