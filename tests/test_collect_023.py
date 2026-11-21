"""023 — un profil d'une autre machine ne fait plus échouer la santé.

Cas réel : le ``ps`` du Ryzen cite le binaire SRBMiner 3.8.3, commun au profil
Pearl de l'Athlon (port 21550).  La collecte interrogeait ce port fermé et
posait ``api_error`` alors que le GPU du Ryzen remontait par son propre profil
(21373) — pastille orange à tort.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

PS_RYZEN = (
    "### NVSMI\n"
    "0, 0000:08:00.0, 67, 314.00, 64, 100\n"
    "### PS_MINERS\n"
    "fossor 1197806 0.0 84533 /usr/bin/script -qfec /opt/fossor/miners/srbminer/"
    "3.8.3/srbminer_compute --algorithm pearlhash --pool "
    "stratum+tcp://pearl-eu2.luckypool.io:3360 --wallet testwallet --worker ryzen "
    "--api-enable --api-port 21373 /dev/null\n"
    "fossor 1197807 1.6 84533 /opt/fossor/miners/srbminer/3.8.3/srbminer_compute "
    "--algorithm pearlhash --pool stratum+tcp://pearl-eu2.luckypool.io:3360 "
    "--wallet testwallet --worker ryzen --api-enable --api-port 21373\n"
    "### UPTIME\n"
    "84421.00 999999.00\n"
)

BIN = "/opt/fossor/miners/srbminer/3.8.3/srbminer_compute"


def _profile(db, name: str, *, api_port: int, worker: str) -> int:
    from app.models import MinerProfile

    now = datetime.now(timezone.utc).isoformat()
    row = MinerProfile(
        uuid=str(uuid.uuid4()), name=name, coin="PRL",
        wallet="testwallet", pool="stratum+tcp://pearl-eu2.luckypool.io:3360",
        software="srbminer", binary_url="", sha256="b" * 64, binary_path=BIN,
        version="3.8.3", cmdline=f"{BIN} --algorithm pearlhash --api-port {api_port}",
        api_port=api_port, api_kind="srbminer", binary_source="fossor",
        execution_mode="host", threads=0, worker=worker,
        created_at=now, updated_at=now,
    )
    db.add(row); db.commit(); db.refresh(row)
    return row.id


def _machine(db, name: str, host: str, *, gpu_token: int) -> int:
    from app.models import Device, Machine

    now = datetime.now(timezone.utc).isoformat()
    machine = Machine(
        name=name, host=host, ssh_port=22, ssh_user="fossor",
        auth_type="fossor_key", machine_kind="host", enabled=True,
        default_miner_profile_id=None, created_at=now, updated_at=now,
    )
    db.add(machine); db.commit(); db.refresh(machine)
    db.add(Device(
        uuid=str(uuid.uuid4()), machine_id=machine.id, kind="gpu", index=0,
        vendor="nvidia", model="NVIDIA GeForce RTX 4080", vram_mb=16384,
        pci_bus="0000:08:00.0", present=True, enabled_for_mining=True,
        role="default_and_spec", default_token_id=gpu_token,
        created_at=now, updated_at=now,
    ))
    db.commit()
    return machine.id


def test_un_profil_etranger_n_est_pas_actif(db):
    """Le binaire commun ne prouve pas que le profil de l'Athlon tourne ici."""
    from app import collector as C

    athlon_profile = _profile(db, "Athlon Pearl GPU (SRBMiner)", api_port=21550, worker="athlon")
    ryzen_profile = _profile(db, "Ryzen Pearl GPU (SRBMiner)", api_port=21373, worker="ryzen")
    _machine(db, "athlon", "192.0.2.10", gpu_token=athlon_profile)
    ryzen_id = _machine(db, "ryzen", "192.0.2.55", gpu_token=ryzen_profile)
    from app.models import Machine

    ryzen = db.get(Machine, ryzen_id)

    active = C.active_profiles(db, ryzen, PS_RYZEN)

    assert [p.id for p in active] == [ryzen_profile]


def test_collecte_ne_sonde_que_le_port_du_profil_de_la_machine(db, monkeypatch):
    """La santé reste verte : le port étranger 21550 n'est même pas interrogé."""
    from app import collector as C
    from app import health, ssh_client
    from app.models import MachineStatus

    athlon_profile = _profile(db, "Athlon Pearl GPU (SRBMiner)", api_port=21550, worker="athlon")
    ryzen_profile = _profile(db, "Ryzen Pearl GPU (SRBMiner)", api_port=21373, worker="ryzen")
    _machine(db, "athlon", "192.0.2.10", gpu_token=athlon_profile)
    ryzen_id = _machine(db, "ryzen", "192.0.2.55", gpu_token=ryzen_profile)
    seen: list[int] = []

    async def fake_collect(machine, actor="system"):
        return {"ok": True, "stdout": PS_RYZEN}

    async def fake_api(machine, port, api_kind=""):
        seen.append(int(port))
        if int(port) != 21373:
            return None, "API du mineur injoignable (direct : port fermé ; SSH : API du mineur injoignable)"
        payload = (FIX / "srbminer_383_athlon.json").read_text(encoding="utf-8")
        return C._parse_api_payload(payload, api_kind), None

    async def no_detect(database, machine):
        return True

    async def no_cpu(database, machine, stdout, stats):
        return ""

    monkeypatch.setattr(ssh_client, "ssh_collect_stats", fake_collect)
    monkeypatch.setattr(C, "_fetch_miner_api", fake_api)
    monkeypatch.setattr(C, "_ensure_devices", no_detect)
    monkeypatch.setattr(C, "_enrich_cpu_state", no_cpu)

    result = asyncio.run(C.collect_machine(ryzen_id))

    assert seen == [21373]
    assert result["online"] is True
    row = db.query(MachineStatus).filter(MachineStatus.machine_uuid == result.get("uuid", "")).first()
    if row is None:
        from app.models import Machine

        machine = db.get(Machine, ryzen_id)
        row = db.query(MachineStatus).filter(MachineStatus.machine_uuid == machine.uuid).first()
    assert row is not None
    assert row.api_error is None
    status = health.evaluate(
        {"collected": True, "online": True, "api_error": row.api_error,
         "total_hashrate_hs": row.total_hashrate_hs,
         "devices": [{"kind": "gpu", "hashrate_hs": row.total_hashrate_hs,
                      "enabled_for_mining": True, "present": True}],
         "cpu": {}}, {}, {},
    )
    assert status["status"] == "green", status


def test_api_injoignable_sans_hashrate_reste_signalee(db, monkeypatch):
    """La règle inverse tient toujours : pas de hashrate → api_error visible."""
    from app import collector as C
    from app import ssh_client
    from app.models import MachineStatus

    profile = _profile(db, "Ryzen Pearl GPU (SRBMiner)", api_port=21373, worker="ryzen")
    machine_id = _machine(db, "ryzen", "192.0.2.55", gpu_token=profile)

    async def fake_collect(machine, actor="system"):
        return {"ok": True, "stdout": "### PS_MINERS\n### UPTIME\n100.00 999.00\n"}

    async def fake_api(machine, port, api_kind=""):
        return None, "API du mineur injoignable (direct : port fermé ; SSH : API du mineur injoignable)"

    async def no_detect(database, machine):
        return True

    async def no_cpu(database, machine, stdout, stats):
        return ""

    monkeypatch.setattr(ssh_client, "ssh_collect_stats", fake_collect)
    monkeypatch.setattr(C, "_fetch_miner_api", fake_api)
    monkeypatch.setattr(C, "_ensure_devices", no_detect)
    monkeypatch.setattr(C, "_enrich_cpu_state", no_cpu)

    result = asyncio.run(C.collect_machine(machine_id))
    from app.models import Machine

    machine = db.get(Machine, machine_id)
    row = db.query(MachineStatus).filter(MachineStatus.machine_uuid == machine.uuid).first()
    assert row is not None and row.api_error
    assert "injoignable" in row.api_error
