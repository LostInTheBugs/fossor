"""Benchmark de limite de puissance (§10.4) — logique, 409, simulation, modes."""

from __future__ import annotations

import json
import uuid as uuid_lib

import pytest
from fastapi.testclient import TestClient  # noqa: F401 — le client vient de conftest

from app import benchmark as B
from app import benchmark_runner as RUN


# ── Paliers et choix ─────────────────────────────────────────────────
def test_levels_bornes_par_min_et_max():
    rows = B.levels(100, 250, 180)
    assert [row["watts"] for row in rows] == [180, 162, 144, 126, 108]
    assert all(100 <= row["watts"] <= 250 for row in rows)


def test_levels_jamais_sous_le_minimum_ni_au_dessus_du_maximum():
    assert all(row["watts"] == 100 for row in B.levels(100, 250, 100))
    assert B.levels(100, 250, 300)[0]["watts"] == 250
    assert B.levels(100, 250, 260)[0]["watts"] == 250
    assert all(row["watts"] >= 150 for row in B.levels(150, 250, 160))
    with pytest.raises(ValueError):
        B.levels(250, 100, 180)
    with pytest.raises(ValueError):
        B.levels(None, 250, 180)


def test_choix_performance_et_rendement():
    steps = [
        {"watts": 180, "percent": 100, "hashrate_hs": 2_000.0, "power_w": 172.0, "result": "ok"},
        {"watts": 162, "percent": 90, "hashrate_hs": 1_900.0, "power_w": 155.0, "result": "ok"},
        {"watts": 126, "percent": 70, "hashrate_hs": 1_700.0, "power_w": 120.0, "result": "ok"},
    ]
    bests = B.pick_bests(steps)
    assert bests["performance"]["watts"] == 180
    assert bests["efficiency"]["watts"] == 126
    assert bests["efficiency"]["efficiency"] > bests["performance"]["efficiency"]


def test_choix_ignore_les_paliers_arretes():
    steps = [
        {"watts": 180, "hashrate_hs": 2_000.0, "power_w": 172.0, "result": "ok"},
        {"watts": 90, "hashrate_hs": 9_000.0, "power_w": 80.0, "result": "stopped"},
    ]
    bests = B.pick_bests(steps)
    assert bests["performance"]["watts"] == 180
    assert bests["efficiency"]["watts"] == 180
    assert B.pick_bests([]) == {"performance": None, "efficiency": None}


def test_arret_temperature_rejets_et_effondrement():
    assert "temperature" in B.stop_reason({"temperature_c": 86, "hashrate_hs": 1.0}, temp_limit_c=80)
    assert "rejets" in B.stop_reason({"reject_pct": 5.0, "hashrate_hs": 1.0})
    assert "hashrate" in B.stop_reason({"hashrate_hs": 10.0}, first_hashrate=100.0)
    assert B.stop_reason({"temperature_c": 70, "reject_pct": 0.5, "hashrate_hs": 100.0},
                         first_hashrate=100.0) is None


def test_mode_aucun_remet_la_limite_par_defaut():
    bests = {"performance": {"watts": 180}, "efficiency": {"watts": 108}}
    assert B.claimed_watts("none", bests, minimum=100, maximum=250, default=180) == 180
    assert B.claimed_watts("performance", bests, minimum=100, maximum=250, default=180) == 180
    assert B.claimed_watts("efficiency", bests, minimum=100, maximum=250, default=180) == 108
    assert B.claimed_watts("efficiency", bests, minimum=100, maximum=250, default=180) != 90
    with pytest.raises(ValueError):
        B.claimed_watts("turbo", bests, minimum=100, maximum=250, default=180)


def test_reapplication_apres_redemarrage():
    assert B.needs_reapply(5000, 120) is True
    assert B.needs_reapply(5000, 6000) is False
    assert B.needs_reapply(None, 10) is False


def test_estimation_et_hiveos():
    assert B.estimate(level_count=5, warmup_s=60, measure_min=3) == 5 * 240
    assert "HiveOS" in B.hiveos_hint(0, 150)


# ── Base de test ─────────────────────────────────────────────────────
def _db():
    from app.database import SessionLocal

    return SessionLocal()


def _profile(db, coin="PRL"):
    from app.models import MinerProfile

    row = db.query(MinerProfile).filter(MinerProfile.coin == coin).first()
    if row is not None:
        return row
    row = MinerProfile(
        uuid=str(uuid_lib.uuid4()),
        name=f"bench-{uuid_lib.uuid4().hex[:6]}",
        coin=coin,
        wallet="prl1testwalletfossor0000000000000000000000000000000000",
        pool="stratum+tcp://pool.exemple.tld:3333",
        software="srbminer",
        binary_url="http://192.0.2.1/srbminer.tar.gz",
        sha256="0" * 64,
        binary_path="/opt/fossor/miners/srbminer",
        version="1.0.0",
        cmdline="/bin/true",
        enabled=True,
    )
    db.add(row)
    db.commit()
    return row


def _machine(db, *, hiveos=False, uptime=5000, model="RTX 5060 Ti"):
    from app.models import Device, Machine

    profile = _profile(db)
    machine = Machine(
        name=f"bench-{uuid_lib.uuid4().hex[:6]}",
        host="192.0.2.21",
        ssh_port=22,
        ssh_user="fossor",
        auth_type="provision_pending",
        usage="gpu",
        os_kind="hiveos" if hiveos else "ubuntu",
        simulation_disabled=False,
        default_miner_profile_id=profile.id,
    )
    db.add(machine)
    db.commit()
    device = Device(
        uuid=str(uuid_lib.uuid4()),
        machine_id=machine.id,
        kind="gpu",
        index=0,
        model=model,
        present=True,
        enabled_for_mining=True,
        integrated=False,
        occupied_external=False,
        role="default_and_spec",
        created_at=B.iso(B.now_utc()),
        updated_at=B.iso(B.now_utc()),
    )
    db.add(device)
    db.commit()
    return machine, device, profile


def _run(db, machine, profile, **kw):
    from app.models import BenchmarkRun

    row = BenchmarkRun(
        uuid=str(uuid_lib.uuid4()),
        machine_id=machine.id,
        algo=profile.coin,
        profile_id=profile.id,
        status=kw.pop("status", "proposed"),
        measure_min=1,
        warmup_s=0,
        temp_limit_c=80.0,
        apply_mode=kw.pop("apply_mode", "none"),
        created_at=B.iso(B.now_utc()),
        **kw,
    )
    db.add(row)
    db.commit()
    return row


# ── API : une seule machine a la fois ────────────────────────────────
async def test_une_seule_machine_a_la_fois_409(client):
    db = _db()
    try:
        machine, device, profile = _machine(db)
        running = _run(db, machine, profile, status="running")
        other, other_device, other_profile = _machine(db)
        response = await client.post("/api/benchmark", json={"machine_id": other.id})
        assert response.status_code == 409
        assert str(running.id) in response.json()["detail"]
        running.status = "done"
        db.commit()
        assert (await client.post("/api/benchmark", json={"machine_id": other.id})).status_code == 200
    finally:
        db.close()


async def test_validation_et_options(client):
    db = _db()
    try:
        machine, device, profile = _machine(db)
        options = (await client.get("/api/benchmark/options")).json()
        assert options["machines"]
        assert options["sudoers_rule"].startswith("fossor ALL=")
        preview = (await client.post("/api/benchmark/preview", json={"machine_id": machine.id})).json()
        assert preview["algo"] == "PRL"
        assert len(preview["levels"]) == 5
        assert preview["duration_s"] > 0
        assert preview["simulated"] is True
        assert preview["bounds_source"] == "simulees"
    finally:
        db.close()


# ── Simulation : bout en bout, retour verifie ────────────────────────
async def test_benchmark_simule_jusquau_bout(client):
    db = _db()
    try:
        machine, device, profile = _machine(db)
        created = (await client.post("/api/benchmark", json={"machine_id": machine.id})).json()
        payload = (await client.post(f"/api/benchmark/{created['id']}/execute", json={})).json()["run"]
        assert payload["status"] == "done"
        assert payload["simulated"] is True
        assert len(payload["steps"]) == 5
        assert [s["watts"] for s in payload["steps"]] == [180, 162, 144, 126, 108]
        assert payload["bests"]["performance"]["watts"] == 180
        assert payload["bests"]["efficiency"]["watts"] == 108
        assert payload["restore_status"] == "ok"
        assert payload["verified"] is True
        assert payload["apply_mode"] == "none"
        assert payload["applied_watts"] == 180  # mode Aucun -> limite par defaut
        assert any(e["kind"] == "restore-verified" for e in payload["events"])
        rows = (await client.get("/api/benchmark/profiles")).json()["profiles"]
        assert {(row["gpu_model"], row["mode"]) for row in rows} >= {
            ("RTX 5060 Ti", "performance"), ("RTX 5060 Ti", "efficiency")}
    finally:
        db.close()


async def test_application_du_mode_puis_reapplication(client):
    db = _db()
    try:
        machine, device, profile = _machine(db)
        created = (await client.post("/api/benchmark", json={"machine_id": machine.id})).json()
        await client.post(f"/api/benchmark/{created['id']}/execute", json={})
        applied = (await client.post(
            f"/api/benchmark/{created['id']}/apply",
            json={"mode": "efficiency"})).json()
        assert applied["run"]["applied_watts"] == 108
        assert applied["run"]["apply_mode"] == "efficiency"
        assert B.needs_reapply(5000, 10) is True
        replayed = (await client.post(f"/api/benchmark/{created['id']}/reapply")).json()
        assert replayed["result"]["watts"] == 108
        back = (await client.post(f"/api/benchmark/{created['id']}/apply", json={"mode": "none"})).json()
        assert back["run"]["applied_watts"] == 180
    finally:
        db.close()


async def test_hiveos_proposition_seulement(client):
    db = _db()
    try:
        machine, device, profile = _machine(db, hiveos=True)
        created = (await client.post("/api/benchmark", json={"machine_id": machine.id})).json()
        payload = (await client.post(f"/api/benchmark/{created['id']}/execute", json={})).json()["run"]
        assert payload["hiveos_proposal"]
        assert "HiveOS" in payload["hiveos_proposal"]
        assert payload["applied_watts"] is None
        assert any(e["kind"] == "hiveos-proposal" for e in payload["events"])
    finally:
        db.close()


async def test_annulation_retour_verifie(client):
    db = _db()
    try:
        machine, device, profile = _machine(db)
        created = (await client.post("/api/benchmark", json={"machine_id": machine.id})).json()
        await client.post(f"/api/benchmark/{created['id']}/cancel", json={})
        payload = (await client.post(f"/api/benchmark/{created['id']}/execute", json={})).json()["run"]
        assert payload["status"] == "cancelled"
        assert payload["restore_status"] == "ok"
        assert payload["verified"] is True
    finally:
        db.close()
