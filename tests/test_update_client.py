"""Côté Fossor (§4.18) : sauvegarde avant demande, protocole fichiers, fenêtre.

Les tests couvrent ce que le brief exige de l'application :

* le tag de cible est validé (``latest; rm -rf /`` n'atteint jamais l'updater) ;
* la sauvegarde ``pre-<cible>-<horodatage>`` (base cohérente + configuration)
  existe **avant** l'écriture de ``request.json`` ;
* sans compagnon, la route refuse la demande (bouton grisé côté écran) ;
* la fenêtre horaire de la mise à jour automatique gère le passage de minuit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import auto_update, updater_client

PARIS = ZoneInfo("Europe/Paris")


def session():
    from app.database import SessionLocal

    return SessionLocal()


def write_status(tmp_path: Path, **extra) -> None:
    path = tmp_path / "update" / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "idle",
        "from": "2026.11.003-dev",
        "to": None,
        "step": "",
        "message": "En attente",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── Validation ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("tag", ["2026.11.004", "2026.11.004-c1", "2030.01.999"])
def test_target_accepts_release_tags(tag):
    assert updater_client.normalize_target(tag) == tag


@pytest.mark.parametrize("tag", ["latest", "latest; rm -rf /", "2026.11.4", "", None, "v2026.11.004"])
def test_target_rejects_anything_else(tag):
    with pytest.raises(ValueError):
        updater_client.normalize_target(tag)


# ── Sauvegarde avant demande ───────────────────────────────────────────
async def test_backup_copies_a_consistent_database(client, tmp_path):
    created = await client.post(
        "/api/machines",
        json={"name": "rig-preu", "host": "10.0.0.9", "ssh_port": 22,
              "ssh_user": "fossor", "auth_type": "key"},
    )
    assert created.status_code in (200, 201), created.text
    db = session()
    try:
        backup = updater_client.create_pre_update_backup(db, "2026.11.004")
    finally:
        db.close()

    assert backup.name.startswith("pre-2026.11.004-")
    assert (backup / "fossor.db").is_file()
    assert (backup / "config.json").is_file()
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["target"] == "2026.11.004"

    con = sqlite3.connect(backup / "fossor.db")
    try:
        names = [row[0] for row in con.execute("select name from machines").fetchall()]
    finally:
        con.close()
    assert "rig-preu" in names


async def test_request_writes_backup_then_request(client, tmp_path):
    write_status(tmp_path)
    db = session()
    try:
        result = updater_client.request_update(db, "2026.11.004")
    finally:
        db.close()

    request = json.loads((tmp_path / "update" / "request.json").read_text())
    assert request["target"] == "2026.11.004"
    assert request["image"] == updater_client.expected_image()
    assert len(request["nonce"]) >= 8
    backup = Path(request["backup"])
    assert backup.is_dir() and (backup / "fossor.db").is_file()
    # la sauvegarde est datée d'avant la demande
    assert backup.name.startswith("pre-2026.11.004-")
    assert result["backup"] == str(backup)


async def test_request_refused_without_companion(client, tmp_path):
    with pytest.raises(ValueError) as excinfo:
        db = session()
        try:
            updater_client.request_update(db, "2026.11.004")
        finally:
            db.close()
    assert "fossor-updater" in str(excinfo.value)
    assert not (tmp_path / "update" / "request.json").exists()


def test_stale_heartbeat_means_absent(tmp_path):
    write_status(tmp_path, heartbeat_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    assert not updater_client.updater_present()

    write_status(tmp_path)
    assert updater_client.updater_present()


def test_active_state_counts_as_present_even_with_old_heartbeat(tmp_path):
    write_status(tmp_path, state="health_check",
                 heartbeat_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    assert updater_client.updater_present()


def test_pending_request_detected(tmp_path):
    write_status(tmp_path, state="pulling", request_nonce="abc123456789")
    (tmp_path / "update" / "request.json").write_text(
        json.dumps({"target": "2026.11.004", "nonce": "abc123456789"})
    )
    assert (updater_client.pending_request() or {}).get("target") == "2026.11.004"


# ── API ────────────────────────────────────────────────────────────────
async def test_status_exposes_updater_and_auto_block(client, tmp_path):
    response = await client.get("/api/update/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["updater"]["present"] is False
    assert payload["auto"]["enabled"] is False
    assert payload["auto"]["window"] == auto_update.DEFAULT_WINDOW


async def test_apply_route_returns_400_without_updater(client, tmp_path):
    response = await client.post("/api/update/apply", json={"target": "2026.11.004"})
    assert response.status_code == 400
    assert "fossor-updater" in response.json()["detail"]


async def test_apply_route_rejects_bad_tag(client, tmp_path):
    write_status(tmp_path)
    response = await client.post("/api/update/apply", json={"target": "latest; rm -rf /"})
    assert response.status_code == 400
    assert "Cible invalide" in response.json()["detail"]


async def test_apply_route_deposits_request(client, tmp_path):
    write_status(tmp_path)
    response = await client.post("/api/update/apply", json={"target": "2026.11.004"})
    assert response.status_code == 200, response.text
    assert (tmp_path / "update" / "request.json").is_file()
    assert response.json()["updater"]["pending"] == "2026.11.004"


async def test_auto_route_saves_window_and_refuses_garbage(client):
    response = await client.put(
        "/api/update/auto",
        json={"enabled": True, "window": {"start": "23:30", "end": "01:15", "timezone": "Europe/Paris"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["window"]["start"] == "23:30"

    bad = await client.put("/api/update/auto", json={"window": {"start": "25:00", "end": "02:00"}})
    assert bad.status_code == 400
    unknown_zone = await client.put("/api/update/auto", json={"window": {"timezone": "Mars/Olympus"}})
    assert unknown_zone.status_code == 400


# ── Fenêtre horaire ────────────────────────────────────────────────────
CROSSING = {"start": "23:00", "end": "02:00", "timezone": "Europe/Paris"}
DAYTIME = {"start": "03:00", "end": "05:00", "timezone": "Europe/Paris"}


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("2026-11-16T23:30:00", True),   # après le début
        ("2026-11-17T01:59:00", True),   # avant la fin, lendemain
        ("2026-11-17T02:00:00", False),  # borne de fin exclue
        ("2026-11-17T12:00:00", False),  # plein jour
        ("2026-11-16T22:59:00", False),  # juste avant le début
    ],
)
def test_window_crossing_midnight(moment, expected):
    local = datetime.fromisoformat(moment).replace(tzinfo=PARIS)
    assert auto_update.in_window(local, CROSSING) is expected


def test_window_daytime_and_utc_input():
    # 04:00 Paris = 03:00 UTC en hiver : la conversion doit se faire dans le fuseau
    utc = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
    assert auto_update.in_window(utc, DAYTIME)
    assert not auto_update.in_window(datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc), DAYTIME)


def test_window_full_day_when_start_equals_end():
    window = {"start": "00:00", "end": "00:00", "timezone": "Europe/Paris"}
    assert auto_update.in_window(datetime(2026, 11, 16, 4, 0, tzinfo=PARIS), window)


def test_window_validation():
    assert auto_update.normalize_window({"start": "03:00", "end": "05:00"})["timezone"] == "Europe/Paris"
    with pytest.raises(ValueError):
        auto_update.normalize_window({"start": "3h", "end": "05:00"})
    with pytest.raises(ValueError):
        auto_update.normalize_window({"start": "03:00", "end": "05:00", "timezone": "Nowhere/Land"})


# ── Déclencheur automatique ────────────────────────────────────────────
def update_available(target="2026.11.004", installed="2026.11.003-dev"):
    return {"update_available": True, "latest_version": target, "installed_version": installed}


def test_auto_update_disabled_by_default(client, tmp_path):
    write_status(tmp_path)
    db = session()
    try:
        verdict, reason = auto_update.should_trigger(
            db, datetime(2026, 11, 16, 4, 0, tzinfo=PARIS), update_available()
        )
        assert not verdict and reason == "désactivée"
        auto_update.save(db, enabled=True)
        verdict, reason = auto_update.should_trigger(
            db, datetime(2026, 11, 16, 4, 0, tzinfo=PARIS), update_available()
        )
        assert verdict, reason
        verdict, reason = auto_update.should_trigger(
            db, datetime(2026, 11, 16, 12, 0, tzinfo=PARIS), update_available()
        )
        assert not verdict and reason == "hors plage horaire"
    finally:
        db.close()


def test_auto_update_tick_deposits_request_once(client, tmp_path):
    write_status(tmp_path)
    db = session()
    try:
        auto_update.save(db, enabled=True)
        moment = datetime(2026, 11, 16, 4, 0, tzinfo=PARIS)
        original = auto_update.should_trigger

        monkeypatch_status = update_available()
        import app.update_checker as checker

        saved_state = dict(checker.STATE)
        checker.STATE.update({
            "latest_version": monkeypatch_status["latest_version"],
            "releases": [{"version": "2026.11.004", "name": "2026.11.004", "body": "",
                          "published_at": None, "url": None}],
        })
        try:
            first = auto_update.tick(db, moment)
            second = auto_update.tick(db, moment + timedelta(minutes=5))
        finally:
            checker.STATE.update(saved_state)
        assert first["triggered"] is True
        assert second["triggered"] is False and second["reason"] == "tentative déjà faite"
        assert (tmp_path / "update" / "request.json").is_file()
        assert auto_update.should_trigger is original
    finally:
        db.close()
