import os
import sys
import datetime
import time as _time_module

import pytest
from httpx import AsyncClient, ASGITransport

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    """Set up isolated test environment with tmp_path for DB and signing key."""
    # Isolated data dir for signing key
    monkeypatch.setenv("FOSSOR_DATA_DIR", str(tmp_path))
    # Isolated DB in tmp_path
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test_fossor.db")
    # Master key for secrets
    monkeypatch.setenv("FOSSOR_MASTER_KEY", "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v=")
    # Admin password for login tests
    monkeypatch.setenv("FOSSOR_ADMIN_PASSWORD", "admin")
    # Reset cached globals so each test gets fresh state
    import app.auth
    import app.database
    app.auth.SERIALIZER = None
    app.auth.ADMIN_PASSWORD_HASH = None  # type: ignore
    app.database._lazy._engine = None
    app.database._lazy._session_factory = None
    app.database._db_url = None
    # Clear cached fernet
    try:
        import app.secrets
        app.secrets._fernet = None
    except Exception:
        pass
    # Import models AFTER resetting so Base includes User
    from app.models import User, Machine, AuditLog
    # Create tables (startup event doesn't fire with ASGITransport)
    app.database.init_db()
    yield
    # Clean up: drop all tables so subsequent tests in same class start fresh
    app.database.Base.metadata.drop_all(bind=app.database.engine)


@pytest.fixture
def unauth_client():
    """Client for tests that need unauthenticated access."""
    from app.main import app as application
    transport = ASGITransport(app=application)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def client():
    """Authenticated client — auto-logs in as admin."""
    from app.main import app as application
    transport = ASGITransport(app=application)
    ac = AsyncClient(transport=transport, base_url="http://test")
    r = await ac.post("/api/login", data={
        "username": "admin",
        "password": "admin",
    })
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    ac.cookies.set("fossor_session", r.cookies.get("fossor_session", ""), domain="test")
    yield ac


class TestVersionEndpoint:
    async def test_version_returns_file_content(self, client):
        r = await client.get("/api/version")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert data["version"] == "2026.11.002-dev"


class TestHealthEndpoint:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestLoginRequired:
    """Test that protected endpoints require authentication."""

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
        """Forge a cookie using a known wrong key — must get 401."""
        from itsdangerous import URLSafeTimedSerializer
        import json

        forged_key = "forged-test-key-not-a-secret"
        payload = {"sub": "admin"}
        forged = URLSafeTimedSerializer(forged_key).dumps(payload)

        r = await unauth_client.get("/api/machines", cookies={"fossor_session": forged})
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"

    async def test_forged_cookie_with_single_char_key(self, unauth_client):
        """Forge a cookie with a single-char key — must get 401."""
        from itsdangerous import URLSafeTimedSerializer
        import json

        forged_key = "x"
        payload = {"sub": "admin"}
        forged = URLSafeTimedSerializer(forged_key).dumps(payload)

        r = await unauth_client.get("/api/machines", cookies={"fossor_session": forged})
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"


class TestExpiredToken:
    """Verify that an expired token is rejected (401).

    itsdangerous TimestampSigner uses integer seconds for timestamps.
    We patch time.time() to return a value far in the future AFTER creating
    the token — so the token's timestamp is in the past relative to the new
    time, and loads() with max_age raises SignatureExpired.
    """

    async def test_expired_token_rejected(self, unauth_client):
        import app.auth as auth_mod

        # Step 1: Get the real serializer and create a valid token (time ≈ now)
        s = auth_mod._load_or_create_serializer()
        token = s.dumps({"sub": "admin"})

        # Step 2: Save original time.time() value, then patch it.
        # Must capture the original function before patching time module.
        _orig_time = _time_module.time
        _time_module.time = lambda: _orig_time() + 10000

        try:
            # Step 3: Reset serializer so endpoint creates a fresh one using patched time
            auth_mod.SERIALIZER = None

            # Step 4: Endpoint calls get_current_user() → s.loads(token, max_age=3600)
            # Token was signed at t=now, but now time.time() returns now+10000,
            # so the token appears 10000s old — well beyond max_age=3600s.
            r = await unauth_client.get("/api/machines", cookies={"fossor_session": token})
            assert r.status_code == 401, f"Expected 401 for expired token, got {r.status_code}: {r.text}"
        finally:
            _time_module.time = _orig_time
            auth_mod.SERIALIZER = None
