"""Tests du mode boost (§10.3) — pool, portée, séquence, retour vérifié.

Aucune machine réelle : les machines HiveOS sont simulées par un faux pilote
et la base est celle du harnais habituel.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import boost as B


class FakeDevice:
    def __init__(self, **kw):
        self.uuid = kw.get("uuid", str(uuid.uuid4()))
        self.machine_id = kw.get("machine_id", 1)
        self.kind = kw.get("kind", "gpu")
        self.index = kw.get("index", 0)
        self.model = kw.get("model", "RTX")
        self.present = kw.get("present", True)
        self.enabled_for_mining = kw.get("enabled_for_mining", True)
        self.integrated = kw.get("integrated", False)
        self.occupied_external = kw.get("occupied_external", False)
        self.boost_pool = kw.get("boost_pool", True)
        self.role = kw.get("role", "default_and_spec")


# ── Pool ──────────────────────────────────────────────────────────────
def test_pool_hiveos_in_athlon_out_and_ryzen_cpu_never():
    """GPU HiveOS par défaut, GPU athlon seulement si cochés, CPU Ryzen jamais."""
    hive = [FakeDevice(machine_id=1, index=i) for i in range(5)]
    athlon_gpu = FakeDevice(machine_id=2, index=0, boost_pool=False)
    ryzen_gpu = FakeDevice(machine_id=3, index=0, boost_pool=True)
    ryzen_cpu = FakeDevice(machine_id=3, kind="cpu", index=None, boost_pool=True)
    rows = B.pool_rows(hive + [athlon_gpu, ryzen_gpu, ryzen_cpu], {ryzen_cpu.uuid: "BIGCOIN"})
    in_pool = [row for row in rows if row["in_pool"]]
    assert len(in_pool) == 6, rows  # 5 GPU HiveOS + GPU Ryzen coché
    assert athlon_gpu.uuid not in {row["uuid"] for row in in_pool}
    bad = next(row for row in rows if row["uuid"] == ryzen_cpu.uuid)
    assert not bad["in_pool"] and "BIGCOIN" in bad["excluded_reason"]


def test_integrated_gpu_never_in_pool():
    igpu = FakeDevice(integrated=True)
    row = B.pool_rows([igpu])[0]
    assert not row["in_pool"] and "intégré" in row["excluded_reason"]


def test_proportion_is_bounded_and_non_empty():
    assert B.proportion_count(10, 50) == 5
    assert B.proportion_count(3, 10) == 1
    assert B.proportion_count(0, 50) == 0
    with pytest.raises(B.BoostError):
        B.proportion_count(4, 0)
    with pytest.raises(B.BoostError):
        B.proportion_count(4, 101)


def test_selection_proportion_takes_gpus_first():
    devices = [
        FakeDevice(kind="cpu", index=None, machine_id=1),
        FakeDevice(kind="gpu", index=0, machine_id=1),
        FakeDevice(kind="gpu", index=1, machine_id=1),
        FakeDevice(kind="gpu", index=2, machine_id=2),
    ]
    rows = B.select_rows(B.pool_rows(devices), scope="proportion", proportion=50)
    assert len(rows) == 2
    assert all(row["kind"] == "gpu" for row in rows)


def test_hiveos_all_or_nothing_refusal():
    assert B.hiveos_refusal(machine_name="rig", hiveos=True, gpu_total=6, selected_gpu_indices=[0, 1]) is not None
    assert B.hiveos_refusal(machine_name="rig", hiveos=True, gpu_total=6, selected_gpu_indices=range(6)) is None
    assert B.hiveos_refusal(machine_name="athlon", hiveos=False, gpu_total=2, selected_gpu_indices=[0]) is None


# ── Durée et échéance ─────────────────────────────────────────────────
def test_duration_bounded_and_deadline():
    assert B.validate_duration(None) == 6.0
    assert B.validate_duration(24) == 24.0
    for bad in (0, 0.5, 73, "x"):
        with pytest.raises(B.BoostError):
            B.validate_duration(bad)
    start = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    assert B.deadline(start, 6) == start + timedelta(hours=6)


def test_cancel_requests_stop_and_is_due():
    moment = datetime.now(timezone.utc)
    boost = type("BoostRow", (), {"stop_requested": True, "ends_at": B.parse_iso(B.deadline(moment, 6))})()
    assert B.is_due(boost, moment)
    boost.stop_requested = False
    assert not B.is_due(boost, moment)
    assert B.is_due(boost, moment + timedelta(hours=7))


# ── Séquences ─────────────────────────────────────────────────────────
def test_entry_and_exit_sequences_order():
    entry = B_ENTRY = __import__("app.boost_runner", fromlist=["x"]).entry_sequence(True)
    assert entry[:2] == ["wd stop", "miner stop"]
    exit_steps = __import__("app.boost_runner", fromlist=["x"]).exit_sequence(True)
    assert exit_steps.index("miner start") < exit_steps.index("wd start")
    assert exit_steps[-1] == "token par défaut vérifié"


# ── Règle Hermes ──────────────────────────────────────────────────────
def _rule(db, rule_id, *, enabled, params=None):
    from app.models import Rule

    rule = db.get(Rule, rule_id)
    rule.enabled = enabled
    if params is not None:
        rule.params = params
    db.commit()


def test_hermes_rule_disabled_proposes_only():
    from app import rules as R
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _rule(db, "boost_launch", enabled=False)
        plan = R.plan_boost(db, {"age_hours": 2, "verdict": "retenu"})
        assert plan["allowed"] is False and "désactivée" in plan["reason"]
        _rule(db, "boost_launch", enabled=True, params=R.default_params("boost_launch"))
        _rule(db, "auto_launch", enabled=False)
        plan = R.plan_boost(db, {"age_hours": 2, "verdict": "retenu"})
        assert plan["allowed"] is True and plan["launch"] is False
        # seuils : chaîne trop vieille, verdict différent
        assert R.plan_boost(db, {"age_hours": 30, "verdict": "retenu"})["allowed"] is False
        assert R.plan_boost(db, {"age_hours": 2, "verdict": "veille"})["allowed"] is False
        _rule(db, "auto_launch", enabled=True, params={"gpu": True, "cpu": True})
        plan = R.plan_boost(db, {"age_hours": 2, "verdict": "retenu"})
        assert plan["allowed"] is True and plan["launch"] is True
    finally:
        _rule(db, "boost_launch", enabled=False)
        _rule(db, "auto_launch", enabled=False, params={"gpu": True, "cpu": True})
        db.close()


# ── Dette : « database is locked » à l'épinglage ──────────────────────
def test_pin_host_key_succeeds_while_a_reader_is_open():
    """Le lecteur ne doit plus bloquer l'écriture d'épinglage (WAL + busy_timeout)."""
    from app import ssh_client
    from app.database import SessionLocal
    from app.models import Machine

    reader = SessionLocal()
    try:
        machine = Machine(
            uuid=str(uuid.uuid4()),
            name="epingles",
            host="192.0.2.10",
            ssh_port=22,
            ssh_user="tester",
            auth_type="password",
        )
        reader.add(machine)
        reader.commit()
        reader.refresh(machine)
        machine_id = machine.id
        # Transaction de lecture **ouverte** (comme une requête en cours).
        assert reader.query(Machine).filter(Machine.id == machine_id).first() is not None
        ssh_client._pin_host_key(machine_id, "SHA256:test", "ssh-ed25519 AAAA test")
        reader.expire_all()
        fresh = reader.query(Machine).filter(Machine.id == machine_id).first()
        assert fresh.host_key == "SHA256:test"
    finally:
        reader.close()


# ── Cycle complet simulé ──────────────────────────────────────────────
def _seed(db, *, hiveos: bool = False):
    from app.models import Campaign, Device, Machine, MinerProfile

    profile = MinerProfile(
        uuid=str(uuid.uuid4()),
        name=f"boost-{uuid.uuid4().hex[:6]}",
        coin="NEWC",
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
    db.add(profile)
    db.commit()
    db.refresh(profile)
    machine = Machine(
        uuid=str(uuid.uuid4()),
        name=f"boost-machine-{uuid.uuid4().hex[:6]}",
        host="192.0.2.20",
        ssh_port=22,
        ssh_user="tester",
        auth_type="password",
        usage="both",
        enabled=True,
        os_kind="hiveos" if hiveos else "ubuntu",
        default_miner_profile_id=profile.id,
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    db.add(Device(
        uuid=str(uuid.uuid4()),
        machine_id=machine.id,
        kind="gpu",
        index=0,
        model="RTX test",
        present=True,
        enabled_for_mining=True,
        boost_pool=True,
        default_token_id=None,
    ))
    db.commit()
    campaign = Campaign(
        uuid=str(uuid.uuid4()),
        coin="NEWC",
        profile_id=profile.id,
        start_at=datetime.now(timezone.utc).isoformat(),
        status="running",
        origin="manual",
        execution_mode="host",
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return machine, profile, campaign


class FakeBackend:
    def __init__(self, hashrate=1500.0):
        self.calls = []
        self.hashrate_value = hashrate

    async def start(self, machine, profile, argv, simulated):
        self.calls.append(("start", machine.name, bool(simulated)))
        return {"commands": ["start"]}

    async def stop(self, machine, profile, simulated):
        self.calls.append(("stop", machine.name, bool(simulated)))
        return {"commands": ["stop"]}

    async def hashrate(self, machine, profile):
        return self.hashrate_value


@pytest.mark.asyncio
async def test_boost_full_cycle_simulated_and_restore_verified():
    from app import boost_runner as RUN
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        machine, profile, campaign = _seed(db)
        boost = RUN.create(db, {"campaign_id": campaign.id, "coin": "NEWC", "scope": "pool", "duration_h": 6})
        assert boost.status == "approved"
        backend = FakeBackend(1500.0)
        result = await RUN.start_boost(db, boost, backend=backend, simulated=True)
        db.refresh(boost)
        assert result["action"] == "started" and boost.status == "running"
        assert boost.ends_at is not None
        assert ("start", machine.name, True) in backend.calls
        stop = await RUN.stop_boost(db, boost, backend=backend, simulated=True)
        db.refresh(boost)
        assert stop["verified"] is True
        assert boost.status == "done"
        assert any(call[0] == "start" for call in backend.calls)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restore_retries_then_fails_and_alerts():
    from app import boost_runner as RUN
    from app.database import SessionLocal
    from app.models import BoostDevice

    db = SessionLocal()
    try:
        machine, profile, campaign = _seed(db)
        boost = RUN.create(db, {"campaign_id": campaign.id, "coin": "NEWC", "scope": "pool"})
        backend = FakeBackend(0.0)
        await RUN.start_boost(db, boost, backend=backend, simulated=False)
        stop = await RUN.stop_boost(db, boost, backend=backend, simulated=False)
        db.refresh(boost)
        # Premier passage : la référence n'est pas revenue, échéance encore future.
        assert stop["action"] == "restore-pending"
        boost.restore_deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.commit()
        second = await RUN._verify_restore(db, boost, datetime.now(timezone.utc), backend, False)
        assert second["action"] == "restore-retry"
        boost.restore_deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.commit()
        third = await RUN._verify_restore(db, boost, datetime.now(timezone.utc), backend, False)
        assert third["action"] == "restore-retry"
        boost.restore_deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.commit()
        fourth = await RUN._verify_restore(db, boost, datetime.now(timezone.utc), backend, False)
        db.refresh(boost)
        assert fourth["action"] == "failed"
        assert boost.status == "failed" and "deux essais" in (boost.note or "")
        assert db.query(BoostDevice).filter(BoostDevice.boost_id == boost.id).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_watchdog_silent_during_boost_switch():
    from app import boost_runner as RUN
    from app import watchdog as W
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        machine, profile, campaign = _seed(db)
        boost = RUN.create(db, {"campaign_id": campaign.id, "coin": "NEWC", "scope": "pool"})
        reason = W.campaign_switching(db, machine)
        assert "boost" in reason and "entrée" in reason
        boost.status = "stopping"
        db.commit()
        assert "sortie" in W.campaign_switching(db, machine)
        boost.status = "done"
        db.commit()
        assert W.campaign_switching(db, machine) == ""
    finally:
        db.close()


def test_one_boost_per_device_is_refused():
    from app import boost_runner as RUN
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        machine, profile, campaign = _seed(db)
        RUN.create(db, {"campaign_id": campaign.id, "coin": "NEWC", "scope": "pool"})
        with pytest.raises(B.BoostError, match="déjà engagé"):
            RUN.create(db, {"campaign_id": campaign.id, "coin": "NEWC", "scope": "pool"})
    finally:
        db.close()
