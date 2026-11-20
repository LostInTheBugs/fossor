"""Correctifs postmigration athlon (sur ``main``, sans tag).

Aucune connexion réelle : le SSH du push est **injecté**, l'API du mineur est un
corps JSON figé (fixture athlon), et la matrice VM utilise des objets machines.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

FIX = Path(__file__).parent / "fixtures"
SHA = "b" * 64

# ``ps`` réel de l'athlon : l'unité Fossor est là, mais ``--api-port`` a disparu.
PS = (
    "### NVSMI\n"
    "0, 0000:08:00.0, 61, 148.32, 55, 100\n"
    "### PS_MINERS\n"
    "root 1187 6.4 160630 /opt/fossor/miners/srbminer/3.8.3/SRBMiner-MULTI "
    "--algorithm pearlhash --wallet testwallet000000000000 "
    "--pool pearl-eu2.luckypool.io:3360 --worker athlon --api-enable\n"
    "### UPTIME\n"
    "160630.00 999999.00\n"
)


def profile_dict(**over) -> dict:
    data = {
        "name": "Athlon KSH",
        "coin": "KSH",
        "wallet": "Ktestaddress000000000000000000",
        "pool": "stratum+tcp://pool.exemple.tld:3333",
        "software": "srbminer",
        "binary_url": "",
        "sha256": SHA,
        "binary_path": "SRBMiner-MULTI",
        "version": "3.8.3",
        "cmdline": (
            "/opt/fossor/miners/srbminer/3.8.3/SRBMiner-MULTI --algorithm pearlhash "
            "--pool {pool} --wallet {wallet} --gpus {gpus} --api-port {api_port}"
        ),
        "api_port": 21550,
        "api_kind": "srbminer",
        "binary_source": "fossor",
        "execution_mode": "host",
    }
    data.update(over)
    return data


@pytest.fixture
def session():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _machine_with_profile(db):
    from app.models import Machine, MinerProfile

    now = datetime.now(timezone.utc).isoformat()
    profile = MinerProfile(
        uuid=str(uuid.uuid4()),
        name="athlon-ksh",
        coin="KSH",
        wallet="Ktestaddress000000000000000000",
        pool="stratum+tcp://pool.exemple.tld:3333",
        software="srbminer",
        binary_url="",
        sha256=SHA,
        binary_path="SRBMiner-MULTI",
        version="3.8.3",
        cmdline=profile_dict()["cmdline"],
        api_port=21550,
        api_kind="srbminer",
        binary_source="fossor",
        execution_mode="host",
        created_at=now,
        updated_at=now,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    machine = Machine(
        name="athlon-postmigration",
        host="192.0.2.10",
        ssh_port=22,
        ssh_user="fossor",
        auth_type="fossor_key",
        machine_kind="host",
        default_miner_profile_id=profile.id,
        created_at=now,
        updated_at=now,
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine, profile


# ── 1. API déclarée par le profil pour une unité Fossor ────────────────
def test_fossor_unit_takes_api_from_its_profile(session):
    from app import collector as C

    machine, profile = _machine_with_profile(session)
    found = C.launched_profile(session, machine, PS)
    assert found is not None and found.id == profile.id

    stats = {"miner": "", "api_port": None,
             "miners": [{"name": "SRBMiner", "api_port": None}]}
    C._apply_profile_api(stats, profile)
    assert stats["miner"] == "srbminer"
    assert stats["api_kind"] == "srbminer"
    assert stats["api_port"] == 21550
    assert stats["miners"][0]["api_port"] == 21550
    assert stats["miners"][0]["name"] == "srbminer"


def test_foreign_process_is_not_a_fossor_unit(session):
    from app import collector as C

    machine, _profile = _machine_with_profile(session)
    foreign = "### PS_MINERS\nroot 1 0 0 /usr/bin/cpuminer-korsh --algo yespower\n"
    assert C.launched_profile(session, machine, foreign) is None


def test_profile_api_payload_carries_each_gpu_hashrate():
    from app import collector as C

    payload = (FIX / "srbminer_383_athlon.json").read_text(encoding="utf-8")
    api = C._parse_api_payload(payload, "srbminer")
    assert api is not None
    assert api["total_hashrate_hs"] > 7e13
    gpus = [gpu for gpu in api["gpus"] if gpu["hashrate_hs"]]
    assert len(gpus) == 3
    # ``norm_bus`` retire le domaine PCI : ``0000:08:00.0`` → ``08:00.0``.
    assert gpus[0]["pci_bus"].endswith("08:00.0")


def test_collect_machine_applies_the_profile_api(monkeypatch, session):
    """La collecte complète lit l'API du profil, pas le seul ``ps``."""
    from app import collector as C
    from app import ssh_client

    machine, _profile = _machine_with_profile(session)
    seen = {}

    async def fake_collect(machine_, actor="system"):
        return {"ok": True, "stdout": PS}

    async def fake_api(machine_, port, api_kind=""):
        seen["port"] = port
        seen["kind"] = api_kind
        payload = (FIX / "srbminer_383_athlon.json").read_text(encoding="utf-8")
        return C._parse_api_payload(payload, api_kind), None

    async def fake_devices(db, machine_):
        return False

    async def fake_cpu(db, machine_, stdout, stats):
        return ""

    monkeypatch.setattr(ssh_client, "ssh_collect_stats", fake_collect)
    monkeypatch.setattr(C, "_fetch_miner_api", fake_api)
    monkeypatch.setattr(C, "_ensure_devices", fake_devices)
    monkeypatch.setattr(C, "_enrich_cpu_state", fake_cpu)

    import app.database as D

    original = D.SessionLocal

    class _Factory:
        """Même session : la preuve lit ce que la collecte vient d'écrire."""

        def __call__(self):
            return session

    monkeypatch.setattr(D, "SessionLocal", _Factory())
    try:
        import asyncio

        result = asyncio.run(C.collect_machine(machine.id))
    finally:
        D.SessionLocal = original

    assert result.get("online") is True, result
    assert seen == {"port": 21550, "kind": "srbminer"}
    assert result["miner"] == "srbminer"
    assert result["api_port"] == 21550


# ── 2. Push SSH de l'archive (binary_source=fossor) ────────────────────
def _stored_profile() -> dict:
    from app import binaries as B

    blob = b"\x1f\x8b" + b"archive" * 200
    uid = str(uuid.uuid4())
    saved = B.save_binary(uid, blob)
    return {
        "uuid": uid,
        "sha256": saved["sha256"],
        "binary_stored": saved["path"],
        "binary_kind": "tar.gz",
        "binary_source": "fossor",
        "software": "srbminer",
        "version": "3.8.3",
        "binary_path": "SRBMiner-MULTI",
    }


def _joined(command) -> str:
    return " ".join(command) if not isinstance(command, str) else command


@pytest.mark.asyncio
async def test_push_binary_sends_then_checks_remote_sha256():
    from app import miner_remote as R

    profile = _stored_profile()
    calls = {"put": [], "steps": []}

    async def put(machine, path, remote):
        calls["put"].append((str(path), remote))

    async def run(machine, step):
        calls["steps"].append(_joined(step.command()))
        return {"stdout": f"{profile['sha256']}  {R.install_tmp(profile)}\n",
                "status": 0, "ok": True}

    result = await R.push_binary(object(), profile, user="fossor", put=put, run=run)
    assert result["pushed"] is True
    assert result["sha256"] == profile["sha256"]
    assert calls["put"] and calls["put"][0][1] == R.install_tmp(profile)
    assert any("sha256sum" in step for step in calls["steps"])


@pytest.mark.asyncio
async def test_push_binary_refuses_a_bad_remote_sha256():
    from app import miner_remote as R

    profile = _stored_profile()
    steps = []

    async def put(machine, path, remote):
        return None

    async def run(machine, step):
        steps.append(_joined(step.command()))
        return {"stdout": "f" * 64 + "  /tmp/x\n", "status": 0, "ok": True}

    with pytest.raises(R.MinerError) as exc:
        await R.push_binary(object(), profile, user="fossor", put=put, run=run)
    assert "SHA256 différent" in str(exc.value)
    assert any("rm -f" in step for step in steps)


def test_fossor_install_plan_has_no_download_step():
    from app import miner_remote as R

    titles = [step.title for step in R.install_plan(profile_dict(), "fossor")]
    assert not any("téléchargement" in title for title in titles)
    assert "extraction" in titles and "empreinte de l'archive" in titles

    url_profile = profile_dict(binary_source="url", binary_url="https://exemple.tld/x.tar.gz")
    assert any("téléchargement" in step.title for step in R.install_plan(url_profile, "fossor"))


# ── 3. Gouverneur CPU ──────────────────────────────────────────────────
def test_cpu_governor_is_off_by_default():
    from app import miners as M

    clean = M.validate_profile(profile_dict())
    assert clean["cpu_governor"] is False
    argv = M.render_argv(clean, M.build_values(clean, gpus=[0], api_port=21550))
    assert "ExecStartPre" not in M.render_unit(clean, argv, user="fossor")


def test_cpu_governor_adds_a_root_execstartpre():
    from app import miners as M

    clean = M.validate_profile(profile_dict(cpu_governor=True))
    argv = M.render_argv(clean, M.build_values(clean, gpus=[0], api_port=21550))
    unit = M.render_unit(clean, argv, user="fossor")
    line = next(item for item in unit.splitlines() if item.startswith("ExecStartPre="))
    assert line.startswith("ExecStartPre=+-/bin/sh -c")
    assert "performance" in line and "scaling_governor" in line
    assert unit.index("ExecStartPre=") < unit.index("ExecStart=")


# ── 4. Simulation VM décidée par machine ───────────────────────────────
class _Query:
    def __init__(self, rows):
        self.rows = rows

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, vms):
        self.vms = vms
        self.commits = 0

    def query(self, model):
        if getattr(model, "__name__", "") == "SpecVm":
            return _Query(self.vms)
        return _Query([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _Backend:
    def __init__(self, seen):
        self.seen = seen

    async def collect(self, machine, model, row, profile, simulated):
        self.seen.append((row.name, simulated))
        return {"hashrate_hs": 1.5e9, "hashrate_source": "api profil"}


def _vm(name: str, machine) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, campaign_id=1, hashrate_at=None, updated_at=None,
        destroyed_at=None, mode="vm", hashrate_hs=None, hashrate_source="",
        machine=machine,
    )


@pytest.mark.asyncio
async def test_vm_hashrate_matrix_is_per_machine(monkeypatch):
    from app import campaign_runner as RUN

    armed = SimpleNamespace(name="rig-arme", simulation_disabled=True)
    unarmed = SimpleNamespace(name="rig-simule", simulation_disabled=False)
    vms = [_vm("fossor-vm-a", armed), _vm("fossor-vm-b", unarmed)]
    seen: list = []
    monkeypatch.setattr(
        "app.routers.vms._vm_backend",
        lambda db, row: (row.machine, _Backend(seen), {}),
    )
    moment = datetime.now(timezone.utc)
    await RUN.refresh_vm_hashrates(_DB(vms), moment, None)
    assert ("fossor-vm-a", False) in seen
    assert ("fossor-vm-b", True) in seen

    forced: list = []
    vms2 = [_vm("fossor-vm-c", armed), _vm("fossor-vm-d", unarmed)]
    monkeypatch.setattr(
        "app.routers.vms._vm_backend",
        lambda db, row: (row.machine, _Backend(forced), {}),
    )
    await RUN.refresh_vm_hashrates(_DB(vms2), moment, True)
    assert forced == [("fossor-vm-c", True), ("fossor-vm-d", True)]


# ── 5. Pool sans schéma ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "pool",
    [
        "stratum+tcp://pool.exemple.tld:3360",
        "stratum+ssl://pool.exemple.tld:3360",
        "pool.exemple.tld:3360",
    ],
)
def test_pool_forms_accepted(pool):
    from app import miners as M

    assert M.validate_profile(profile_dict(pool=pool))["pool"] == pool


@pytest.mark.parametrize(
    "pool",
    ["pool.exemple.tld", "ftp://pool.exemple.tld:3360", "stratum+tcp://:3360", "solo:1"],
)
def test_pool_rubbish_refused(pool):
    from app import miners as M

    with pytest.raises(M.ProfileError):
        M.validate_profile(profile_dict(pool=pool))
