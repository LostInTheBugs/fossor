"""API validation of machines: host/name/port, and audit actor (§7.8-7.9)."""

from __future__ import annotations

import asyncssh
import pytest


BASE = {"name": "jetable", "host": "192.0.2.10", "ssh_port": 22, "ssh_user": "root"}


class TestMachineValidation:
    @pytest.mark.asyncio
    async def test_empty_host_rejected_422_french(self, client):
        response = await client.post("/api/machines", json={**BASE, "host": ""})
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "Hôte" in detail and "obligatoire" in detail

    @pytest.mark.asyncio
    async def test_whitespace_host_rejected(self, client):
        response = await client.post("/api/machines", json={**BASE, "host": "   "})
        assert response.status_code == 422
        assert "obligatoire" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_host_rejected(self, client):
        response = await client.post("/api/machines", json={**BASE, "host": "rig..7095FA"})
        assert response.status_code == 422
        assert "adresse invalide" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_host_rejected(self, client):
        payload = {k: v for k, v in BASE.items() if k != "host"}
        response = await client.post("/api/machines", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_name_rejected(self, client):
        response = await client.post("/api/machines", json={**BASE, "name": " "})
        assert response.status_code == 422
        assert "Nom" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_port_out_of_range_rejected(self, client):
        for port in (0, 65536, -1, "abc"):
            response = await client.post("/api/machines", json={**BASE, "ssh_port": port})
            assert response.status_code == 422, (port, response.text)
            assert "Port SSH" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_valid_ipv4_ipv6_and_dns_accepted(self, client):
        for index, host in enumerate(("192.0.2.11", "2001:db8::42", "rig.exemple.net")):
            response = await client.post(
                "/api/machines", json={**BASE, "name": f"valide-{index}", "host": host}
            )
            assert response.status_code == 200, (host, response.text)
            assert response.json()["host"] == host

    @pytest.mark.asyncio
    async def test_patch_empty_host_rejected(self, client):
        created = await client.post("/api/machines", json=BASE)
        machine_id = created.json()["id"]
        response = await client.patch(f"/api/machines/{machine_id}", json={"host": ""})
        assert response.status_code == 422
        assert "obligatoire" in response.json()["detail"]


class TestProvisionPending:
    @pytest.mark.asyncio
    async def test_test_and_detect_refuse_without_connection(self, client, monkeypatch):
        created = await client.post(
            "/api/machines",
            json={**BASE, "name": "a-preparer", "auth_type": "provision_pending"},
        )
        assert created.status_code == 200, created.text
        machine_id = created.json()["id"]

        async def forbidden(*args, **kwargs):
            raise AssertionError("aucune connexion SSH ne doit être tentée")

        monkeypatch.setattr("app.ssh_client.read_server_host_key", forbidden)

        tested = (await client.post(f"/api/machines/{machine_id}/test")).json()
        assert tested["ok"] is False
        assert "pas encore été préparée" in tested["error"]

        detected = (await client.post(f"/api/machines/{machine_id}/detect")).json()
        assert detected["ok"] is False
        assert "pas encore été préparée" in detected["error"]


class TestAudit:
    @pytest.mark.asyncio
    async def test_host_key_pinned_once_and_actor_is_user(self, client, monkeypatch):
        """A creation pins (and audits) the host key exactly once, as the user."""
        created = await client.post(
            "/api/machines",
            json={**BASE, "name": "pin-once", "auth_type": "password", "secret": "secret"},
        )
        assert created.status_code == 200, created.text
        machine_id = created.json()["id"]

        server_key = asyncssh.generate_private_key("ssh-ed25519")

        async def fake_read(host, port):
            return server_key

        monkeypatch.setattr("app.ssh_client.read_server_host_key", fake_read)

        from app.ssh_client import _resolve_host_key

        for _ in range(2):  # second call must see the pin written by the first
            _conn, _fp, failure = await _resolve_host_key(
                host="192.0.2.10",
                port=22,
                pinned_fp=None,
                pinned_public_key=None,
                machine_id=machine_id,
                audit_kind="ssh-test",
                start=0.0,
                actor="admin",
            )
            assert failure is None

        from app.database import SessionLocal
        from app.models import AuditLog

        db = SessionLocal()
        try:
            rows = (
                db.query(AuditLog)
                .filter(AuditLog.machine_id == machine_id)
                .filter(AuditLog.action == "host-key-pinned")
                .all()
            )
        finally:
            db.close()

        assert len(rows) == 1, [(row.action, row.actor) for row in rows]
        assert rows[0].actor == "admin"
        assert rows[0].actor != "hermes"
