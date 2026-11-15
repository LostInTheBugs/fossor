import os
import sys
import datetime
import time as _time_module

import pytest
from httpx import AsyncClient, ASGITransport

pytest_plugins = ("pytest_asyncio",)


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
