"""6.7-fix — plan d'adoption : lanceur prouvé, chemins absolus, compte d'origine.

Aucune machine réelle : faux hôte, sorties de commandes écrites à la main,
adresses et wallets fictifs (192.0.2.x / 0x…).  Les quatre cas reproduisent la
forme des mineurs relevés en production : démon relatif lancé par cron
``@reboot``, mineur root sous cron de surveillance, démon sans lanceur trouvé,
wallet.dat exposé.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import adoption as A

WALLET = "0x" + "ab" * 20  # 40 hex — masqué, jamais affiché
BIGCOIN_DIR = "/home/administrateur/bigcoin-mine/bigcoin"

PS = (
    "administrateur 3602 1 12.0 86400 ./bitcoind -datadir=/home/administrateur/.bigcoin -daemon\n"
    "administrateur 3600 1 0.0 86000 /bin/sh /usr/local/bin/bigcoin-start.sh\n"
    "root 3400 1 180.0 7200 /opt/fossor/miners/srbminer/1.9.4/srbminer --algorithm pearlhash "
    "--pool stratum+tcp://pearl.example:3333\n"
    f"root 699 1 180.0 6000 /root/wam/wam-miner --algo wam --pool stratum+tcp://pool.wamcoin.org:3333 "
    f"-u {WALLET} -p hunter2 -t 2\n"
    f"root 4063 1 95.0 3000 /root/korsh/korshd --pool stratum+tcp://korsh.example:4444 -u {WALLET}\n"
)

PROC = (
    "PID 3602\n"
    f"EXE {BIGCOIN_DIR}/bitcoind\n"
    f"CWD {BIGCOIN_DIR}\n"
    "UNIT cron.service\n"
    "PPID 1\n"
    "CMD ./bitcoind -datadir=/home/administrateur/.bigcoin -daemon\n"
    "PID 3400\n"
    "EXE /opt/fossor/miners/srbminer/1.9.4/srbminer\n"
    "CWD /opt/fossor/miners/srbminer/1.9.4\n"
    "UNIT fossor-miner-ryzen-pearl-gpu-srbminer.service\n"
    "PPID 1\n"
    "CMD /opt/fossor/miners/srbminer/1.9.4/srbminer --algorithm pearlhash\n"
    "PID 699\n"
    "EXE /root/wam/wam-miner\n"
    "CWD /root/wam\n"
    "UNIT cron.service\n"
    "PPID 1\n"
    f"CMD /root/wam/wam-miner --algo wam -u {WALLET} -p hunter2 -t 2\n"
    "PID 4063\n"
    "EXE /root/korsh/korshd\n"
    "CWD /root/korsh\n"
    "UNIT \n"
    "PPID 1\n"
    "CMD /root/korsh/korshd --pool stratum+tcp://korsh.example:4444\n"
)

FILES = (
    "FILE /usr/local/bin/bigcoin-start.sh\n"
    "#!/bin/sh\n"
    f"cd {BIGCOIN_DIR}\n"
    "./bitcoind -datadir=/home/administrateur/.bigcoin -daemon\n"
    "EOFILE\n"
    "FILE /root/wam/wam-cron.sh\n"
    "#!/bin/sh\n"
    "pgrep -f wam-miner >/dev/null || /root/wam/wam-miner --algo wam -t 2\n"
    "EOFILE\n"
)

PROBES = (
    f"X {BIGCOIN_DIR}/bitcoin-cli\n"
    "W /home/administrateur/.bigcoin/wallet.dat 286720\n"
)

LS = (
    f"LS {BIGCOIN_DIR}\n"
    "total 8\n"
    "-rwxr-xr-x 1 administrateur administrateur 900 Jan  1 00:00 bitcoind\n"
    "-rwxr-xr-x 1 administrateur administrateur 100 Jan  1 00:00 bitcoin-cli\n"
    "LSEND\n"
)

UNITS = "fossor-miner-ryzen-pearl-gpu-srbminer.service enabled\n"

CRONTABS = {
    "administrateur": "@reboot /usr/local/bin/bigcoin-start.sh\n",
    "root": "* * * * * /root/wam/wam-cron.sh\n",
}

MINERS = [
    {
        "name": "srbminer", "kind": "gpu", "pid": 3400, "user": "root",
        "pool": "stratum+tcp://pearl.example:3333", "coin": "PRL",
        "cmd": "srbminer --algorithm pearlhash",
    },
    {
        "name": "wam-miner", "kind": "cpu", "pid": 699, "user": "root",
        "pool": "stratum+tcp://pool.wamcoin.org:3333", "threads": 2,
        "coin": "WAM", "cmd": "wam-miner --pool …",
    },
    {
        "name": "Minage solo (nœud)", "kind": "solo", "pid": 3602,
        "user": "administrateur", "coin": "BCC", "binary": "bitcoind",
    },
    {
        "name": "korshd", "kind": "cpu", "pid": 4063, "user": "root",
        "pool": "stratum+tcp://korsh.example:4444", "coin": "KORSH",
        "cmd": "korshd --pool …",
    },
]


def make_runner(
    fail_on: str | None = None,
    crontabs: dict | None = None,
    units: str = UNITS,
    probes: str = PROBES,
    found: str = "",
    ls: str = LS,
    proc: str = PROC,
):
    crontabs = CRONTABS if crontabs is None else crontabs
    calls: list[str] = []

    async def run(command: str):
        calls.append(command)
        if fail_on and fail_on in command:
            return 1, "", "boom"
        if command.startswith("ps -eo user"):
            return 0, PS, ""
        if command.startswith("screen -ls"):
            return 0, "", ""
        if "list-unit-files" in command:
            return 0, units, ""
        if "readlink /proc/" in command:
            return 0, proc, ""
        if "ls -l " in command:
            return 0, ls, ""
        if "grep -rls" in command:
            return 0, found, ""
        if "sudo -n cat" in command or "FILE " in command:
            return 0, FILES, ""
        if "test -x " in command:
            return 0, probes, ""
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


def named(plan, name: str):
    return next(i for i in plan["miners"] if i["process"]["name"] == name)


# --------------------------------------------------------------------------- #
# Cas 1 — démon relatif lancé par cron @reboot, unité Fossor voisine
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lanceur_cron_reboot_jamais_l_unite_fossor_voisine():
    plan, calls = await _plan()
    solo = named(plan, "Minage solo (nœud)")
    pearl = named(plan, "srbminer")
    # L'unité Pearl tourne : elle est attribuée au processus pearl, pas au nœud.
    assert pearl["launcher"]["mode"] == "service"
    assert pearl["launcher"]["unit"] == "fossor-miner-ryzen-pearl-gpu-srbminer.service"
    assert pearl["adoptable"] is False and "déjà lancé" in pearl["blocked_reason"]
    # Le nœud est lancé par le cron @reboot de l'administrateur, via son script.
    assert solo["launcher"]["mode"] == "cron @reboot"
    assert solo["launcher"]["account"] == "administrateur"
    assert solo["launcher"]["detail"] == "@reboot /usr/local/bin/bigcoin-start.sh"
    assert solo["launcher"]["script"] == "/usr/local/bin/bigcoin-start.sh"
    assert "script lu sur l'hôte" in solo["launcher"]["reason"]
    assert solo["adoptable"] is True and not solo["blocked_reason"]
    # Aucune étape ni retour arrière ne touche l'unité Pearl.
    commands = [s["command"] for s in A.steps_for(solo)] + [
        a["command"] for a in A.rollback_for(solo)
    ]
    assert not any("fossor-miner-ryzen-pearl" in c for c in commands), commands
    unit_step = next(s for s in A.steps_for(solo) if s["key"] == "unit")
    assert "systemctl enable --now" in unit_step["command"]


@pytest.mark.asyncio
async def test_chemins_absolus_et_cli_du_meme_repertoire():
    plan, _ = await _plan()
    solo = named(plan, "Minage solo (nœud)")
    assert solo["process"]["binary"] == f"{BIGCOIN_DIR}/bitcoind"
    assert solo["process"]["cwd"] == BIGCOIN_DIR
    assert solo["profile"]["cli"] == f"{BIGCOIN_DIR}/bitcoin-cli"
    unit = solo["unit"]
    assert unit["exec_start"].startswith(f"{BIGCOIN_DIR}/bitcoind ")
    assert "./bitcoind" not in unit["content"]
    assert "WorkingDirectory=/home/administrateur/.bigcoin" in unit["content"]
    assert "User=administrateur" in unit["content"]
    stop = next(s for s in A.steps_for(solo) if s["key"] == "stop")
    assert stop["command"].startswith(f"{BIGCOIN_DIR}/bitcoin-cli -datadir=")


@pytest.mark.asyncio
async def test_sans_cli_executable_le_mineur_est_bloque():
    plan, _ = await _plan(probes="")
    solo = named(plan, "Minage solo (nœud)")
    assert solo["adoptable"] is False
    assert "CLI" in solo["blocked_reason"] and "introuvable" in solo["blocked_reason"]


# --------------------------------------------------------------------------- #
# Cas 2 — mineur root sous cron de surveillance
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mineur_root_unite_root_et_neutralisation_avant_arret():
    plan, _ = await _plan()
    wam = named(plan, "wam-miner")
    assert wam["launcher"]["mode"] == "cron"
    assert wam["launcher"]["account"] == "root"
    assert wam["launcher"]["script"] == "/root/wam/wam-cron.sh"
    assert wam["unit"]["user"] == "root"
    assert "User=root" in wam["unit"]["content"]
    steps = A.steps_for(wam)
    assert [s["key"] for s in steps] == ["profile", "binary", "neutralize", "stop", "unit", "verify"]
    neutralize, stop = steps[2], steps[3]
    assert neutralize["backup"].startswith("/var/backups/fossor/cron-root-")
    assert "tee" in neutralize["command"] and "chmod 600" in neutralize["command"]
    assert "# fossor-adoption" in neutralize["command"]
    assert "crontab -r" not in neutralize["command"]
    # Le script de surveillance est copié puis commenté avant l'arrêt.
    assert "cp -p /root/wam/wam-cron.sh /var/backups/fossor/script-wam-cron.sh-" in neutralize["command"]
    assert any(r["kind"] == "script" and r["target"] == "/root/wam/wam-cron.sh"
               for r in neutralize["restores"])
    assert stop["command"].startswith("kill -TERM 699")


@pytest.mark.asyncio
async def test_ordre_reel_du_runner_neutralise_avant_arret_puis_retour_arriere():
    plan, _ = await _plan()
    wam = named(plan, "wam-miner")
    runner, calls = make_runner(fail_on="systemctl daemon-reload")
    result = await A.apply(runner, wam, dry_run=False)
    assert result["ok"] is False and result["failed_step"] == "unit"
    neutralize_at = next(i for i, c in enumerate(calls) if "# fossor-adoption" in c)
    stop_at = next(i for i, c in enumerate(calls) if c.startswith("kill -TERM 699"))
    unit_at = next(i for i, c in enumerate(calls) if "systemctl daemon-reload" in c)
    assert neutralize_at < stop_at < unit_at, calls
    assert result["rollback"]["ok"] is True
    assert result["rollback"]["played"] == ["stop_unit", "relaunch", "restore_script", "restore_cron"]
    restored = next(c for c in calls if "crontab /var/backups/fossor/cron-root-" in c)
    assert "-u root" in restored


@pytest.mark.asyncio
async def test_retour_arriere_relance_la_commande_absolue_du_compte_origine():
    plan, _ = await _plan()
    wam = named(plan, "wam-miner")
    actions = A.rollback_for(wam)
    relaunch = next(a for a in actions if a["key"] == "relaunch")
    assert "runuser -u root" in relaunch["command"]
    assert "cd /root/wam" in relaunch["command"]
    assert "/root/wam/wam-miner" in relaunch["command"]
    assert "./wam-miner" not in relaunch["command"]


# --------------------------------------------------------------------------- #
# Cas 3 — aucun lanceur trouvé → bloqué
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sans_lanceur_trouve_le_mineur_est_bloque_avec_la_raison():
    plan, calls = await _plan(found="")
    korsh = named(plan, "korshd")
    assert korsh["launcher"]["mode"] == "manuel"
    assert korsh["launcher"]["verified"] is False
    assert korsh["adoptable"] is False
    assert "lanceur incertain" in korsh["blocked_reason"]
    assert ("aucun lanceur identifié" in korsh["blocked_reason"] or "ne lance pas" in korsh["blocked_reason"])
    # La recherche a bien balayé HiveOS, rc.local et les unités, en lecture seule.
    search = next(c for c in calls if "grep -rls" in c)
    assert "/hive" in search and "/etc/rc.local" in search and "/hive-config" in search
    assert "korshd" in search


@pytest.mark.asyncio
async def test_lanceur_trouve_dans_rc_local_est_prouve_et_neutralise():
    inner, _calls = make_runner()

    async def run(command: str):
        if "grep -rls" in command:
            return 0, "/etc/rc.local\n", ""
        if "FILE " in command:
            return 0, "FILE /etc/rc.local\n#!/bin/sh\n/root/korsh/korshd --pool x\nEOFILE\n", ""
        return await inner(command)

    machine = SimpleNamespace(id=1, name="Rig", host="192.0.2.10", os_kind="linux")
    plan = await A.build_plan(run, machine, MINERS)
    korsh = named(plan, "korshd")
    assert korsh["launcher"]["mode"] == "script"
    assert korsh["launcher"]["script"] == "/etc/rc.local"
    assert korsh["launcher"]["verified"] is True
    neutralize = next(s for s in A.steps_for(korsh) if s["key"] == "neutralize")
    assert "/etc/rc.local" in neutralize["command"]
    assert "cp -p /etc/rc.local /var/backups/fossor/script-rc.local-" in neutralize["command"]


# --------------------------------------------------------------------------- #
# Cas 4 — wallet.dat exposé sans son contenu
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wallet_present_et_taille_sans_contenu():
    plan, _ = await _plan()
    solo = named(plan, "Minage solo (nœud)")
    assert solo["wallet_present"] is True
    assert solo["wallet_size"] == 286720
    assert solo["wallet"] == "/home/administrateur/.bigcoin/wallet.dat"
    payload = json.dumps(plan)
    assert WALLET not in payload and "hunter2" not in payload
    assert "286720" in payload


@pytest.mark.asyncio
async def test_wallet_absent_bloque_le_noeud():
    probes = f"X {BIGCOIN_DIR}/bitcoin-cli\nW /home/administrateur/.bigcoin/wallet.dat -1\n"
    plan, _ = await _plan(probes=probes)
    solo = named(plan, "Minage solo (nœud)")
    assert solo["wallet_present"] is False and solo["wallet_size"] == 0
    assert solo["adoptable"] is False
    assert "wallet.dat introuvable" in solo["blocked_reason"]


# --------------------------------------------------------------------------- #
# Garde-fou : unité fossor-miner-* existante
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unite_existante_de_meme_nom_refusee():
    plan, _ = await _plan()
    wam = named(plan, "wam-miner")
    existing = wam["unit"]["name"] + " enabled\n"
    plan2, _ = await _plan(units=existing)
    wam2 = named(plan2, "wam-miner")
    assert wam2["adoptable"] is False
    assert "serait arrêtée ou modifiée" in wam2["blocked_reason"]


def test_toute_etape_touchant_une_unite_existante_refuse_l_adoption():
    existing = "fossor-miner-ryzen-pearl-gpu-srbminer.service"
    steps = [{"key": "stop", "command": "sudo -n systemctl disable --now " + existing}]
    assert A.touches_existing_unit(steps, [existing]) == existing
    assert A.touches_existing_unit(steps, []) == ""


# --------------------------------------------------------------------------- #
# Secrets, lecture seule, reste du module
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_plan_masque_wallet_et_mot_de_passe():
    plan, _ = await _plan()
    wam = named(plan, "wam-miner")
    assert "hunter2" not in wam["args_masked"]
    assert WALLET not in wam["args_masked"]
    assert "•••" in wam["args_masked"]
    assert WALLET[12:30] not in wam["args_masked"]
    assert wam["unit"]["content"] and "hunter2" not in json.dumps(plan)


@pytest.mark.asyncio
async def test_sondes_strictement_en_lecture_seule():
    _plan_result, calls = await _plan()
    forbidden = (
        "systemctl stop", "systemctl disable", "systemctl enable", "systemctl restart",
        "kill ", "tee ", "crontab -r", "cat >", "rm -rf", "mv ", "chmod ", "sed -i",
    )
    for command in calls:
        assert not any(token in command for token in forbidden), command


@pytest.mark.asyncio
async def test_solo_arret_par_cli_jamais_kill9_et_wallet_avant():
    plan, _ = await _plan()
    solo = named(plan, "Minage solo (nœud)")
    steps = A.steps_for(solo)
    assert [s["key"] for s in steps] == ["wallet", "neutralize", "stop", "unit", "verify"]
    assert all("kill -9" not in s["command"] for s in steps)
    wallet = steps[0]["command"]
    assert "/home/administrateur/.bigcoin/wallet.dat" in wallet
    assert "cp -p" in wallet and "chmod 600" in wallet
    assert "stat -c %s" in wallet and "sha256sum" in wallet
    assert "WALLET_BACKUP_OK" in wallet
    # Le cron @reboot est neutralisé avant l'arrêt du démon.
    neutralize = steps[1]
    assert neutralize["key"] == "neutralize" and "# fossor-adoption" in neutralize["command"]
    assert neutralize["backup"].startswith("/var/backups/fossor/cron-administrateur-")


@pytest.mark.asyncio
async def test_hiveos_unite_persistante_ou_explication():
    machine = SimpleNamespace(id=2, name="Rig HiveOS", host="192.0.2.11", os_kind="hiveos")
    runner, _ = make_runner()
    plan = await A.build_plan(runner, machine, [MINERS[1]])
    pool = plan["miners"][0]
    assert any("HiveOS" in w for w in pool["warnings"])
    unit_step = next(s for s in A.steps_for(pool) if s["key"] == "unit")
    assert "systemctl is-enabled" in unit_step["command"]


def test_apres_adoption_occupied_by_et_enabled_inchange():
    class DB:
        def commit(self):
            self.committed = True

    device = SimpleNamespace(occupied_external=True, occupied_by="external", enabled_for_mining=False)
    machine = SimpleNamespace(external_miners=json.dumps(MINERS), devices=[device], id=1)
    db = DB()
    A.finish_adoption(db, machine, {"pid": 699, "process": {"name": "wam-miner"}})
    assert device.occupied_by == "fossor_default"
    assert device.occupied_external is False
    assert device.enabled_for_mining is False
    remaining = json.loads(machine.external_miners)
    assert len(remaining) == 3 and all(entry["name"] != "wam-miner" for entry in remaining)
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


# --------------------------------------------------------------------------- #
# 6.7-sondes — sudo -n obligatoire, CLI du nœud résolu dans son répertoire
# --------------------------------------------------------------------------- #
NUD = (
    "SUDO denied\n"
    "PID 3602\n"
    "EXE \n"
    "CWD \n"
    "UNIT \n"
    "PPID 1\n"
    "CMD ./bitcoind -datadir=/home/administrateur/.bigcoin -daemon\n"
)

LS_UNIQUE = (
    f"LS {BIGCOIN_DIR}\n"
    "total 8\n"
    "-rwxr-xr-x 1 administrateur administrateur 900 Jan  1 00:00 bitcoind\n"
    "-rwxr-xr-x 1 administrateur administrateur 100 Jan  1 00:00 bigcoin-cli\n"
    "LSEND\n"
)
PROBES_UNIQUE = f"X {BIGCOIN_DIR}/bigcoin-cli\nW /home/administrateur/.bigcoin/wallet.dat 286720\n"
LS_MANY = (
    f"LS {BIGCOIN_DIR}\n"
    "-rwxr-xr-x 1 administrateur administrateur 900 Jan  1 00:00 bitcoind\n"
    "-rwxr-xr-x 1 administrateur administrateur 100 Jan  1 00:00 alpha-cli\n"
    "-rwxr-xr-x 1 administrateur administrateur 100 Jan  1 00:00 beta-cli\n"
    "LSEND\n"
)
LS_NONE = (
    f"LS {BIGCOIN_DIR}\n"
    "-rwxr-xr-x 1 administrateur administrateur 900 Jan  1 00:00 bitcoind\n"
    "LSEND\n"
)


async def test_proc_sans_sudo_bloque_droits_insuffisants():
    plan, _calls = await _plan(proc=NUD)
    solo = next(m for m in plan["miners"] if m["pid"] == 3602)
    assert solo["adoptable"] is False
    assert "droits insuffisants" in solo["blocked_reason"]


async def test_cli_unique_du_repertoire_retenu():
    plan, _calls = await _plan(ls=LS_UNIQUE, probes=PROBES_UNIQUE)
    solo = next(m for m in plan["miners"] if m["pid"] == 3602)
    assert solo["profile"]["cli"] == f"{BIGCOIN_DIR}/bigcoin-cli"
    assert solo["adoptable"] is True


async def test_plusieurs_cli_bloquent_le_noeud():
    plan, _calls = await _plan(ls=LS_MANY, probes="")
    solo = next(m for m in plan["miners"] if m["pid"] == 3602)
    assert solo["adoptable"] is False
    assert "plusieurs" in solo["blocked_reason"]


async def test_aucun_cli_bloque_le_noeud_sans_preuve():
    plan, _calls = await _plan(ls=LS_NONE, probes="")
    solo = next(m for m in plan["miners"] if m["pid"] == 3602)
    assert solo["adoptable"] is False
    assert "aucun *-cli" in solo["blocked_reason"]
    assert "RPC stop" in solo["blocked_reason"]
