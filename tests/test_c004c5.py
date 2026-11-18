"""2026.11.004-c5 : seconde PSU, TDP du CPU et migration des affectations."""

from __future__ import annotations

import sqlite3
import uuid as uuid_lib

import pytest

from app.cpu_stats import CPU_TDP, cpu_state_with, estimate_cpu_w
from app.psu import compute_power


# ── TDP du modèle (§ point 4) ────────────────────────────────���────────
def test_cpu_tdp_known_models():
    assert estimate_cpu_w("AMD Athlon 3000G with Radeon Vega Graphics") == (
        35.0,
        "TDP du modèle (Athlon 3000G)",
    )
    assert estimate_cpu_w("AMD Ryzen 5 3600 6-Core Processor")[0] == 65.0
    assert estimate_cpu_w("AMD Ryzen 5 5600G with Radeon Graphics")[0] == 65.0
    assert estimate_cpu_w("Intel(R) Core(TM) i5-4570 CPU @ 3.20GHz")[0] == 84.0
    assert estimate_cpu_w("Intel(R) Core(TM) i5-12400 @ 2.50GHz")[0] == 65.0
    assert len(CPU_TDP) >= 5


def test_cpu_tdp_fallback_is_declared_generic():
    assert estimate_cpu_w("Quad-Core Mystery") == (65.0, "estimation générique 65 W")
    assert estimate_cpu_w(None) == (65.0, "estimation générique 65 W")


def _cpu_report(power=None, source=""):
    return {
        "state": {
            "model": "AMD Athlon 3000G with Radeon Vega Graphics",
            "power_w": power,
            "power_source": source,
        }
    }


def test_cpu_power_falls_back_to_tdp_and_says_estimated():
    state = cpu_state_with(_cpu_report())
    assert state["power_w"] == 35.0
    assert state["power_source"] == "TDP du modèle (Athlon 3000G)"
    assert state["power_estimated"] is True


def test_measure_and_typed_value_beat_the_tdp():
    measured = cpu_state_with(_cpu_report(48.0, "rapl"))
    assert (measured["power_w"], measured["power_source"], measured["power_estimated"]) == (
        48.0,
        "rapl",
        False,
    )
    typed = cpu_state_with(_cpu_report(), typed_power_w=45.0)
    assert (typed["power_w"], typed["power_source"], typed["power_estimated"]) == (
        45.0,
        "saisie (machines.cpu_power_w)",
        False,
    )


# ── Affectation PSU (§ point 3) ──────────────────────────────────────
def _psu(pid, rated=750):
    return {
        "id": pid,
        "name": f"PSU {pid}",
        "rated_w": rated,
        "certification": "gold",
        "amps_12v": None,
        "derate_pct": 100,
        "measured_efficiency": None,
        "is_default": pid == 1,
    }


def _card(**kwargs):
    return {
        "kind": "gpu",
        "label": "GPU 0",
        "model": "GeForce RTX 3070",
        "power_w": None,
        "psu_id": None,
        "psu2_id": None,
        "split_w": None,
        **kwargs,
    }


def test_second_psu_alone_carries_the_whole_card():
    """psu_id NULL + psu2_id : la carte entière sur la seconde PSU, sans étiquette."""
    result = compute_power([_psu(1), _psu(2)], devices=[_card(psu2_id=2, split_w=280)])
    by_id = {row["id"]: row for row in result["psus"]}
    assert by_id[1]["load_dc_w"] == 0.0
    assert by_id[2]["load_dc_w"] == 220.0
    assert [item["label"] for item in by_id[2]["items"]] == ["GPU 0"]
    assert not any("slot/riser" in item["label"] for item in by_id[2]["items"])


def test_split_only_when_both_psus_are_set():
    result = compute_power(
        [_psu(1), _psu(2)], devices=[_card(psu_id=1, psu2_id=2, split_w=75)]
    )
    by_id = {row["id"]: row for row in result["psus"]}
    assert by_id[1]["load_dc_w"] == 75.0
    assert by_id[2]["load_dc_w"] == 145.0
    assert any("slot/riser" in item["label"] for item in by_id[1]["items"])


def test_migration_moves_second_psu_up(tmp_path, monkeypatch):
    from app import database

    path = tmp_path / "mig-c5.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE devices (id INTEGER PRIMARY KEY, psu_id INTEGER, "
        "psu2_id INTEGER, split_w FLOAT)"
    )
    conn.execute(
        "INSERT INTO devices (id, psu_id, psu2_id, split_w) VALUES "
        "(1, NULL, 3, 280), (2, 1, 2, 75), (3, 2, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "_get_db_url", lambda: f"sqlite:///{path}")
    database._migrate_device_psu_split_meaning()

    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT id, psu_id, psu2_id, split_w FROM devices ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [(1, 3, None, None), (2, 1, 2, 75.0), (3, 2, None, None)]


async def test_api_refuses_second_psu_without_first(client):
    from app.database import SessionLocal
    from app.models import Device

    created = await client.post(
        "/api/machines",
        json={
            "name": "psu-c5",
            "host": "127.0.0.1",
            "ssh_port": 22,
            "ssh_user": "tester",
            "auth_type": "key",
        },
    )
    assert created.status_code in (200, 201), created.text
    machine_id = created.json()["id"]
    first = await client.post(
        f"/api/machines/{machine_id}/psus",
        json={"name": "Corsair", "rated_w": 750, "certification": "gold"},
    )
    second = await client.post(
        f"/api/machines/{machine_id}/psus",
        json={"name": "Seasonic", "rated_w": 850, "certification": "gold"},
    )
    assert first.status_code == 201 and second.status_code == 201

    db = SessionLocal()
    try:
        device = Device(
            uuid=str(uuid_lib.uuid4()),
            machine_id=machine_id,
            kind="gpu",
            index=0,
            model="GeForce RTX 3070",
            present=True,
            enabled_for_mining=True,
        )
        db.add(device)
        db.commit()
        device_id = device.id
    finally:
        db.close()

    alone = await client.put(
        f"/api/devices/{device_id}/psu",
        json={"psu2_id": second.json()["id"], "split_w": 280},
    )
    assert alone.status_code == 422, alone.text

    twice = await client.put(
        f"/api/devices/{device_id}/psu",
        json={"psu_id": first.json()["id"], "psu2_id": first.json()["id"]},
    )
    assert twice.status_code == 422, twice.text

    saved = await client.put(
        f"/api/devices/{device_id}/psu", json={"psu_id": first.json()["id"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["psu2_id"] is None
    assert saved.json()["split_w"] is None
