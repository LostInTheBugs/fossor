"""§4.1 ter — type d'OS choisi à l'ajout, préréglages et écart de détection."""

import pytest

from app.os_presets import (
    HIVEOS_PASSWORD_HINT,
    OS_KINDS,
    apply_preset,
    compare_os,
    preset_fields,
    public_presets,
)


class _Machine:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestPresets:
    def test_hiveos_preset(self):
        fields = preset_fields("hiveos")
        assert fields["ssh_port"] == 22
        assert fields["auth_type"] == "provision_pending"
        assert fields["ssh_user"] == "fossor"
        preset = public_presets()["presets"]["hiveos"]
        assert preset["temp_user"] == "user"
        assert preset["collector"] == "hiveos"
        assert preset["external_miners"] is True
        assert "1" in HIVEOS_PASSWORD_HINT and "HiveOS" in HIVEOS_PASSWORD_HINT
        # Jamais de mot de passe dans le préréglage, seulement le texte d'aide.
        assert "password" not in fields and "secret" not in fields

    def test_ubuntu_and_proxmox_presets(self):
        assert preset_fields("ubuntu")["auth_type"] == "provision_pending"
        proxmox = preset_fields("proxmox")
        assert proxmox["ssh_user"] == "root"
        assert proxmox["vm_host"] is True
        assert proxmox["usage"] == "none"
        assert preset_fields("other") == {"os_kind": "other"}

    def test_apply_preset_changes_only_what_differs(self):
        machine = _Machine(ssh_port=2222, auth_type="password", ssh_user="root",
                           sudo="none", vm_host=False, usage="both", os_kind="other")
        changed = apply_preset(machine, "hiveos")
        assert machine.ssh_port == 22
        assert machine.auth_type == "provision_pending"
        assert machine.os_kind == "hiveos"
        assert "ssh_port" in changed and "os_kind" in changed


class TestCompare:
    def test_same_os_has_no_banner(self):
        result = compare_os("hiveos", "hiveos")
        assert result["mismatch"] is False and result["banner"] == ""

    def test_mismatch_banner_names_both_os(self):
        result = compare_os("hiveos", "ubuntu")
        assert result["mismatch"] is True
        assert result["banner"] == "OS détecté : Ubuntu/Debian (choisi : HiveOS)"
        assert result["preset_label"] == "Ubuntu/Debian"

    def test_unknown_detection_has_no_banner(self):
        assert compare_os("ubuntu", "other")["mismatch"] is False
        assert compare_os("ubuntu", None)["mismatch"] is False


class TestApi:
    async def _create(self, client, name, **payload):
        body = {"name": name, "host": "192.0.2.77", "ssh_user": "root"}
        body.update(payload)
        response = await client.post("/api/machines", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    async def test_presets_endpoint_exposes_no_password(self, client):
        response = await client.get("/api/machines/os-presets")
        assert response.status_code == 200
        data = response.json()
        assert data["kinds"] == list(OS_KINDS)
        assert data["presets"]["hiveos"]["secret_hint"] == HIVEOS_PASSWORD_HINT
        assert "password" not in data["presets"]["hiveos"]

    async def test_detection_mismatch_then_apply_preset(self, client):
        machine = await self._create(client, "os-mismatch", os_kind="hiveos")
        assert machine["os_kind"] == "hiveos"
        assert machine["os_mismatch"] is False

        from app.database import SessionLocal
        from app.models import Machine

        db = SessionLocal()
        try:
            row = db.query(Machine).filter(Machine.id == machine["id"]).one()
            row.os_kind_detected = "ubuntu"  # ce qu'écrit la détection
            db.commit()
        finally:
            db.close()

        refreshed = (await client.get(f"/api/machines/{machine['id']}")).json()
        assert refreshed["os_kind"] == "hiveos"
        assert refreshed["os_kind_detected"] == "ubuntu"
        assert refreshed["os_mismatch"] is True
        assert refreshed["os_banner"] == "OS détecté : Ubuntu/Debian (choisi : HiveOS)"
        assert refreshed["os_preset_label"] == "Ubuntu/Debian"

        applied = await client.post(
            f"/api/machines/{machine['id']}/os-preset", json={"os_kind": "ubuntu"}
        )
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["os_kind"] == "ubuntu"
        assert body["os_mismatch"] is False
        assert body["ssh_port"] == 22
        assert body["auth_type"] == "provision_pending"
        assert "os_kind" in body["changed"]

    async def test_unknown_os_kind_is_refused(self, client):
        machine = await self._create(client, "os-bad")
        response = await client.post(
            f"/api/machines/{machine['id']}/os-preset", json={"os_kind": "windows"}
        )
        assert response.status_code == 400

    async def test_existing_machine_gets_detected_type_at_migration(self, client):
        machine = await self._create(client, "os-legacy", os_kind="hiveos")

        from app.database import SessionLocal, _migrate_machine_os_detected_column
        from app.models import Machine

        db = SessionLocal()
        try:
            row = db.query(Machine).filter(Machine.id == machine["id"]).one()
            row.os_kind_detected = None  # machine créée avant le champ
            db.commit()
        finally:
            db.close()

        _migrate_machine_os_detected_column()

        db = SessionLocal()
        try:
            row = db.query(Machine).filter(Machine.id == machine["id"]).one()
            assert row.os_kind_detected == "hiveos"
        finally:
            db.close()


@pytest.mark.parametrize("kind", list(OS_KINDS))
def test_every_kind_has_a_label(kind):
    assert public_presets()["labels"][kind]
