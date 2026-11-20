"""Pilotage réel par machine : le global ne décide plus d'une action.

La règle tenue ici : une machine armée (« PILOTER ») agit en réel même avec le
global ON ; une machine non armée reste simulée quel que soit le global.
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai_config import set_setting
from app.database import SessionLocal
from app.models import Machine, MinerProfile
from app.routers import miners as MINERS

# Adresse factice, écrite de zéro : même forme qu'une adresse Pearl réelle
# (119 caractères, un « + »), mais aucune valeur on-chain.
FAKE_WALLET = "prl1" + "q" * 100 + "+" + "A" * 9 + "/" + "B" * 3 + "="
assert len(FAKE_WALLET) == 119


def _machine(name: str) -> Machine:
    db = SessionLocal()
    row = Machine(
        name=name,
        host="192.0.2.10",
        ssh_port=22,
        ssh_user="fossor",
        auth_type="fossor_key",
        enabled=True,
        machine_kind="host",
        os_kind="other",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.expunge(row)
    db.close()
    return row


def _profile(name: str = "Profil pilotage") -> MinerProfile:
    db = SessionLocal()
    row = MinerProfile(
        uuid=str(uuid.uuid4()),
        name=name,
        coin="PRL",
        wallet=FAKE_WALLET,
        pool="stratum+tcp://pool.invalid:3333",
        worker="",
        software="custom",
        binary_path="miner",
        binary_url="",
        version="1.0.0",
        sha256="",
        cmdline=(
            "/opt/fossor/miners/custom/1.0.0/miner --wallet {wallet} --pool {pool} "
            "--worker {worker} --api-port {api_port}"
        ),
        api_port=3333,
        execution_mode="host",
        threads=1,
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.expunge(row)
    db.close()
    return row


def _global(db, value: bool) -> None:
    set_setting(db, "simulation_mode", value)
    db.commit()


def _arm(client, machine_id: int):
    return client.put(
        f"/api/machines/{machine_id}/simulation",
        json={"real": True, "confirm": "PILOTER"},
    )


# ── La fonction unique ────────────────────────────────────────────────
@pytest.mark.parametrize("global_on", [True, False])
@pytest.mark.parametrize("armed", [True, False])
def test_simulated_for_matrix(client, global_on, armed):
    from app import pilotage

    machine = _machine(f"matrice-{global_on}-{armed}")
    db = SessionLocal()
    try:
        _global(db, global_on)
        row = db.query(Machine).filter(Machine.id == machine.id).first()
        row.simulation_disabled = armed
        db.commit()
        assert pilotage.simulated_for(db, row) is (not armed)
        expected = "Pilotage réel" if armed else (
            "Simulation (globale)" if global_on else "Simulation (machine non armée)"
        )
        assert pilotage.label(db, row) == expected
    finally:
        db.close()


# ── Chemins réels : start / stop ─────────────────────────────────────
@pytest.mark.parametrize("global_on", [True, False])
@pytest.mark.parametrize("armed", [True, False])
@pytest.mark.asyncio
async def test_start_matrix(client, monkeypatch, global_on, armed):
    machine = _machine(f"start-{global_on}-{armed}")
    profile = _profile(f"profil-start-{global_on}-{armed}")
    db = SessionLocal()
    try:
        _global(db, global_on)
        row = db.query(Machine).filter(Machine.id == machine.id).first()
        row.simulation_disabled = armed
        row.default_miner_profile_id = profile.id
        db.commit()
    finally:
        db.close()

    seen: dict[str, bool] = {}

    async def fake_start(machine, profile, argv, *, simulated, user):
        seen["simulated"] = simulated
        return {"simulated": simulated, "ok": True, "commands": []}

    monkeypatch.setattr(MINERS.R, "start", fake_start)
    response = await client.post(f"/api/machines/{machine.id}/miner/start", json={})
    assert response.status_code == 200, response.text
    assert seen["simulated"] is (not armed)
    assert response.json()["simulated"] is (not armed)


@pytest.mark.parametrize("global_on", [True, False])
@pytest.mark.parametrize("armed", [True, False])
@pytest.mark.asyncio
async def test_stop_matrix(client, monkeypatch, global_on, armed):
    machine = _machine(f"stop-{global_on}-{armed}")
    profile = _profile(f"profil-stop-{global_on}-{armed}")
    db = SessionLocal()
    try:
        _global(db, global_on)
        row = db.query(Machine).filter(Machine.id == machine.id).first()
        row.simulation_disabled = armed
        row.default_miner_profile_id = profile.id
        db.commit()
    finally:
        db.close()

    seen: dict[str, bool] = {}

    async def fake_stop(machine, profile, *, simulated, user):
        seen["simulated"] = simulated
        return {"simulated": simulated, "ok": True, "commands": []}

    monkeypatch.setattr(MINERS.R, "stop", fake_stop)
    response = await client.post(f"/api/machines/{machine.id}/miner/stop", json={})
    assert response.status_code == 200, response.text
    assert seen["simulated"] is (not armed)
    assert response.json()["simulated"] is (not armed)


@pytest.mark.asyncio
async def test_start_direct_sur_machine_non_armee_global_off_est_simule(client, monkeypatch):
    machine = _machine("direct-off-non-arme")
    profile = _profile("profil-direct-off")
    db = SessionLocal()
    try:
        _global(db, False)
        row = db.query(Machine).filter(Machine.id == machine.id).first()
        row.default_miner_profile_id = profile.id
        db.commit()
    finally:
        db.close()

    async def fake_start(machine, profile, argv, *, simulated, user):
        return {"simulated": simulated, "ok": True, "commands": []}

    monkeypatch.setattr(MINERS.R, "start", fake_start)
    response = await client.post(f"/api/machines/{machine.id}/miner/start", json={})
    assert response.status_code == 200, response.text
    assert response.json()["simulated"] is True


# ── Campagnes ────────────────────────────────────────────────────────
@pytest.mark.parametrize("global_on", [True, False])
@pytest.mark.parametrize("armed", [True, False])
def test_campaign_matrix(client, monkeypatch, global_on, armed):
    from app import pilotage

    machine = _machine(f"campagne-{global_on}-{armed}")
    db = SessionLocal()
    try:
        _global(db, global_on)
        row = db.query(Machine).filter(Machine.id == machine.id).first()
        row.simulation_disabled = armed
        db.commit()
        target = SimpleNamespace(machine=row)
        monkeypatch.setattr(
            "app.campaign_runner.campaign_targets", lambda _db, _campaign: [target]
        )
        campaign = SimpleNamespace(id=1)
        assert pilotage.campaign_simulated(db, campaign) is (not armed)
        assert pilotage.campaign_locked(db, campaign) is (not armed)
    finally:
        db.close()


def test_campaign_mixte_reste_simulee(client, monkeypatch):
    from app import pilotage

    armed = _machine("campagne-armed")
    unarmed = _machine("campagne-unarmed")
    db = SessionLocal()
    try:
        _global(db, True)
        rows = db.query(Machine).filter(Machine.id.in_([armed.id, unarmed.id])).all()
        by_id = {row.id: row for row in rows}
        by_id[armed.id].simulation_disabled = True
        db.commit()
        targets = [SimpleNamespace(machine=by_id[armed.id]), SimpleNamespace(machine=by_id[unarmed.id])]
        monkeypatch.setattr(
            "app.campaign_runner.campaign_targets", lambda _db, _campaign: targets
        )
        assert pilotage.campaign_simulated(db, SimpleNamespace(id=2)) is True
    finally:
        db.close()


# ── Wallet Pearl ─────────────────────────────────────────────────────
def test_wallet_119_caracteres_avec_plus_slash_egal():
    import re

    from app import miners as M

    assert re.fullmatch(M.PATTERNS["wallet"], FAKE_WALLET)
    assert re.fullmatch(M.PATTERNS["wallet"], "a" * 160)
    assert not re.fullmatch(M.PATTERNS["wallet"], "a" * 161)


def test_wallet_reste_un_argv_separe_et_quote():
    from app import miners as M

    profile = {
        "name": "Profil pilotage",
        "software": "custom",
        "binary_path": "miner",
        "version": "1.0.0",
        "cmdline": "miner --wallet {wallet} --pool {pool}",
        "wallet": FAKE_WALLET,
        "pool": "stratum+tcp://pool.invalid:3333",
        "worker": "",
        "api_port": 3333,
        "execution_mode": "host",
        "threads": 1,
    }
    values = M.build_values(profile, worker="rig", gpus=[0], threads=1, api_port=3333)
    argv = M.render_argv(profile, values)
    assert FAKE_WALLET in argv
    assert not any(";" in part or "|" in part for part in argv)
    assert argv[argv.index("--wallet") + 1] == FAKE_WALLET


# ── Garde-fou ────────────────────────────────────────────────────────
def test_aucun_module_ne_decide_sur_la_simulation_globale():
    base = Path(__file__).resolve().parent.parent / "app"
    allowed = {"pilotage.py", "routers/miners.py"}
    offenders: list[str] = []
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        if rel in allowed:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "simulation_enabled(" in line or "simulation_mode" in line:
                offenders.append(f"{rel}:{number}: {stripped}")
    assert not offenders, offenders


def test_start_et_stop_du_routeur_utilisent_simulated_for():
    for function in (MINERS.start_miner, MINERS.stop_miner):
        source = inspect.getsource(function)
        assert "pilotage.simulated_for(db, machine)" in source
        assert "simulation_enabled" not in source
