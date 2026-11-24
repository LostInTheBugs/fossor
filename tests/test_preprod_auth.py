"""Préprod — authentification désactivable sur les réseaux de confiance."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app import auth_settings
from app.database import SessionLocal
from app.models import AuditLog, Machine


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configure(**values) -> None:
    db = SessionLocal()
    try:
        auth_settings.write_settings(db, values)
    finally:
        db.close()


def _asic() -> int:
    db = SessionLocal()
    try:
        row = Machine(name=f"asic-{uuid.uuid4().hex[:6]}", host="192.0.2.170", machine_kind="asic")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _client(ip: str) -> AsyncClient:
    from app.main import app as application

    return AsyncClient(
        transport=ASGITransport(app=application, client=(ip, 1234)),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_trusted_network_is_admin_without_cookie():
    _configure(auth_enabled=False, trusted_networks=["127.0.0.1"])
    async with _client("127.0.0.1") as ac:
        response = await ac.get("/api/machines")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_outside_network_still_requires_login():
    _configure(auth_enabled=False, trusted_networks=["127.0.0.1"])
    async with _client("8.8.8.8") as ac:
        refused = await ac.get("/api/machines")
        login = await ac.post("/api/login", data={"username": "admin", "password": "admin"})
    assert refused.status_code == 401
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_x_forwarded_for_is_ignored_until_a_proxy_is_declared():
    _configure(auth_enabled=False, trusted_networks=["10.0.0.0/8"], trust_proxy=False)
    headers = {"X-Forwarded-For": "10.1.2.3"}
    async with _client("8.8.8.8") as ac:
        spooofed = await ac.get("/api/machines", headers=headers)
    assert spooofed.status_code == 401

    _configure(auth_enabled=False, trusted_networks=["10.0.0.0/8"], trust_proxy=True)
    async with _client("8.8.8.8") as ac:
        proxied = await ac.get("/api/machines", headers=headers)
    assert proxied.status_code == 200


@pytest.mark.asyncio
async def test_hermes_bearer_never_gets_admin_even_on_trusted_network():
    _configure(auth_enabled=False, trusted_networks=["127.0.0.1"])
    async with _client("127.0.0.1") as ac:
        response = await ac.get("/api/machines", headers={"Authorization": "Bearer faux-jeton"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_auth_settings_change_is_audited(client):
    response = await client.put(
        "/api/security/settings",
        json={"auth_enabled": False, "trusted_networks": ["127.0.0.1", "192.168.0.0/16"]},
    )
    assert response.status_code == 200
    db = SessionLocal()
    try:
        row = db.query(AuditLog).filter(AuditLog.action == "security-settings").first()
        assert row is not None
        assert "désactivée" in (row.output or "")
        assert "192.168.0.0/16" in (row.output or "")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_invalid_cidr_is_rejected(client):
    response = await client.put(
        "/api/security/settings", json={"trusted_networks": ["192.168.0.0/16", "pas-un-reseau"]}
    )
    assert response.status_code == 400
    assert "pas-un-reseau" in response.json()["detail"]


@pytest.mark.asyncio
async def test_remember_me_sets_a_thirty_day_cookie(unauth_client):
    response = await unauth_client.post(
        "/api/login",
        data={"username": "admin", "password": "admin", "remember": "1"},
    )
    assert response.status_code == 200
    cookie = response.headers.get("set-cookie", "")
    assert "Max-Age=2592000" in cookie


@pytest.mark.asyncio
async def test_auth_state_exposes_bypass_without_secret():
    _configure(auth_enabled=False, trusted_networks=["127.0.0.1"])
    async with _client("127.0.0.1") as ac:
        response = await ac.get("/api/auth/state")
    body = response.json()
    assert response.status_code == 200
    assert body["auth_enabled"] is False
    assert body["bypass"] is True
    assert body["client_ip"] == "127.0.0.1"
    assert "mot de passe" not in str(body).lower()


@pytest.mark.asyncio
async def test_asic_can_be_armed_but_stays_out_of_campaigns(client):
    """6.8-réel : l'ASIC reçoit l'interrupteur ; son périmètre reste le redémarrage."""
    machine_id = _asic()
    response = await client.put(
        f"/api/machines/{machine_id}/simulation", json={"real": True, "confirm": "PILOTER"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["simulation_disabled"] is True
    assert body["pilotage"] == "Pilotage réel"
    assert body["simulated"] is False

    response = await client.put(
        f"/api/machines/{machine_id}/simulation", json={"real": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["pilotage"].startswith("Simulation")


@pytest.mark.asyncio
async def test_defaults_are_kept_when_nothing_was_saved():
    from app.ai_config import set_setting
    from app.models import Rule  # noqa: F401  (import tardif, tables déjà créées)

    db = SessionLocal()
    try:
        set_setting(db, auth_settings.AUTH_ENABLED_KEY, None)
        db.commit()
        settings = auth_settings.read_settings(db)
    finally:
        db.close()
    assert settings["trusted_networks"] == auth_settings.DEFAULT_NETWORKS
    assert settings["auth_enabled"] is True
