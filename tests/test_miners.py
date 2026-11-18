"""Étape 3a — profils de minage, gabarit, unité systemd, simulation, refus GPU."""

import pytest

from app import miner_remote as R
from app import miners as M

WALLET = "prl1demo000000000000000000000000000000000000"


def profile(**over):
    base = dict(
        name="Pearl SRBMiner",
        coin="PRL",
        wallet=WALLET,
        pool="stratum+tcp://pool.exemple.tld:3360",
        software="srbminer",
        binary_url="https://exemple.tld/srbminer-2.4.7.tar.gz",
        sha256="a" * 64,
        binary_path="SRBMiner-MULTI",
        version="2.4.7",
        cmdline=(
            "/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI --algorithm pearl "
            "--pool {pool} --wallet {wallet} --worker {worker} --gpu-id {gpus} "
            "--api-port {api_port}"
        ),
        api_port=21373,
        execution_mode="host",
    )
    base.update(over)
    return M.validate_profile(base)


class FakeMachine:
    id = 7
    name = "cible-jetable"
    host = "cible.test"
    ssh_port = 22
    ssh_user = "fossor"
    auth_type = "fossor_key"
    secret_ref = None
    host_key = None
    host_key_public = None


# ── 1. Validation de chaque variable, injections refusées ────────────
BAD_WALLETS = [
    "x;rm -rf /",
    "wallet$(id)",
    "wallet`id`",
    "wallet with space",
    "wallet&&id",
    "wallet\nid",
    "court",
    "a" * 129,
    "wallet'quote",
    "wallet|cat",
]
BAD_POOLS = [
    "stratum+tcp://pool.exemple.tld:1",
    "http://pool.exemple.tld:3333",
    "stratum+tcp://pool.exemple.tld:3333; rm -rf /",
    "solo; rm -rf /",
    "stratum+tcp://pool exemple.tld:3333",
    "$(id)",
]


@pytest.mark.parametrize("wallet", BAD_WALLETS)
def test_wallet_injection_refused(wallet):
    with pytest.raises(M.ProfileError):
        profile(wallet=wallet)


@pytest.mark.parametrize("pool", BAD_POOLS)
def test_pool_injection_refused(pool):
    with pytest.raises(M.ProfileError):
        profile(pool=pool)


@pytest.mark.parametrize(
    "gpus", ["0,1;id", "0 1", "a,b", "-1", "0,,1", "$(id)", "0,1\n"]
)
def test_gpus_pattern(gpus):
    with pytest.raises(M.ProfileError):
        M._validated("gpus", gpus)


@pytest.mark.parametrize("worker", ["rig-1", "RIG_01", "a", "x" * 33, "rig 1", "rig;1", "rig$(id)"])
def test_worker_pattern(worker):
    if worker in ("rig-1", "RIG_01", "a"):
        assert M._validated("worker", worker) == worker
    else:
        with pytest.raises(M.ProfileError):
            M._validated("worker", worker)


def test_template_rejects_unknown_variable_and_shell_chars():
    with pytest.raises(M.ProfileError):
        profile(cmdline=profile()["cmdline"] + " --x {evil}")
    with pytest.raises(M.ProfileError):
        profile(cmdline=profile()["cmdline"] + " ; id")
    with pytest.raises(M.ProfileError):
        profile(cmdline=profile()["cmdline"] + " $(id)")
    with pytest.raises(M.ProfileError):
        profile(cmdline=profile()["cmdline"] + " `id`")
    with pytest.raises(M.ProfileError):
        profile(cmdline=profile()["cmdline"] + " && id")


def test_first_token_must_be_the_installed_binary():
    with pytest.raises(M.ProfileError):
        profile(cmdline="/usr/bin/curl {pool} {wallet}")

    with pytest.raises(M.ProfileError):
        profile(cmdline="sh -c {wallet}")


def test_sha256_is_mandatory_and_hex():
    for value in ("", "abc", "z" * 64):
        with pytest.raises(M.ProfileError):
            profile(sha256=value)
    # Case is normalised, the stored value is always lowercase hex.
    assert profile(sha256="A" * 64)["sha256"] == "a" * 64


def test_binary_path_cannot_escape_its_directory():
    for value in ("../bin/x", "/etc/passwd", "a/../../b"):
        with pytest.raises(M.ProfileError):
            profile(binary_path=value)


def test_vm_and_hybrid_modes_accepted_since_3c():
    """§4.6 (3c) : ``vm`` et ``hybrid`` remplacent la liste « plus tard »."""
    assert profile(execution_mode="vm")["execution_mode"] == "vm"
    hybrid = profile(
        execution_mode="hybrid",
        cmdline=(
            "/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI "
            "--algorithm pearl --wallet {wallet} --pool {vm_node} --gpu-id={gpus}"
        ),
    )
    assert hybrid["execution_mode"] == "hybrid"


def test_vm_node_only_in_hybrid_mode():
    """``{vm_node}`` n'a de sens qu'en hybride, et y est obligatoire."""
    with pytest.raises(M.ProfileError) as exc:
        profile(
            execution_mode="host",
            cmdline=(
                "/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI "
                "--wallet {wallet} --pool {vm_node} --gpu-id={gpus}"
            ),
        )
    assert "hybride" in str(exc.value)
    with pytest.raises(M.ProfileError):
        profile(execution_mode="hybrid")


# ── 2. shlex puis substitution : argv, jamais une chaîne ─────────────
def test_shlex_split_then_substitution_keeps_one_argument():
    p = profile(
        cmdline=(
            "/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI "
            "--algorithm 'pearl coin' --wallet {wallet} --gpu-id={gpus}"
        )
    )
    argv = M.render_argv(p, M.build_values(p, worker="rig-1", gpus=[0, 1], api_port=21373))
    assert argv == [
        "/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI",
        "--algorithm",
        "pearl coin",
        "--wallet",
        WALLET,
        "--gpu-id=0,1",
    ]
    assert isinstance(argv, list)


def test_unbalanced_quote_is_refused():
    with pytest.raises(M.ProfileError):
        profile(cmdline='/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI --wallet "{wallet}', )


def test_values_are_checked_after_substitution():
    p = profile()
    with pytest.raises(M.ProfileError):
        M.render_argv(p, {**M.build_values(p, worker="rig", gpus=[0]), "wallet": "x;rm -rf /"})


def test_gpus_required_when_template_uses_them():
    p = profile()
    with pytest.raises(M.ProfileError):
        M.build_values(p, worker="rig", gpus=[], api_port=21373)


def test_cpu_profile_without_gpus():
    p = profile(
        software="xmrig",
        binary_path="xmrig",
        version="6.21.0",
        sha256="b" * 64,
        cmdline=(
            "/opt/fossor/miners/xmrig/6.21.0/xmrig --url {pool} --user {wallet} "
            "--rig-id {worker} --threads {threads}"
        ),
        api_port=0,
    )
    values = M.build_values(p, worker="rig-1", threads=4)
    argv = M.render_argv(p, values)
    assert "--threads" in argv and "4" in argv
    assert "{gpus}" not in " ".join(argv)


# ── 3. Unité systemd figée ───────────────────────────────────────────
def test_systemd_unit_frozen_content():
    p = profile()
    argv = M.render_argv(p, M.build_values(p, worker="rig-1", gpus=[0, 1], api_port=21373))
    unit = M.render_unit(p, argv, user="fossor")
    expected = (
        "[Unit]\n"
        "Description=Fossor miner pearl-srbminer (PRL)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=fossor\n"
        "WorkingDirectory=/opt/fossor/miners/srbminer/2.4.7\n"
        'ExecStart="/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI" "--algorithm" '
        '"pearl" "--pool" "stratum+tcp://pool.exemple.tld:3360" "--wallet" '
        f'"{WALLET}" "--worker" "rig-1" "--gpu-id" "0,1" "--api-port" "21373"\n'
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "KillSignal=SIGTERM\n"
        "TimeoutStopSec=30\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    assert unit == expected


def test_unit_name_and_paths():
    p = profile(name="Pearl SRBMiner")
    assert M.unit_name(p) == "fossor-miner-pearl-srbminer.service"
    assert M.pid_file(p) == "/opt/fossor/run/pearl-srbminer.pid"
    assert M.binary_path(p) == "/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI"


def test_systemd_quote_escapes_percent_and_quotes():
    p = profile()
    argv = M.render_argv(p, M.build_values(p, worker="rig-1", gpus=[0], api_port=21373))
    assert M.systemd_quote('a"b') == '"a\\"b"'
    assert M.systemd_quote("50%") == '"50%%"'
    assert M.render_unit(p, argv, user="fossor").count("ExecStart=") == 1


# ── 4. Simulation : aucun appel SSH, liste exacte des commandes ──────
async def test_simulation_calls_no_runner(monkeypatch):
    p = profile()
    argv = M.render_argv(p, M.build_values(p, worker="rig-1", gpus=[0, 1], api_port=21373))

    async def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("appel SSH pendant la simulation")

    monkeypatch.setattr(R, "RUNNER", boom)
    out = await R.start(FakeMachine(), p, argv, simulated=True, user="fossor")
    assert out["simulated"] is True
    assert out["commands"]
    assert any("sha256sum" in c for c in out["commands"])
    assert any("curl -fsSL" in c or "wget" in c for c in out["commands"])
    assert out["fallback_commands"]

    stop = await R.stop(FakeMachine(), p, simulated=True, user="fossor")
    assert stop["simulated"] is True and stop["commands"]

    status = await R.status(FakeMachine(), p, simulated=True, user="fossor")
    assert status["simulated"] is True and status["commands"]


# ── 5. Refus quand les GPU sont occupées ─────────────────────────────
class Recorder:
    def __init__(self, gpu_line="", probe=None):
        self.commands = []
        self.gpu_line = gpu_line
        self.probe = probe or "TOOL:tar\nTOOL:sha256sum\nTOOL:curl\nINIT:systemd\n"

    async def __call__(self, machine, command, stdin=None, timeout=0):
        self.commands.append(command)
        if "TOOL:" in command:
            return {"status": 0, "stdout": self.probe, "ok": True}
        if "query-compute-apps" in command:
            return {"status": 0, "stdout": self.gpu_line, "ok": True}
        if "ps -eo pid=,args=" in command:
            return {"status": 0, "stdout": "", "ok": True}
        return {"status": 0, "stdout": "", "ok": True}


async def test_start_refused_when_gpu_busy(monkeypatch):
    argv = ["/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI", "--wallet", WALLET]
    recorder = Recorder(gpu_line="4242, /usr/bin/trex, 512 MiB\n")
    monkeypatch.setattr(R, "RUNNER", recorder)
    with pytest.raises(R.MinerError) as exc:
        await R.start(FakeMachine(), profile(), argv, simulated=False, user="fossor")
    assert "occupe les GPU" in str(exc.value)
    assert not any("tar -xzf" in c for c in recorder.commands)


async def test_start_refused_when_foreign_miner_in_ps(monkeypatch):
    argv = ["/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI"]
    recorder = Recorder()
    original = recorder.__call__

    async def with_ps(machine, command, stdin=None, timeout=0):
        result = await original(machine, command, stdin, timeout)
        if "ps -eo pid=,args=" in command:
            result["stdout"] = "777 /usr/bin/lolminer --algo PEARL\n"
        return result

    monkeypatch.setattr(R, "RUNNER", with_ps)
    with pytest.raises(R.MinerError) as exc:
        await R.start(FakeMachine(), profile(), argv, simulated=False, user="fossor")
    assert "occupe les GPU" in str(exc.value)


async def test_bad_sha256_extracts_nothing(monkeypatch):
    argv = ["/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI"]
    recorder = Recorder()
    original = recorder.__call__

    async def bad_hash(machine, command, stdin=None, timeout=0):
        result = await original(machine, command, stdin, timeout)
        if "sha256sum" in command:
            result["stdout"] = "f" * 64 + "  /tmp/fossor-aaaaaaaaaaaa.tar.gz\n"
        return result

    monkeypatch.setattr(R, "RUNNER", bad_hash)
    with pytest.raises(R.MinerError) as exc:
        await R.start(FakeMachine(), profile(), argv, simulated=False, user="fossor")
    assert "SHA256 différent" in str(exc.value)
    assert not any("tar -xzf" in c for c in recorder.commands)
    assert any("rm -f" in c for c in recorder.commands)


async def test_stop_refuses_a_foreign_pid(monkeypatch):
    recorder = Recorder()
    original = recorder.__call__

    async def with_pid(machine, command, stdin=None, timeout=0):
        result = await original(machine, command, stdin, timeout)
        if "cat /opt/fossor/run/" in command:
            result["stdout"] = "31337\n"
        if "ps -p 31337" in command:
            result["stdout"] = "/usr/bin/other-miner --pool x\n"
        return result

    monkeypatch.setattr(R, "RUNNER", with_pid)
    with pytest.raises(R.MinerError) as exc:
        await R.stop(FakeMachine(), profile(), simulated=False, user="fossor")
    assert "n'a pas été lancé par Fossor" in str(exc.value)
    assert not any("kill -TERM 31337" in c for c in recorder.commands)


# ── 6. API : simulation par défaut, confirmation, audit ──────────────
def _body(**over):
    base = dict(
        name="Pearl SRBMiner",
        coin="PRL",
        wallet=WALLET,
        pool="stratum+tcp://pool.exemple.tld:3360",
        software="srbminer",
        binary_url="https://exemple.tld/srbminer-2.4.7.tar.gz",
        sha256="a" * 64,
        binary_path="SRBMiner-MULTI",
        version="2.4.7",
        cmdline=(
            "/opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI --algorithm pearl "
            "--pool {pool} --wallet {wallet} --worker {worker} --gpu-id {gpus} "
            "--api-port {api_port}"
        ),
        api_port=21373,
        execution_mode="host",
    )
    base.update(over)
    return base


async def test_simulation_is_on_by_default(client):
    r = await client.get("/api/miner/settings")
    assert r.status_code == 200
    assert r.json()["simulation"] is True


async def test_disabling_simulation_requires_the_word(client):
    r = await client.put("/api/miner/settings", json={"simulation": False})
    assert r.status_code == 400
    assert "PILOTER" in r.json()["detail"]
    assert (await client.get("/api/miner/settings")).json()["simulation"] is True

    r = await client.put("/api/miner/settings", json={"simulation": False, "confirm": "piloter"})
    assert r.status_code == 400

    r = await client.put("/api/miner/settings", json={"simulation": False, "confirm": "PILOTER"})
    assert r.status_code == 200
    assert r.json()["simulation"] is False

    r = await client.put("/api/miner/settings", json={"simulation": True})
    assert r.json()["simulation"] is True


async def test_profile_with_injected_wallet_is_refused(client):
    r = await client.post("/api/miner/profiles", json=_body(wallet="x;rm -rf /"))
    assert r.status_code == 400
    assert "wallet" in r.json()["detail"]


async def test_profile_crud_and_default_token(client):
    r = await client.post("/api/miner/profiles", json=_body())
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["service"] == "fossor-miner-pearl-srbminer.service"

    r = await client.get("/api/miner/profiles")
    assert len(r.json()) == 1

    r = await client.put("/api/miner/settings", json={"default_profile_id": created["id"]})
    assert r.status_code == 200
    assert r.json()["default_profile_id"] == created["id"]

    r = await client.get(f"/api/miner/profiles/{created['id']}")
    assert r.json()["coin"] == "PRL"

    r = await client.delete(f"/api/miner/profiles/{created['id']}")
    assert r.status_code == 200
    assert (await client.get("/api/miner/settings")).json()["default_profile_id"] is None


async def test_start_simulated_through_the_api(client, monkeypatch):
    from app.database import SessionLocal
    from app.models import Device

    async def boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("appel SSH pendant la simulation")

    monkeypatch.setattr(R, "RUNNER", boom)
    r = await client.post("/api/machines", json={
        "name": "cible-jetable", "host": "cible.test", "ssh_port": 22,
        "ssh_user": "fossor", "auth_type": "fossor_key", "sudo": "sudo without password",
    })
    machine = r.json()
    db = SessionLocal()
    try:
        db.add(Device(uuid="11111111-1111-1111-1111-111111111111", machine_id=machine["id"],
                      kind="gpu", index=0, model="GPU test", present=True, enabled_for_mining=True))
        db.add(Device(uuid="22222222-2222-2222-2222-222222222222", machine_id=machine["id"],
                      kind="gpu", index=1, model="GPU test 2", present=True, enabled_for_mining=True))
        db.commit()
    finally:
        db.close()

    created = (await client.post("/api/miner/profiles", json=_body())).json()
    r = await client.post(f"/api/machines/{machine['id']}/miner/start", json={"profile_id": created["id"]})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["simulated"] is True
    assert payload["argv"][-1] == "21373"
    assert any("sha256sum" in c for c in payload["commands"])
    assert "aucune commande" in payload["note"]

    r = await client.get(f"/api/machines/{machine['id']}/miner/status?profile_id={created['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["simulated"] is True

    r = await client.post(
        f"/api/machines/{machine['id']}/miner/stop", json={"profile_id": created["id"]}
    )
    assert r.json()["simulated"] is True

    from app.models import AuditLog
    db = SessionLocal()
    try:
        actions = [a.action for a in db.query(AuditLog).all()]
    finally:
        db.close()
    assert "miner-start" in actions and "miner-stop" in actions


async def test_start_requires_fossor_key(client):
    r = await client.post("/api/machines", json={
        "name": "mdp", "host": "mdp.test", "auth_type": "password", "secret": "x",
    })
    machine = r.json()
    created = (await client.post("/api/miner/profiles", json=_body())).json()
    r = await client.post(f"/api/machines/{machine['id']}/miner/start", json={"profile_id": created["id"]})
    assert r.status_code == 400
    assert "fossor_key" in r.json()["detail"] or "clé Fossor" in r.json()["detail"]


def test_parse_hashrate_shapes():
    assert R.parse_hashrate('{"hashrate": 1234567}') == 1234567
    assert R.parse_hashrate('{"hashrate": {"total": [42.5, "MH/s"]}}') == 42.5
    assert R.parse_hashrate('{"total_hashrate": "7.5 MH/s"}') == 7.5
    assert R.parse_hashrate("not json") is None
    assert R.parse_hashrate("") is None


def test_parse_probe_detects_systemd():
    probe = R.parse_probe("TOOL:tar\nTOOL:systemctl\nMISSING:wget\nINIT:systemd\n")
    assert probe["systemd"] is True
    assert "wget" in probe["missing"]
    assert R.parse_probe("TOOL:tar\nINIT:bash\n")["systemd"] is False


def test_parse_occupancy_ignores_our_own_tree():
    ours = "42 /opt/fossor/miners/srbminer/2.4.7/SRBMiner-MULTI --wallet x\n"
    assert R.parse_occupancy("", ours) == []
    other = R.parse_occupancy("", "43 /usr/bin/lolminer --algo PEARL\n")
    assert other and other[0]["pid"] == "43"
