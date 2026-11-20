"""Préprod — niveau de journal, bruit d'audit et voyants des routines."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app import observability
from app.database import SessionLocal
from app.models import AuditLog


def _db():
    return SessionLocal()


def _count(action: str) -> int:
    db = _db()
    try:
        return db.query(AuditLog).filter(AuditLog.action == action).count()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_default_level_is_warning(client):
    response = await client.get("/api/logs/settings")
    assert response.status_code == 200
    assert response.json()["level"] == "WARNING"


@pytest.mark.asyncio
async def test_level_change_applies_to_stdout_and_is_audited(client):
    response = await client.put("/api/logs/settings", json={"level": "DEBUG"})
    assert response.status_code == 200
    assert logging.getLogger("fossor").level == logging.DEBUG
    assert _count("log-level") == 1
    # On remet le défaut pour les tests suivants.
    await client.put("/api/logs/settings", json={"level": "WARNING"})


@pytest.mark.asyncio
async def test_invalid_level_is_rejected(client):
    response = await client.put("/api/logs/settings", json={"level": "VERBOSE"})
    assert response.status_code == 400


def test_routine_ok_twice_writes_no_audit_line():
    db = _db()
    try:
        observability.mark_routine(db, "collect", True, "4/4 machines jointes", interval_s=60)
        observability.mark_routine(db, "collect", True, "4/4 machines jointes", interval_s=60)
    finally:
        db.close()
    assert _count("routine-collect") == 0


def test_routine_state_change_writes_one_warning_line():
    db = _db()
    try:
        observability.mark_routine(db, "collect", True, "4/4 machines jointes", interval_s=60)
        observability.mark_routine(db, "collect", False, "machine 3 injoignable", interval_s=60)
        observability.mark_routine(db, "collect", False, "machine 3 injoignable", interval_s=60)
    finally:
        db.close()
    assert _count("routine-collect") == 1


def test_error_level_filters_the_recovery_warning():
    db = _db()
    try:
        observability.set_log_level(db, "ERROR")
        observability.mark_routine(db, "collect", True, "ok", interval_s=60)
        observability.mark_routine(db, "collect", False, "panne", interval_s=60)
        before = _count("routine-collect")
        observability.mark_routine(db, "collect", True, "rétablie", interval_s=60)
        after = _count("routine-collect")
        observability.set_log_level(db, "WARNING")
    finally:
        db.close()
    assert before == 1  # l'erreur passe
    assert after == before  # le retour à OK est un WARNING, filtré par ERROR


def test_critical_level_silences_routine_errors_but_not_actions(client):
    db = _db()
    try:
        observability.set_log_level(db, "CRITICAL")
    finally:
        db.close()
    db = _db()
    try:
        observability.mark_routine(db, "backup", False, "disque plein", interval_s=60)
    finally:
        db.close()
    assert _count("routine-backup") == 0


@pytest.mark.asyncio
async def test_actions_are_always_audited_whatever_the_level(client):
    await client.put("/api/logs/settings", json={"level": "CRITICAL"})
    response = await client.put(
        "/api/security/settings", json={"auth_enabled": False, "trusted_networks": ["127.0.0.1"]}
    )
    assert response.status_code == 200
    assert _count("security-settings") >= 1
    await client.put("/api/logs/settings", json={"level": "WARNING"})


def test_snapshot_flags_late_and_failure():
    from app.ai_config import set_setting

    now = datetime.now(timezone.utc)
    states = {
        "backup": {
            "state": "ok",
            "last_run": (now - timedelta(hours=5)).isoformat(),
            "last_ok": (now - timedelta(hours=5)).isoformat(),
            "detail": "fossor-1.db",
            "interval_s": 3600,
        },
        "collect": {
            "state": "fail",
            "last_run": now.isoformat(),
            "detail": "panne",
            "interval_s": 60,
        },
    }
    db = _db()
    try:
        set_setting(db, observability.ROUTINE_KEY, states)
        db.commit()
        snapshot = {row["name"]: row for row in observability.routine_snapshot(db)}
    finally:
        db.close()
    assert snapshot["backup"]["state"] == "late"
    assert snapshot["collect"]["state"] == "fail"
    assert snapshot["veille"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_snapshot_is_served_by_the_api(client):
    response = await client.get("/api/routines")
    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "WARNING"
    assert {row["name"] for row in body["routines"]} == set(observability.ROUTINE_LABELS)
