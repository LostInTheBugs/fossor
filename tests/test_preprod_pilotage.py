"""Préprod — ASIC hors pilotage et simulation par machine."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import auth_settings, pilotage
from app.database import SessionLocal
from app.models import Device, Machine, MinerProfile
from app.rules import free_capacity, validate_proposal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _machine(name: str, kind: str = "host", **extra) -> Machine:
    db = SessionLocal()
    try:
        row = Machine(name=name, host="127.0.0.1", machine_kind=kind, **extra)
        db.add(row)
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def _device(machine_id: int, kind: str = "cpu", index: int = 0) -> str:
    db = SessionLocal()
    try:
        row = Device(
            uuid=str(uuid.uuid4()),
            machine_id=machine_id,
            kind=kind,
            index=index,
            model="Test",
            present=True,
            enabled_for_mining=True,
            role="default_and_spec",
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(row)
        db.commit()
        return row.uuid
    finally:
        db.close()


@pytest.mark.asyncio
async def test_campaign_options_never_list_an_asic(client):
    _machine("athlon-3000g")
    asic = _machine("ks0-ultra", kind="asic")
    _device(asic.id)
    response = await client.get("/api/campaigns/options")
    assert response.status_code == 200
    names = [row["name"] for row in response.json()["machines"]]
    assert "athlon-3000g" in names
    assert "ks0-ultra" not in names


@pytest.mark.asyncio
async def test_campaign_creation_refuses_an_asic(client):
    asic = _machine("ks0-ultra-b", kind="asic")
    device_uuid = _device(asic.id)
    response = await client.post(
        "/api/campaigns",
        json={
            "coin": "XMR",
            "profile_id": 1,
            "start_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "devices": [{"machine_id": asic.id, "device_uuid": device_uuid}],
        },
    )
    assert response.status_code == 400
    assert "ASIC" in response.json()["detail"]


def test_free_capacity_excludes_asic_devices():
    asic = _machine("ks0-ultra-c", kind="asic")
    _device(asic.id)
    db = SessionLocal()
    try:
        capacity = free_capacity(db)
    finally:
        db.close()
    labels = [row["machine"] for row in capacity["cpu"] + capacity["gpu"]]
    assert "ks0-ultra-c" not in labels


def test_proposal_refuses_an_asic_device():
    asic = _machine("ks0-ultra-d", kind="asic")
    device_uuid = _device(asic.id)
    db = SessionLocal()
    try:
        profile = MinerProfile(
            uuid=str(uuid.uuid4()),
            name="Test XMR",
            coin="XMR",
            wallet="wallet-de-test",
            pool="pool.exemple.tld:3333",
            software="xmrig",
            binary_url="https://exemple.tld/xmrig.tar.gz",
            sha256="0" * 64,
            binary_path="/usr/bin/xmrig",
            version="6.21.0",
            cmdline="{binary} -o {pool} -u {wallet}",
        )
        db.add(profile)
        db.commit()
        from app.rules import ProposalIn

        data = ProposalIn(
            coin="XMR",
            profile_id=profile.id,
            device_uuids=[device_uuid],
            start_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            end_at=None,
            note="test",
        )
        verdict = validate_proposal(db, data)
    finally:
        db.close()
    codes = {reason["code"] for reason in verdict["reasons"]}
    assert "asic_not_pilotable" in codes


@pytest.mark.asyncio
async def test_machine_simulation_requires_piloter_then_stays_under_global(client):
    host = _machine("athlon-3000g")
    refused = await client.put(f"/api/machines/{host.id}/simulation", json={"real": True})
    assert refused.status_code == 400
    assert "PILOTER" in refused.json()["detail"]

    armed = await client.put(
        f"/api/machines/{host.id}/simulation", json={"real": True, "confirm": "PILOTER"}
    )
    assert armed.status_code == 200
    assert armed.json()["simulation_disabled"] is True
    # Machine armée : elle pilote en réel, même avec le global ON.
    assert armed.json()["simulated"] is False
    assert armed.json()["pilotage"] == "Pilotage réel"

    listing = await client.get("/api/machines")
    row = next(item for item in listing.json() if item["id"] == host.id)
    assert row["simulation_disabled"] is True
    assert row["pilotage"] == "Pilotage réel"


@pytest.mark.asyncio
async def test_real_piloting_needs_both_switches(client):
    host = _machine("athlon-3000g-b")
    await client.put(
        f"/api/machines/{host.id}/simulation", json={"real": True, "confirm": "PILOTER"}
    )
    db = SessionLocal()
    try:
        from app.ai_config import set_setting

        set_setting(db, "simulation_mode", False)
        db.commit()
        row = db.query(Machine).filter(Machine.id == host.id).first()
        assert pilotage.simulated_for(db, row) is False
        assert pilotage.label(db, row) == "Pilotage réel"
        row.simulation_disabled = False
        db.commit()
        assert pilotage.simulated_for(db, row) is True
        assert pilotage.label(db, row) == "Simulation (machine non armée)"
        set_setting(db, "simulation_mode", True)
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_simulation_change_is_audited(client):
    from app.models import AuditLog

    host = _machine("athlon-3000g-c")
    response = await client.put(
        f"/api/machines/{host.id}/simulation", json={"real": True, "confirm": "PILOTER"}
    )
    assert response.status_code == 200
    db = SessionLocal()
    try:
        row = (
            db.query(AuditLog)
            .filter(AuditLog.action == "machine-simulation")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert "PILOTER" not in (row.output or "")  # jamais le mot recopié
    finally:
        db.close()
