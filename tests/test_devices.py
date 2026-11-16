"""Device table: idempotent re-detection (§4.2) and the device API.

Imports of ``app.database`` / ``app.models`` are deliberately *inside* the
fixtures: the test environment (DATABASE_URL) is set per test by conftest, and
importing the engine at collection time would bind it to the default path.
"""

import uuid as uuid_lib
from pathlib import Path

import pytest

from app.hw_parser import parse_detect_hw

FIXTURES = Path(__file__).parent / "fixtures"


def detect(name: str) -> dict:
    return parse_detect_hw((FIXTURES / name).read_text())


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def machine(db):
    from app.models import Machine

    row = Machine(name="rig-test", host="192.0.2.10", ssh_port=22, ssh_user="root")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def devices_of(db, machine):
    from app.models import Device

    return (
        db.query(Device)
        .filter(Device.machine_id == machine.id)
        .order_by(Device.kind.asc(), Device.index.asc())
        .all()
    )


def apply(db, machine, name: str):
    from app.devices import apply_detection

    return apply_detection(db, machine, detect(name))


class TestApplyDetection:
    def test_first_detection_creates_cpu_and_gpus(self, db, machine):
        stats = apply(db, machine, "detect_hw_hiveos_rig.txt")
        assert stats == {"created": 6, "updated": 0, "missing": 0, "device_count": 6}

        rows = devices_of(db, machine)
        assert [d.kind for d in rows].count("cpu") == 1
        assert [d.kind for d in rows].count("gpu") == 5
        assert all(d.present for d in rows)
        assert all(d.enabled_for_mining for d in rows)

        cpu = rows[0]
        assert cpu.model == "Intel(R) Core(TM) i5-4570 CPU @ 3.20GHz"
        assert (cpu.cores, cpu.threads, cpu.ram_mb) == (4, 4, 16000)
        assert cpu.flags == "aes,avx2"

        gpus = [d for d in rows if d.kind == "gpu"]
        assert [g.model for g in gpus] == ["NVIDIA GeForce RTX 3070"] * 4 + ["NVIDIA GeForce RTX 3080"]
        assert [g.vram_mb for g in gpus] == [8192, 8192, 8192, 8192, 10240]
        assert [g.pci_bus for g in gpus] == ["01:00.0", "02:00.0", "03:00.0", "04:00.0", "05:00.0"]
        assert all(g.driver == "535.113.01" for g in gpus)

    def test_device_uuids_are_uuid4(self, db, machine):
        apply(db, machine, "detect_hw_hiveos_rig.txt")
        for device in devices_of(db, machine):
            parsed = uuid_lib.UUID(device.uuid)
            assert parsed.version == 4
            assert str(parsed) == device.uuid

    def test_second_detection_keeps_uuid_and_count(self, db, machine):
        apply(db, machine, "detect_hw_hiveos_rig.txt")
        before = {d.uuid: d.pci_bus for d in devices_of(db, machine)}

        stats = apply(db, machine, "detect_hw_hiveos_rig.txt")
        assert stats["created"] == 0
        assert stats["updated"] == 6
        assert stats["missing"] == 0

        after = {d.uuid: d.pci_bus for d in devices_of(db, machine)}
        assert after == before
        assert len(after) == 6

    def test_removed_card_is_flagged_absent_not_deleted(self, db, machine):
        from app.devices import apply_detection

        detected = detect("detect_hw_hiveos_rig.txt")
        apply_detection(db, machine, detected)

        shrunk = dict(detected)
        shrunk["gpus"] = [g for g in detected["gpus"] if g["pci_bus"] != "03:00.0"]
        apply_detection(db, machine, shrunk)

        rows = devices_of(db, machine)
        assert len(rows) == 6  # nothing deleted
        gone = [d for d in rows if d.pci_bus == "03:00.0"][0]
        assert gone.present is False
        assert gone.model == "NVIDIA GeForce RTX 3070"
        assert sum(1 for d in rows if d.present) == 5

    def test_enabled_choice_survives_redetection(self, db, machine):
        from app.models import Device

        apply(db, machine, "detect_hw_hiveos_rig.txt")
        cpu = next(d for d in devices_of(db, machine) if d.kind == "cpu")
        gpu = next(d for d in devices_of(db, machine) if d.pci_bus == "02:00.0")
        cpu.enabled_for_mining = False
        gpu.enabled_for_mining = False
        db.commit()
        cpu_uuid, gpu_uuid = cpu.uuid, gpu.uuid

        apply(db, machine, "detect_hw_hiveos_rig.txt")

        cpu = db.query(Device).filter(Device.uuid == cpu_uuid).one()
        gpu = db.query(Device).filter(Device.uuid == gpu_uuid).one()
        assert cpu.enabled_for_mining is False
        assert gpu.enabled_for_mining is False
        assert cpu.present is True and gpu.present is True

    def test_pci_bus_notation_does_not_duplicate(self, db, machine):
        from app.devices import apply_detection

        detected = detect("detect_hw_hiveos_rig.txt")
        apply_detection(db, machine, detected)

        padded = dict(detected)
        padded["gpus"] = [
            {**gpu, "pci_bus": f"0000:{gpu['pci_bus']}"} for gpu in detected["gpus"]
        ]
        stats = apply_detection(db, machine, padded)
        assert stats["created"] == 0
        assert len(devices_of(db, machine)) == 6

    def test_vm_host_and_os_kind_follow_detection(self, db, machine):
        assert machine.vm_host is False
        apply(db, machine, "detect_hw_ubuntu_kvm.txt")
        db.refresh(machine)
        assert machine.vm_host is True
        assert machine.os_kind == "ubuntu"

    def test_machine_without_gpu_keeps_cpu_only(self, db, machine):
        apply(db, machine, "detect_hw_no_gpu.txt")
        rows = devices_of(db, machine)
        assert len(rows) == 1
        assert rows[0].kind == "cpu"

    def test_amd_card_without_vram_is_stored(self, db, machine):
        apply(db, machine, "detect_hw_amd_lspci.txt")
        gpus = [d for d in devices_of(db, machine) if d.kind == "gpu"]
        assert [(g.vendor, g.model, g.vram_mb) for g in gpus] == [
            ("AMD", "Radeon RX 6700/6700 XT/6750 XT / 6800M", None),
            ("Intel", "UHD Graphics 630", None),
        ]


class TestDeviceApi:
    async def _create_machine(self, client) -> int:
        r = await client.post("/api/machines", json={
            "name": "jetable", "host": "192.0.2.10", "ssh_user": "root",
        })
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def _fake_detect(self, fixture: str):
        payload = detect(fixture)

        async def fake_detect_hw(machine):
            return {
                "ok": True,
                "latency_ms": 1.0,
                "host_key_fingerprint": "SHA256:fake",
                **payload,
            }

        return fake_detect_hw

    async def test_detect_then_list_devices(self, client, monkeypatch):
        machine_id = await self._create_machine(client)
        monkeypatch.setattr("app.ssh_client.ssh_detect_hw", self._fake_detect("detect_hw_ubuntu_kvm.txt"))

        r = await client.post(f"/api/machines/{machine_id}/detect")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["vm_host"] is True
        assert body["os_kind"] == "ubuntu"
        assert body["stats"]["created"] == 2
        assert [d["kind"] for d in body["devices"]] == ["cpu", "gpu"]

        r = await client.get(f"/api/machines/{machine_id}/devices")
        assert r.status_code == 200
        devices = r.json()
        assert len(devices) == 2
        gpu = [d for d in devices if d["kind"] == "gpu"][0]
        assert gpu["model"] == "NVIDIA GeForce RTX 4080"
        assert gpu["vram_mb"] == 16376
        assert gpu["pci_bus"] == "0b:00.0"
        assert gpu["enabled_for_mining"] is True
        assert gpu["present"] is True

    async def test_detect_is_idempotent_through_api(self, client, monkeypatch):
        machine_id = await self._create_machine(client)
        monkeypatch.setattr("app.ssh_client.ssh_detect_hw", self._fake_detect("detect_hw_hiveos_rig.txt"))

        first = (await client.post(f"/api/machines/{machine_id}/detect")).json()
        second = (await client.post(f"/api/machines/{machine_id}/detect")).json()
        assert len(first["devices"]) == len(second["devices"]) == 6
        assert [d["uuid"] for d in first["devices"]] == [d["uuid"] for d in second["devices"]]
        assert second["stats"]["created"] == 0

    async def test_detect_refuses_changed_fingerprint(self, client, monkeypatch):
        machine_id = await self._create_machine(client)

        async def fake_refused(machine):
            return {
                "ok": False,
                "error": "Empreinte changée : ancienne SHA256:aaa, nouvelle SHA256:bbb",
                "host_key_fingerprint": "SHA256:bbb",
            }

        monkeypatch.setattr("app.ssh_client.ssh_detect_hw", fake_refused)
        r = await client.post(f"/api/machines/{machine_id}/detect")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["error"].startswith("Empreinte changée")

        devices = (await client.get(f"/api/machines/{machine_id}/devices")).json()
        assert devices == []

    async def test_patch_device_switch_and_audit(self, client, monkeypatch):
        machine_id = await self._create_machine(client)
        monkeypatch.setattr("app.ssh_client.ssh_detect_hw", self._fake_detect("detect_hw_hiveos_rig.txt"))
        devices = (await client.post(f"/api/machines/{machine_id}/detect")).json()["devices"]
        gpu = [d for d in devices if d["kind"] == "gpu"][0]

        r = await client.patch(f"/api/devices/{gpu['id']}", json={"enabled_for_mining": False})
        assert r.status_code == 200, r.text
        assert r.json()["enabled_for_mining"] is False
        assert r.json()["uuid"] == gpu["uuid"]

        # Persisted, not just echoed
        devices = (await client.get(f"/api/machines/{machine_id}/devices")).json()
        assert [d for d in devices if d["id"] == gpu["id"]][0]["enabled_for_mining"] is False

        r = await client.get("/api/audit-log")
        actions = [(entry["action"], entry["result"]) for entry in r.json()]
        assert ("detect", "ok") in actions
        assert ("device-update", "ok") in actions

    async def test_patch_device_requires_enabled_field(self, client, monkeypatch):
        machine_id = await self._create_machine(client)
        monkeypatch.setattr("app.ssh_client.ssh_detect_hw", self._fake_detect("detect_hw_no_gpu.txt"))
        devices = (await client.post(f"/api/machines/{machine_id}/detect")).json()["devices"]
        r = await client.patch(f"/api/devices/{devices[0]['id']}", json={"model": "x"})
        assert r.status_code == 400
        assert r.json()["detail"] == "enabled_for_mining is required"

    async def test_patch_unknown_device(self, client):
        r = await client.patch("/api/devices/9999", json={"enabled_for_mining": True})
        assert r.status_code == 404
        assert r.json()["detail"] == "Device not found"

    async def test_devices_require_login(self, unauth_client):
        r = await unauth_client.get("/api/machines/1/devices")
        assert r.status_code == 401
        r = await unauth_client.patch("/api/devices/1", json={"enabled_for_mining": True})
        assert r.status_code == 401
