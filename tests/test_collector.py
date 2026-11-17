"""Collector tests: storage, offline machines, usage=none, secrets, purge."""

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
HIVE = (FIXTURES / "collect_stats_hiveos.txt").read_text()
UBUNTU = (FIXTURES / "collect_stats_ubuntu.txt").read_text()


def seed_machine(usage="both", enabled=True, host="rig.invalid", name="rig-collect"):
    """Create a machine with 1 CPU and 5 GPUs; return its id."""
    from app.database import SessionLocal
    from app.models import Device, Machine

    db = SessionLocal()
    try:
        machine = Machine(
            name=name,
            host=host,
            ssh_port=22,
            ssh_user="tester",
            auth_type="password",
            usage=usage,
            enabled=enabled,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(
            uuid="11111111-1111-4111-8111-111111111111",
            machine_id=machine.id, kind="cpu", model="Ryzen 9", index=0,
        ))
        for position, bus in enumerate(
            ["01:00.0", "02:00.0", "04:00.0", "05:00.0", "06:00.0"]
        ):
            db.add(Device(
                uuid=f"22222222-2222-4222-8222-00000000000{position}",
                machine_id=machine.id,
                kind="gpu",
                model=f"GeForce RTX 30{position}0",
                index=position,
                pci_bus=bus,
                vendor="NVIDIA",
            ))
        db.commit()
        return machine.id
    finally:
        db.close()


async def test_collect_stores_one_metric_per_device(monkeypatch):
    machine_id = seed_machine()
    from app import collector

    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": HIVE}

    async def no_api(host, port):  # pragma: no cover - HiveOS covers hashrates
        return None

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    monkeypatch.setattr(collector, "fetch_srbminer", no_api)

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True
    assert result["devices"] == 6  # 1 CPU + 5 GPU
    assert result["api_port"] == 21550

    from app.database import SessionLocal
    from app.models import MachineStatus, Metric

    db = SessionLocal()
    try:
        metrics = db.query(Metric).all()
        assert len(metrics) == 6
        assert all(m.device_uuid for m in metrics)  # every row matched a device
        gpu_rows = [
            m for m in metrics
            if m.device_uuid != "11111111-1111-4111-8111-111111111111"
        ]
        assert len(gpu_rows) == 5
        assert gpu_rows[0].hashrate_hs == pytest.approx(6.853887963e13)
        assert gpu_rows[0].temp_c == 42
        assert gpu_rows[0].power_w == pytest.approx(93)

        status = db.query(MachineStatus).one()
        assert status.online is True
        assert status.error is None
        assert status.miner == "srbminer"
        assert status.coin == "PRL"  # alias PEARL → PRL (2026.11.003-c4)
        assert status.api_port == 21550
        assert status.total_hashrate_hs == pytest.approx(3.28947e14)
    finally:
        db.close()


async def test_collect_marks_machine_offline_with_its_error(monkeypatch):
    machine_id = seed_machine(name="rig-offline")
    from app import collector

    async def failing_ssh(machine, actor=None):
        return {"ok": False, "error": "Empreinte changée : ancienne A, nouvelle B"}

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", failing_ssh)

    result = await collector.collect_machine(machine_id)
    assert result["online"] is False
    assert "Empreinte changée" in result["error"]

    from app.database import SessionLocal
    from app.models import MachineStatus, Metric

    db = SessionLocal()
    try:
        status = db.query(MachineStatus).one()
        assert status.online is False
        assert "Empreinte changée" in status.error
        assert db.query(Metric).count() == 0
    finally:
        db.close()


async def test_usage_none_is_never_contacted(monkeypatch):
    machine_id = seed_machine(usage="none", name="rig-none")
    from app import collector

    calls: list[int] = []

    async def spy_ssh(machine, actor=None):
        calls.append(machine.id)
        return {"ok": True, "stdout": HIVE}

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", spy_ssh)

    result = await collector.collect_machine(machine_id)
    assert result.get("skipped") is True
    assert calls == []

    # collect_all filters it out before any connection attempt.
    assert await collector.collect_all() == []
    assert calls == []


async def test_srbminer_api_completes_ubuntu_machine(monkeypatch):
    machine_id = seed_machine(name="rig-ubuntu", host="ubuntu.invalid")
    from app import collector
    from app.stats_parser import parse_srbminer_api

    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": UBUNTU}

    async def fake_api(host, port):
        assert (host, port) == ("ubuntu.invalid", 21550)
        return parse_srbminer_api((FIXTURES / "srbminer_api.json").read_text())

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    monkeypatch.setattr(collector, "fetch_srbminer", fake_api)

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True

    from app.database import SessionLocal
    from app.models import MachineStatus, Metric

    db = SessionLocal()
    try:
        hashrates = sorted(
            m.hashrate_hs for m in db.query(Metric).all() if m.hashrate_hs
        )
        assert hashrates == [6.84e13, 6.854e13]
        status = db.query(MachineStatus).one()
        assert status.accepted == 1420
        assert status.total_hashrate_hs == pytest.approx(1.3694e14)
    finally:
        db.close()


async def test_hiveos_secrets_reach_neither_database_nor_logs(monkeypatch, caplog):
    machine_id = seed_machine(name="rig-secrets")
    from app import collector

    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": HIVE}

    async def no_api(host, port):
        return None

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    monkeypatch.setattr(collector, "fetch_srbminer", no_api)

    with caplog.at_level("DEBUG"):
        await collector.collect_machine(machine_id)

    assert "S3CR3T-HIVE-PASS" not in caplog.text
    assert "RIG-FAKE-ABC123" not in caplog.text

    db_path = os.environ["DATABASE_URL"].replace("sqlite:///", "")
    # app.database freezes its SessionLocal at import (see skill notes): read the
    # database the collector actually wrote to, not the env var of this test.
    from app.database import engine

    db_path = engine.url.database or db_path
    with open(db_path, "rb") as handle:
        raw = handle.read()
    assert b"S3CR3T-HIVE-PASS" not in raw
    assert b"RIG-FAKE-ABC123" not in raw


def test_purge_removes_only_expired_metrics(monkeypatch):
    monkeypatch.setenv("FOSSOR_METRICS_RETENTION_DAYS", "7")
    from app.database import SessionLocal
    from app.models import Metric

    db = SessionLocal()
    try:
        db.add(Metric(machine_uuid="m-old", ts="2020-01-01T00:00:00+00:00", temp_c=40))
        db.add(Metric(machine_uuid="m-new", ts="2999-01-01T00:00:00+00:00", temp_c=50))
        db.commit()
    finally:
        db.close()

    from app import collector

    assert collector.purge_old_metrics() == 1

    db = SessionLocal()
    try:
        remaining = [m.machine_uuid for m in db.query(Metric).all()]
        assert remaining == ["m-new"]
    finally:
        db.close()


def test_collect_interval_is_configurable(monkeypatch):
    from app import collector

    monkeypatch.setenv("FOSSOR_COLLECT_INTERVAL", "15")
    assert collector.collect_interval() == 15
    monkeypatch.setenv("FOSSOR_COLLECT_INTERVAL", "nonsense")
    assert collector.collect_interval() == 60
    monkeypatch.setenv("FOSSOR_METRICS_RETENTION_DAYS", "3")
    assert collector.retention_days() == 3
