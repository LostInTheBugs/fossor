"""Config export / import (§4.13): round-trip, secrets, dry run, backup, validation."""

import json
import os
from pathlib import Path

import pytest

MACHINE_UUID = "11111111-1111-4111-8111-111111111111"
CPU_UUID = "22222222-2222-4222-8222-222222222222"
GPU_UUID = "33333333-3333-4333-8333-333333333333"
SECRET = "s3cr3t-pass-ne-doit-pas-fuir"
PASSPHRASE = "phrase-de-passe-de-test"

MACHINE_KEYS = (
    "uuid", "name", "host", "ssh_port", "ssh_user", "auth_type", "sudo",
    "os_kind", "host_key", "usage", "vm_host", "enabled",
)
DEVICE_KEYS = (
    "uuid", "machine_uuid", "kind", "index", "vendor", "model", "vram_mb",
    "pci_bus", "driver", "cores", "threads", "ram_mb", "flags", "nv_uuid",
    "present", "enabled_for_mining", "integrated",
)


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def seed(db):
    from app.models import AppSetting, Device, Machine
    from app.secrets import encrypt_secret

    machine = Machine(
        uuid=MACHINE_UUID,
        name="rig-alpha",
        host="192.0.2.60",
        ssh_port=2222,
        ssh_user="miner",
        auth_type="password",
        sudo="none",
        os_kind="hiveos",
        usage="both",
        vm_host=False,
        enabled=True,
        secret_ref=encrypt_secret(SECRET),
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    db.add(Device(uuid=CPU_UUID, machine_id=machine.id, kind="cpu", index=0,
                  vendor="AMD", model="Ryzen 9 5900X", cores=12, threads=24,
                  ram_mb=32768, present=True, enabled_for_mining=True, integrated=False))
    db.add(Device(uuid=GPU_UUID, machine_id=machine.id, kind="gpu", index=0,
                  vendor="NVIDIA", model="GeForce RTX 3080", vram_mb=10240,
                  pci_bus="01:00.0", driver="535.104", present=True,
                  enabled_for_mining=False, integrated=False))
    db.add(AppSetting(key="simulation_mode", value=json.dumps(True)))
    db.commit()
    return machine


def wipe(db):
    from app.models import AppSetting, Device, Machine

    db.query(Device).delete()
    db.query(Machine).delete()
    db.query(AppSetting).delete()
    db.commit()


def business(doc: dict) -> dict:
    """Export payload without ids, dates and secret flags — what must round-trip."""
    return {
        "machines": sorted(
            ({key: row.get(key) for key in MACHINE_KEYS} for row in doc["machines"]),
            key=lambda row: row["uuid"],
        ),
        "devices": sorted(
            ({key: row.get(key) for key in DEVICE_KEYS} for row in doc["devices"]),
            key=lambda row: row["uuid"],
        ),
        "settings": doc["settings"],
    }


async def test_export_import_roundtrip_on_empty_db(client, db):
    from app.models import AppSetting, Device, Machine

    seed(db)
    response = await client.get("/api/config/export")
    assert response.status_code == 200
    exported = response.json()

    # §4.13: identifiers are uuids, no SQL id, no plaintext secret.
    assert exported["format_version"] == 1
    assert exported["app_version"]
    assert exported["exported_at"]
    assert exported["machines"][0]["uuid"] == MACHINE_UUID
    assert "id" not in exported["machines"][0]
    assert exported["machines"][0]["has_secret"] is True
    assert exported["secrets"] is None
    assert SECRET not in json.dumps(exported)

    wipe(db)
    assert db.query(Machine).count() == 0

    # dry run: differences computed, nothing written
    preview = await client.post("/api/config/import?dry_run=true", json=exported)
    assert preview.status_code == 200
    body = preview.json()
    assert body["dry_run"] is True
    assert len(body["diff"]["machines"]["added"]) == 1
    assert len(body["diff"]["devices"]["added"]) == 2
    assert body["diff"]["settings"]["added"] == ["simulation_mode"]
    assert db.query(Machine).count() == 0
    assert db.query(Device).count() == 0
    assert db.query(AppSetting).count() == 0
    assert not (Path(os.environ["FOSSOR_DATA_DIR"]) / "backups").exists()

    # real import: backup first, then apply
    applied = await client.post("/api/config/import?dry_run=false&mode=merge", json=exported)
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["dry_run"] is False
    assert result["stats"]["machines"]["created"] == 1
    backup = Path(result["backup"])
    assert backup.exists()
    assert backup.parent.name == "backups"
    assert backup.name.startswith("config-")
    assert json.loads(backup.read_text())["machines"] == []  # backup of the empty base

    again = (await client.get("/api/config/export")).json()
    assert business(again) == business(exported)
    # Secrets were not exported, so the secret is not restored.
    assert again["machines"][0]["has_secret"] is False


async def test_include_secrets_roundtrip_with_passphrase(client, db):
    from app.models import Machine
    from app.secrets import decrypt_secret

    seed(db)
    response = await client.post(
        "/api/config/export",
        json={"include_secrets": True, "passphrase": PASSPHRASE},
    )
    assert response.status_code == 200
    exported = response.json()
    assert exported["secrets"]["kdf"] == "pbkdf2-sha256"
    assert exported["secrets"]["iterations"] >= 200_000
    assert exported["secrets"]["salt"]
    assert SECRET not in json.dumps(exported)
    assert MACHINE_UUID in exported["secrets"]["tokens"]

    wipe(db)
    applied = await client.post(
        "/api/config/import?dry_run=false&mode=merge",
        json=exported,
        headers={"X-Fossor-Passphrase": PASSPHRASE},
    )
    assert applied.status_code == 200, applied.text
    machine = db.query(Machine).filter(Machine.uuid == MACHINE_UUID).first()
    assert machine is not None
    assert decrypt_secret(machine.secret_ref) == SECRET


async def test_wrong_passphrase_writes_nothing(client, db):
    from app.models import Machine

    seed(db)
    exported = (
        await client.post("/api/config/export",
                          json={"include_secrets": True, "passphrase": PASSPHRASE})
    ).json()
    wipe(db)

    response = await client.post(
        "/api/config/import?dry_run=false&mode=merge",
        json=exported,
        headers={"X-Fossor-Passphrase": "mauvaise-phrase"},
    )
    assert response.status_code == 400
    assert "Phrase de passe incorrecte" in response.json()["detail"]
    assert db.query(Machine).count() == 0
    assert not (Path(os.environ["FOSSOR_DATA_DIR"]) / "backups").exists()


async def test_backup_contains_the_config_before_import(client, db):
    seed(db)
    exported = (await client.get("/api/config/export")).json()

    await client.patch(f"/api/machines/1", json={"name": "renamed-before-import"})
    result = (await client.post("/api/config/import?dry_run=false&mode=merge", json=exported)).json()
    backup = json.loads(Path(result["backup"]).read_text())
    assert backup["machines"][0]["name"] == "renamed-before-import"


async def test_replace_mode_deletes_entries_absent_from_the_file(client, db):
    from app.models import Machine

    seed(db)
    exported = (await client.get("/api/config/export")).json()
    extra_uuid = "44444444-4444-4444-8444-444444444444"
    db.add(Machine(uuid=extra_uuid, name="extra", host="192.0.2.61"))
    db.commit()

    result = await client.post("/api/config/import?dry_run=false&mode=replace", json=exported)
    assert result.status_code == 200
    db.expire_all()
    assert db.query(Machine).filter(Machine.uuid == extra_uuid).first() is None
    assert db.query(Machine).filter(Machine.uuid == MACHINE_UUID).first() is not None


async def test_unknown_format_version_is_rejected(client, db):
    seed(db)
    exported = (await client.get("/api/config/export")).json()
    exported["format_version"] = 99
    response = await client.post("/api/config/import?dry_run=true", json=exported)
    assert response.status_code == 400
    assert "format_version inconnu" in response.json()["detail"]


async def test_missing_field_is_rejected_with_a_clear_message(client, db):
    seed(db)
    exported = (await client.get("/api/config/export")).json()
    del exported["machines"]
    response = await client.post("/api/config/import?dry_run=true", json=exported)
    assert response.status_code == 400
    assert "machines" in response.json()["detail"]


async def test_invalid_mode_is_rejected(client, db):
    seed(db)
    exported = (await client.get("/api/config/export")).json()
    response = await client.post("/api/config/import?dry_run=true&mode=ecraser", json=exported)
    assert response.status_code == 400
