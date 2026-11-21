"""023 — compléments du boost : threads CPU et proposition pilotée par la règle."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

BIN = "/opt/fossor/miners/srbminer/3.8.3/srbminer_compute"


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed(db, *, threads: int = 12, cpu: bool = False):
    from app.models import Device, Machine, MinerProfile

    now = _now().isoformat()
    profile = MinerProfile(
        uuid=str(uuid.uuid4()), name="Ryzen Pearl GPU (SRBMiner)", coin="PRL",
        wallet="testwallet", pool="stratum+tcp://pearl-eu2.luckypool.io:3360",
        software="srbminer", binary_url="", sha256="b" * 64, binary_path=BIN,
        version="3.8.3", cmdline=f"{BIN} --algorithm pearlhash",
        api_port=21373, api_kind="srbminer", binary_source="fossor",
        execution_mode="host", threads=0, worker="ryzen",
        created_at=now, updated_at=now,
    )
    db.add(profile); db.commit(); db.refresh(profile)
    machine = Machine(
        name="ryzen-sim", host="192.0.2.55", ssh_port=22, ssh_user="fossor",
        auth_type="fossor_key", machine_kind="host", enabled=True,
        simulation_disabled=False, created_at=now, updated_at=now,
    )
    db.add(machine); db.commit(); db.refresh(machine)
    device = Device(
        uuid=str(uuid.uuid4()), machine_id=machine.id,
        kind="cpu" if cpu else "gpu", index=None if cpu else 0,
        vendor="AMD", model="Ryzen 5 3600", cores=6, threads=threads,
        pci_bus=None if cpu else "0000:05:00.0", present=True,
        enabled_for_mining=True, role="default_and_spec", boost_pool=True,
        default_token_id=profile.id, created_at=now, updated_at=now,
    )
    db.add(device); db.commit(); db.refresh(device)
    return machine, profile, device


def test_proportion_de_threads_cpu(db):
    from app import boost_runner as BR
    from app.ai_config import set_setting

    machine, _, device = _seed(db, threads=12, cpu=True)
    rows = [{"uuid": device.uuid, "machine_id": machine.id, "kind": "cpu",
             "index": None, "label": "Ryzen 5 3600"}]

    targets = BR.targets_from_rows(db, rows)
    assert [t.threads for t in targets] == [12]  # 100 % par défaut

    set_setting(db, "boost_cpu_thread_pct", 50)
    db.commit()
    targets = BR.targets_from_rows(db, rows)
    assert [t.threads for t in targets] == [6]


def _rule(db, enabled: bool):
    from app.models import Rule

    rule = db.get(Rule, "boost_launch")
    if rule is None:
        rule = Rule(id="boost_launch", enabled=enabled, params={})
        db.add(rule)
    else:
        rule.enabled = enabled
        rule.params = {}
    db.commit()


@pytest.mark.asyncio
async def test_post_proposal_boost_reste_propose(client, db):
    from app.main import app
    import app.routers.hermes as H
    from app.models import Boost

    app.dependency_overrides[H.require_hermes_token] = lambda: "hermes"
    machine, profile, device = _seed(db)
    start = (_now() + timedelta(minutes=5)).isoformat()
    end = (_now() + timedelta(hours=6)).isoformat()
    payload = {
        "coin": "PRL", "profile_id": profile.id, "device_uuids": [device.uuid],
        "start_at": start, "end_at": end, "execution_mode": "host",
        "note": "veille : candidat retenu", "candidate": {"age_hours": 2,
        "network_share_pct": 0.1, "kind": "gpu"},
        "boost": {"scope": "pool", "duration_h": 6, "proportion": 100,
                  "facts": {"verdict": "retenu"}},
    }
    try:
        _rule(db, False)
        r = await client.post("/api/hermes/proposals", json=payload)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "proposed"
        assert body["boost"] is None or body["boost"]["created"] is False
        assert db.query(Boost).count() == 0

        from app.models import Device

        second = Device(
            uuid=str(uuid.uuid4()), machine_id=machine.id, kind="gpu", index=1,
            vendor="nvidia", model="NVIDIA GeForce RTX 4080", vram_mb=16384,
            pci_bus="0000:06:00.0", present=True, enabled_for_mining=True,
            role="default_and_spec", boost_pool=True,
            default_token_id=profile.id, created_at=_now().isoformat(),
            updated_at=_now().isoformat(),
        )
        db.add(second); db.commit(); db.refresh(second)
        payload2 = {**payload, "device_uuids": [second.uuid]}
        _rule(db, True)
        r2 = await client.post("/api/hermes/proposals", json=payload2)
        assert r2.status_code == 201, r2.text
        body2 = r2.json()
        assert body2["boost"] is not None and body2["boost"]["created"] is True
        assert body2["boost"]["status"] == "proposed"
        boost = db.query(Boost).filter(Boost.id == body2["boost"]["id"]).first()
        assert boost is not None and boost.status == "proposed"
    finally:
        app.dependency_overrides.pop(H.require_hermes_token, None)
