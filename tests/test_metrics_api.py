"""API tests for GET /api/metrics/live and /api/metrics/history."""

from datetime import datetime, timedelta, timezone


def _iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def seed_live_data(usage="both"):
    """One machine, one GPU, one metric row and a machine_status row."""
    from app.database import SessionLocal
    from app.models import Device, Machine, MachineStatus, Metric

    db = SessionLocal()
    try:
        machine = Machine(name="rig-api", host="10.0.0.9", usage=usage, enabled=True)
        db.add(machine)
        db.commit()
        db.refresh(machine)

        device = Device(
            uuid="33333333-3333-4333-8333-333333333333",
            machine_id=machine.id,
            kind="gpu",
            index=0,
            model="GeForce RTX 4090",
            vendor="NVIDIA",
            pci_bus="01:00.0",
        )
        db.add(device)
        ts = _iso(-30)
        db.add(Metric(
            machine_uuid=machine.uuid,
            device_uuid=device.uuid,
            ts=ts,
            hashrate_hs=6.85e13,
            temp_c=42,
            fan_pct=60,
            power_w=120,
            load_pct=99,
        ))
        db.add(MachineStatus(
            machine_uuid=machine.uuid,
            ts=ts,
            online=True,
            miner="srbminer",
            algo="pearlhash",
            coin="PEARL",
            uptime_s=3600,
            accepted=10,
            rejected=0,
            api_port=21550,
            total_hashrate_hs=6.85e13,
            total_power_w=120,
        ))
        db.commit()
        return machine.uuid
    finally:
        db.close()


async def test_live_returns_machine_and_device_state(client):
    uuid = seed_live_data()
    response = await client.get("/api/metrics/live")
    assert response.status_code == 200

    entry = next(item for item in response.json() if item["uuid"] == uuid)
    assert entry["online"] is True
    assert entry["collected"] is True
    assert entry["miner"] == "srbminer"
    assert entry["coin"] == "PEARL"
    assert entry["api_port"] == 21550
    assert entry["uptime_s"] == 3600
    assert entry["total_hashrate_human"] == "68.5 TH/s"
    assert entry["age_s"] >= 0

    assert len(entry["devices"]) == 1
    device = entry["devices"][0]
    assert device["uuid"] == "33333333-3333-4333-8333-333333333333"
    assert device["kind"] == "gpu"
    assert device["hashrate_human"] == "68.5 TH/s"
    assert device["temp_c"] == 42
    assert device["fan_pct"] == 60
    assert device["power_w"] == 120
    assert device["load_pct"] == 99


async def test_live_flags_usage_none_as_not_collected(client):
    uuid = seed_live_data(usage="none")
    response = await client.get("/api/metrics/live")
    entry = next(item for item in response.json() if item["uuid"] == uuid)
    assert entry["collected"] is False


async def test_live_requires_login(unauth_client):
    response = await unauth_client.get("/api/metrics/live")
    assert response.status_code in (401, 403)


async def test_history_returns_points_for_one_machine(client):
    uuid = seed_live_data()
    response = await client.get(f"/api/metrics/history?machine={uuid}&hours=24")
    assert response.status_code == 200
    body = response.json()
    assert body["machine"] == uuid
    assert body["name"] == "rig-api"
    assert body["count"] == 1
    point = body["points"][0]
    assert point["device_uuid"] == "33333333-3333-4333-8333-333333333333"
    assert point["hashrate_hs"] == 6.85e13
    assert point["temp_c"] == 42


async def test_history_rejects_unknown_machine(client):
    response = await client.get("/api/metrics/history?machine=does-not-exist")
    assert response.status_code == 404


async def test_history_hours_is_bounded(client):
    uuid = seed_live_data()
    assert (await client.get(f"/api/metrics/history?machine={uuid}&hours=0")).status_code == 422
    assert (await client.get(f"/api/metrics/history?machine={uuid}&hours=999")).status_code == 422


async def test_history_excludes_points_outside_window(client):
    uuid = seed_live_data()
    from app.database import SessionLocal
    from app.models import Metric

    db = SessionLocal()
    try:
        db.add(Metric(
            machine_uuid=uuid,
            device_uuid=None,
            ts=_iso(-3 * 86400),
            hashrate_hs=1e13,
        ))
        db.commit()
    finally:
        db.close()

    body = (await client.get(f"/api/metrics/history?machine={uuid}&hours=24")).json()
    assert body["count"] == 1
