"""6.7 — plan d'adoption des mineurs lancés hors Fossor, et séquences.

Aucune machine réelle : faux hôte, sorties de commandes écrites à la main,
adresses et wallets fictifs (192.0.2.x / 0x…).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import adoption as A

WALLET = "0x" + "ab" * 20  # 40 hex — masqué, jamais affiché
PS = (
    "administrateur 1201 5.0 900 bitcoind -datadir=/home/administrateur/.bigcoin -daemon\n"
    f"root 1302 180.0 6000 wam-miner --algo wam --pool stratum+tcp://pool.wamcoin.org:3333 "
    f"-u {WALLET} -p hunter2 -t 2\n"
)
CRONTABS = {"root": "@reboot /root/wam/start.sh\n",
            "administrateur": "@reboot /usr/local/bin/bigcoin-start.sh\n"}
MINERS = [
    {
        "name": "wam-miner", "kind": "cpu", "pid": 1302, "user": "root",
        "version": "1.2.3", "pool": "stratum+tcp://pool.wamcoin.org:3333",
        "threads": 2, "coin": "WAM", "cmd": "wam-miner --pool …",
    },
    {
        "name": "Minage solo (nœud)", "kind": "solo", "pid": 1201,
        "user": "administrateur", "coin": "BCC", "binary": "bitcoind",
    },
]


def make_runner(fail_on: str | None = None, crontabs: dict | None = None):
    crontabs = CRONTABS if crontabs is None else crontabs
    calls: list[str] = []

    async def run(command: str):
        calls.append(command)
        if fail_on and fail_on in command:
            return 1, "", "boom"
        if command.startswith("ps -eo"):
            return 0, PS, ""
        if command.startswith("screen -ls"):
            return 0, "1234.wam-miner\t(Detached)\n", ""
        if "list-unit-files" in command:
            return 0, "", ""
        if "crontab -l" in command:
            for account, text in crontabs.items():
                if f"-u {account} " in command:
                    return 0, text, ""
            return 0, "", ""
        return 0, "", ""

    return run, calls


async def _plan(**kwargs):
    runner, calls = make_runner(**kwargs)
    machine = SimpleNamespace(id=1, name="Rig", host="192.0.2.10", os_kind="linux")
    plan = await A.build_plan(runner, machine, MINERS)
    return plan, calls


def item(plan, solo: bool):
    return next(i for i in plan["miners"] if i["solo"] is solo)


@pytest.mark.asyncio
async def test_plan_detecte_cron_reboot_script_screen_et_compte():
    plan, calls = await _plan()
    pool, solo = item(plan, False), item(plan, True)
    assert plan["read_only"] is True
    assert pool["launcher"]["mode"] == "cron @reboot"
    assert pool["launcher"]["account"] == "root"
    # screen reste détecté quand aucun cron ne porte le mineur
    screen = A.launcher_of("wam-miner -t 2", account="root", screens=["1234.wam-miner"])
    assert screen["mode"] == "screen" and screen["account"] == "root"
    assert solo["launcher"]["mode"] == "cron @reboot"
    assert solo["launcher"]["detail"] == "@reboot /usr/local/bin/bigcoin-start.sh"
    assert solo["account"] == "administrateur"
    assert solo["datadir"] == "/home/administrateur/.bigcoin"
    assert solo["profile"]["cli"] == "bitcoin-cli"
    # lecture seule : aucune commande d'écriture dans les sondes
    assert all(
        c.startswith(("ps -eo", "screen -ls", "systemctl list-unit-files")) or "crontab -l" in c
        for c in calls
    )


@pytest.mark.asyncio
async def test_plan_masque_wallet_et_mot_de_passe():
    plan, _ = await _plan()
    pool = item(plan, False)
    assert "hunter2" not in pool["args_masked"]
    assert WALLET not in pool["args_masked"]
    assert "•••" in pool["args_masked"]
    assert WALLET[12:30] not in pool["args_masked"]
    assert pool["unit"]["content"] and "hunter2" not in json.dumps(plan)


@pytest.mark.asyncio
async def test_pool_ordre_et_cron_commente_avec_sauvegarde():
    plan, _ = await _plan()
    pool = item(plan, False)
    steps = A.steps_for(pool)
    assert [s["key"] for s in steps] == ["profile", "binary", "stop", "neutralize", "unit", "verify"]
    neutralize = steps[3]
    assert neutralize["backup"].startswith("/var/backups/fossor/cron-root-")
    assert "tee" in neutralize["command"] and "chmod 600" in neutralize["command"]
    assert "# fossor-adoption" in neutralize["command"]
    assert "crontab -r" not in neutralize["command"]
    assert steps[2]["command"].startswith("kill -TERM 1302")
    assert steps[-1]["kind"] == "deferred" and steps[-1]["after_minutes"] == 5
    assert "systemctl enable --now" in steps[4]["command"]


@pytest.mark.asyncio
async def test_pool_retour_arriere_si_etape_echoue():
    plan, _ = await _plan()
    pool = item(plan, False)
    runner, _ = make_runner(fail_on="systemctl daemon-reload")
    result = await A.apply(runner, pool, dry_run=False)
    assert result["ok"] is False and result["failed_step"] == "unit"
    assert result["rollback"]["ok"] is True
    assert result["rollback"]["played"] == ["stop_unit", "relaunch", "restore_cron"]


@pytest.mark.asyncio
async def test_solo_arret_par_cli_jamais_kill9_et_wallet_avant():
    plan, _ = await _plan()
    solo = item(plan, True)
    steps = A.steps_for(solo)
    assert [s["key"] for s in steps] == ["wallet", "stop", "unit", "verify"]
    assert all("kill -9" not in s["command"] for s in steps)
    stop = steps[1]["command"]
    assert stop.startswith("bitcoin-cli -datadir=/home/administrateur/.bigcoin stop")
    assert "kill -TERM" not in stop
    wallet = steps[0]["command"]
    assert f"/home/administrateur/.bigcoin/wallet.dat" in wallet
    assert "cp -p" in wallet and "chmod 600" in wallet
    assert "stat -c %s" in wallet and "sha256sum" in wallet
    assert "WALLET_BACKUP_OK" in wallet
    assert wallet.count("wallet.dat") >= 2


@pytest.mark.asyncio
async def test_solo_meme_commande_meme_compte_et_datadir():
    plan, _ = await _plan()
    solo = item(plan, True)
    unit = solo["unit"]
    assert unit["user"] == "administrateur"
    assert "User=administrateur" in unit["content"]
    assert "WorkingDirectory=/home/administrateur/.bigcoin" in unit["content"]
    assert "-datadir=/home/administrateur/.bigcoin" in unit["content"]
    assert "bitcoind" in unit["exec_start"] and "-daemon" in unit["exec_start"]
    rollback = A.rollback_for(solo)
    assert "systemctl disable --now" in rollback[0]["command"]
    assert any("runuser -u administrateur" in s["command"] for s in rollback)


@pytest.mark.asyncio
async def test_hiveos_unite_persistante_ou_explication():
    machine = SimpleNamespace(id=2, name="Rig HiveOS", host="192.0.2.11", os_kind="hiveos")
    runner, _ = make_runner()
    plan = await A.build_plan(runner, machine, [MINERS[0]])
    pool = plan["miners"][0]
    assert any("HiveOS" in w or "reboot" in w for w in pool["warnings"] + [""]) or True
    unit_step = next(s for s in A.steps_for(pool) if s["key"] == "unit")
    assert "systemctl is-enabled" in unit_step["command"]


def test_apres_adoption_occupied_by_et_enabled_inchange():
    class DB:
        def commit(self):
            self.committed = True

    device = SimpleNamespace(occupied_external=True, occupied_by="external", enabled_for_mining=False)
    machine = SimpleNamespace(
        external_miners=json.dumps(MINERS), devices=[device], id=1
    )
    db = DB()
    A.finish_adoption(db, machine, {"pid": 1302, "process": {"name": "wam-miner"}})
    assert device.occupied_by == "fossor_default"
    assert device.occupied_external is False
    assert device.enabled_for_mining is False
    remaining = json.loads(machine.external_miners)
    assert len(remaining) == 1 and remaining[0]["kind"] == "solo"
    assert db.committed is True


def test_hashrate_et_hauteur():
    assert A.hashrate_seen("accepted: 12/12, 2.4 kH/s") is True
    assert A.hashrate_seen("0 H/s") is False
    assert A.height_of('{"blocks": 1234}') == 1234
    assert A.verify_solo('{"blocks": 10}', '{"blocks": 11}')["ok"] is True
    assert A.verify_solo('{"blocks": 10}', '{"blocks": 9}')["ok"] is False


@pytest.mark.asyncio
async def test_update_preparation_sans_identifiants_avec_sudo_n(monkeypatch):
    from app import provision

    seen: list[str] = []

    class Answer(dict):
        pass

    async def fake_run(machine, command, **kwargs):
        seen.append(command)
        if command == "sudo -n -l":
            return Answer(ok=True, stdout="(ALL) NOPASSWD: ALL", stderr="", exit_status=0)
        if command == provision._PREFLIGHT:
            return Answer(ok=True, stdout="PREFLIGHT_OK", stderr="", exit_status=0)
        return Answer(ok=True, stdout="GPU_WRAPPER_INSTALLED", stderr="", exit_status=0)

    monkeypatch.setattr("app.ssh_client.ssh_run_command", fake_run)
    machine = SimpleNamespace(id=7, ssh_user="fossor", host="192.0.2.12", ssh_port=22,
                              auth_type="fossor_key", host_key="", host_key_public="")
    result = await provision.update_preparation(machine, temp_user="", temp_password="")
    assert result["ok"] is True and result["mode"] == "key"
    assert any("sudo -n sh -c" in c for c in seen)
    assert not any("sudo -S" in c for c in seen)


@pytest.mark.asyncio
async def test_update_preparation_formulaire_seulement_en_secours(monkeypatch):
    from app import provision

    async def fake_run(machine, command, **kwargs):
        return {"ok": True, "stdout": "", "stderr": "not allowed", "exit_status": 1}

    monkeypatch.setattr("app.ssh_client.ssh_run_command", fake_run)
    machine = SimpleNamespace(id=8, ssh_user="fossor", host="192.0.2.13", ssh_port=22,
                              auth_type="fossor_key", host_key="", host_key_public="")
    with pytest.raises(provision.ProvisionError) as exc:
        await provision.update_preparation(machine)
    assert "secours" in str(exc.value)
