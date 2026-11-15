import os
import sys

# Set env vars BEFORE importing app modules
os.environ["FOSSOR_MASTER_KEY"] = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v="
os.environ["FOSSOR_ADMIN_PASSWORD"] = "admin"
os.environ["DATABASE_URL"] = "sqlite:///./data/test_fossor.db"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, engine
from app.models import Machine, AuditLog

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Create tables before each test, clear after."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def unauth_client():
    """Client for tests that need unauthenticated access."""
    from app.main import app as application
    transport = ASGITransport(app=application)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def client():
    from app.main import app as application
    transport = ASGITransport(app=application)
    ac = AsyncClient(transport=transport, base_url="http://test")
    # Auto-login for all tests
    r = await ac.post("/api/login", data={
        "username": "admin",
        "password": "admin",
    })
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    ac.cookies.set("fossor_session", r.cookies.get("fossor_session", ""), domain="test")
    return ac


class TestVersionEndpoint:
    async def test_version_returns_file_content(self, client):
        r = await client.get("/api/version")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert data["version"] == "2026.11.001"


class TestHealthEndpoint:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestLoginRequired:
    """Test that /api/machines requires authentication."""

    async def test_list_machines_requires_login(self, unauth_client):
        r = await unauth_client.get("/api/machines")
        assert r.status_code == 401

    async def test_create_machine_requires_login(self, unauth_client):
        r = await unauth_client.post(
            "/api/machines",
            json={
                "name": "test", "host": "10.0.0.1", "usage": "both",
                "auth_type": "key", "sudo": "none", "os_kind": "other",
            },
        )
        assert r.status_code == 401

    async def test_get_machine_requires_login(self, unauth_client):
        r = await unauth_client.get("/api/machines/1")
        assert r.status_code == 401

    async def test_update_machine_requires_login(self, unauth_client):
        r = await unauth_client.patch(
            "/api/machines/1",
            json={"name": "updated"},
        )
        assert r.status_code == 401

    async def test_delete_machine_requires_login(self, unauth_client):
        r = await unauth_client.delete("/api/machines/1")
        assert r.status_code == 401

    async def test_audit_log_requires_login(self, unauth_client):
        r = await unauth_client.get("/api/audit-log")
        assert r.status_code == 401


class TestMachineCRUD:
    """Test full machine CRUD cycle."""

    async def test_create_machine(self, client):
        r = await client.post(
            "/api/machines",
            json={
                "name": "rig-alpha",
                "host": "192.0.2.10",
                "ssh_port": 22,
                "ssh_user": "root",
                "auth_type": "key",
                "sudo": "none",
                "os_kind": "other",
                "usage": "both",
                "vm_host": False,
                "enabled": True,
                "secret": "ssh-rsa AAAAB3...test",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "rig-alpha"
        assert data["host"] == "192.0.2.10"
        assert data["ssh_port"] == 22
        assert data["auth_type"] == "key"
        assert data["has_secret"] is True
        assert data["ssh_user"] == "root"
        assert data["usage"] == "both"
        assert data["sudo"] == "none"
        assert data["os_kind"] == "other"
        assert data["vm_host"] is False
        assert data["enabled"] is True
        assert data["id"] is not None
        assert "secret" not in data
        assert "secret_ref" not in data
        assert data["has_secret"] is True

    async def test_create_machine_defaults(self, client):
        """Test that ssh_port defaults to 22."""
        r = await client.post(
            "/api/machines",
            json={
                "name": "rig-mini",
                "host": "10.0.0.5",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ssh_port"] == 22

    async def test_list_machines(self, client):
        for i in range(2):
            await client.post(
                "/api/machines",
                json={
                    "name": f"list-test-{i}",
                    "host": f"10.0.0.{10+i}",
                    "usage": "cpu",
                    "auth_type": "password",
                    "sudo": "sudo without password",
                    "os_kind": "ubuntu",
                },
            )
        r = await client.get("/api/machines")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        names = {m["name"] for m in data}
        assert names == {"list-test-0", "list-test-1"}

    async def test_get_machine(self, client):
        r = await client.post(
            "/api/machines",
            json={
                "name": "get-test",
                "host": "10.0.0.20",
                "usage": "gpu",
                "host_key": "SHA256:abc123",
            },
        )
        data = r.json()
        mid = data["id"]

        r2 = await client.get(f"/api/machines/{mid}")
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["name"] == "get-test"
        assert d2["usage"] == "gpu"
        assert d2["host_key"] == "SHA256:abc123"

    async def test_update_machine(self, client):
        r = await client.post(
            "/api/machines",
            json={"name": "update-target", "host": "10.0.0.30", "usage": "cpu"},
        )
        mid = r.json()["id"]

        r2 = await client.patch(
            f"/api/machines/{mid}",
            json={"usage": "gpu", "enabled": False},
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["usage"] == "gpu"
        assert d2["enabled"] is False

    async def test_update_machine_secret(self, client):
        r = await client.post(
            "/api/machines",
            json={"name": "secret-test", "host": "10.0.0.40", "secret": "old-secret"},
        )
        mid = r.json()["id"]
        assert r.json()["has_secret"] is True

        r2 = await client.patch(
            f"/api/machines/{mid}",
            json={"secret": "new-secret"},
        )
        assert r2.status_code == 200
        assert r2.json()["has_secret"] is True

        r3 = await client.patch(
            f"/api/machines/{mid}",
            json={"host": "10.0.0.41"},
        )
        assert r3.status_code == 200
        assert r3.json()["has_secret"] is True

    async def test_delete_machine(self, client):
        r = await client.post(
            "/api/machines",
            json={"name": "del-test", "host": "10.0.0.50"},
        )
        mid = r.json()["id"]

        r2 = await client.delete(f"/api/machines/{mid}")
        assert r2.status_code == 200

        r3 = await client.get("/api/machines")
        assert len(r3.json()) == 0

    async def test_get_nonexistent(self, client):
        r = await client.get("/api/machines/9999")
        assert r.status_code == 404

    async def test_delete_nonexistent(self, client):
        r = await client.delete("/api/machines/9999")
        assert r.status_code == 404


class TestNoSecretLeak:
    """Verify NO secret data is ever returned in any API response."""

    def _check_no_secret_keys(self, data, path=""):
        """Recursively check a dict/list for secret key leakage."""
        if isinstance(data, dict):
            dangerous_keys = {
                "secret_ref", "secret", "password", "key",
                "private_key", "ssh_key", "token",
            }
            for key in data:
                if key in dangerous_keys:
                    if key not in {"host_key", "secret_ref"}:
                        raise AssertionError(
                            f"Secret key '{key}' found in response at {path}"
                        )
                self._check_no_secret_keys(data[key], f"{path}.{key}")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                self._check_no_secret_keys(item, f"{path}[{i}]")

    async def _create_with_secret(self, client, name):
        r = await client.post("/api/machines", json={
            "name": name, "host": "10.0.0.99",
            "secret": "s3cr3t_p@ssw0rd_!!!"
        })
        assert r.status_code == 200
        return r.json()["id"]

    async def test_create_does_not_leak_secret(self, client):
        r = await client.post("/api/machines", json={
            "name": "leak-test-1", "host": "10.0.0.1",
            "secret": "super_secret_password_123",
        })
        self._check_no_secret_keys(r.json())

    async def test_list_does_not_leak_secret(self, client):
        await self._create_with_secret(client, "leak-test-2")
        r = await client.get("/api/machines")
        self._check_no_secret_keys(r.json())

    async def test_get_does_not_leak_secret(self, client):
        mid = await self._create_with_secret(client, "leak-test-3")
        r = await client.get(f"/api/machines/{mid}")
        self._check_no_secret_keys(r.json())

    async def test_update_does_not_leak_secret(self, client):
        mid = await self._create_with_secret(client, "leak-test-4")
        r = await client.patch(f"/api/machines/{mid}", json={"host": "10.0.0.2"})
        self._check_no_secret_keys(r.json())

    async def test_has_secret_flag_works(self, client):
        r1 = await client.post("/api/machines", json={
            "name": "with-sec", "host": "10.0.0.3", "secret": "test",
        })
        assert r1.json()["has_secret"] is True

        r2 = await client.post("/api/machines", json={
            "name": "no-sec", "host": "10.0.0.4",
        })
        assert r2.json()["has_secret"] is False

        r3 = await client.patch(f"/api/machines/{r1.json()['id']}", json={"host": "10.0.0.3"})
        assert r3.json()["has_secret"] is True


class TestForgedCookie:
    """Verify that a cookie signed with a wrong key is rejected (401)."""

    async def test_forged_cookie_with_wrong_key(self, unauth_client):
        """Forge a cookie using the old default key — must get 401."""
        from app.auth import _load_or_create_serializer
        from itsdangerous import URLSafeTimedSerializer
        import json, os, secrets

        # Save original serializer
        from app import auth
        orig_serializer = auth.SERIALIZER
        auth.SERIALIZER = None

        # Generate a key file with a known wrong key
        data_dir = auth._get_data_dir()
        key_file = data_dir / "fossor_signing_key"
        old_key = key_file.read_text().strip() if key_file.exists() else None

        try:
            # Write a known wrong key
            wrong_key = "forged-test-key-not-a-secret"
            key_file.write_text(wrong_key)
            os.chmod(key_file, 0o600)
            auth.SERIALIZER = None  # force reload

            # Forge a token with the wrong key
            wrong_s = URLSafeTimedSerializer(wrong_key)
            import datetime
            from datetime import datetime as dt, timedelta, timezone
            payload = {"sub": "admin", "exp": (dt.now(timezone.utc) + timedelta(minutes=60)).isoformat()}
            forged = wrong_s.dumps(json.dumps(payload))

            r = await unauth_client.get("/api/machines", cookies={"fossor_session": forged})
            assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"
        finally:
            # Restore original key
            auth.SERIALIZER = None
            if old_key:
                key_file.write_text(old_key)
            elif key_file.exists():
                key_file.unlink()
