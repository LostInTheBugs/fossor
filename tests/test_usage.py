"""Machine usage follows its devices (§4.2), on PATCH and on detect."""

import uuid as uuid_lib
from pathlib import Path

import pytest

from app.hw_parser import parse_detect_hw

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_machine(db, cpu=True, gpu=True):
    from app.models import Device, Machine

    machine = Machine(name=f"usage-{uuid_lib.uuid4().hex[:6]}", host="192.0.2.40", usage="both")
    db.add(machine)
    db.commit()
    db.refresh(machine)
    rows = []
    if cpu:
        rows.append(Device(uuid=str(uuid_lib.uuid4()), machine_id=machine.id, kind="cpu",
                           model="Test CPU", index=0, present=True, enabled_for_mining=True))
    if gpu:
        rows.append(Device(uuid=str(uuid_lib.uuid4()), machine_id=machine.id, kind="gpu",
                           model="Test GPU", index=0, present=True, enabled_for_mining=True))
    for row in rows:
        db.add(row)
    db.commit()
    return machine, rows


async def patch_device(client, device_id, enabled):
    return await client.patch(f"/api/devices/{device_id}", json={"enabled_for_mining": enabled})


async def machine_usage(client, machine_id):
    response = await client.get(f"/api/machines/{machine_id}")
    assert response.status_code == 200
    return response.json()["usage"]


async def test_both_when_cpu_and_gpu_enabled(client, db):
    machine, _ = make_machine(db, cpu=True, gpu=True)
    assert await machine_usage(client, machine.id) == "both"


async def test_cpu_only_after_unchecking_gpu(client, db):
    machine, rows = make_machine(db, cpu=True, gpu=True)
    gpu = next(row for row in rows if row.kind == "gpu")
    response = await patch_device(client, gpu.id, False)
    assert response.status_code == 200
    assert await machine_usage(client, machine.id) == "cpu"


async def test_gpu_only_after_unchecking_cpu(client, db):
    machine, rows = make_machine(db, cpu=True, gpu=True)
    cpu = next(row for row in rows if row.kind == "cpu")
    assert (await patch_device(client, cpu.id, False)).status_code == 200
    assert await machine_usage(client, machine.id) == "gpu"


async def test_none_when_everything_unchecked(client, db):
    machine, rows = make_machine(db, cpu=True, gpu=True)
    for row in rows:
        assert (await patch_device(client, row.id, False)).status_code == 200
    assert await machine_usage(client, machine.id) == "none"


async def test_machine_without_gpu_unchecking_its_only_cpu_gives_none(client, db):
    """(a) The reported bug: no GPU, the only CPU unchecked — usage stayed ``both``."""
    machine, rows = make_machine(db, cpu=True, gpu=False)
    # Stored usage is stale until a PATCH (that is precisely the bug):
    assert await machine_usage(client, machine.id) == "both"
    assert (await patch_device(client, rows[0].id, False)).status_code == 200
    assert await machine_usage(client, machine.id) == "none"


def test_compute_usage_ignores_absent_devices(db):
    from app.devices import compute_usage
    from app.models import Device, Machine

    machine = Machine(name="absent", host="192.0.2.41", usage="both")
    db.add(machine)
    db.commit()
    db.refresh(machine)
    db.add(Device(uuid=str(uuid_lib.uuid4()), machine_id=machine.id, kind="gpu",
                  model="Gone", index=0, present=False, enabled_for_mining=True))
    db.commit()
    assert compute_usage(db, machine) == "none"


def test_detection_recomputes_usage(db):
    """Detect is the second place where usage must be recalculated."""
    from app.devices import apply_detection
    from app.models import Machine

    machine = Machine(name="detect-usage", host="192.0.2.42", usage="none")
    db.add(machine)
    db.commit()
    db.refresh(machine)
    detected = parse_detect_hw((FIXTURES / "detect_hw_no_gpu.txt").read_text())
    apply_detection(db, machine, detected)
    db.refresh(machine)
    assert machine.usage == "cpu"
