"""2026.11.004-c1 — sonde du solo BIG : suivre la redirection réelle du script.

Sortie ``ps`` relevée en prod (adresse de wallet et adresse IP retirées) :

    administrator 1104500 0.0 309300 bash -c cd ~/bigcoin-mine/bigcoin && \
        nohup ./mine-bigcoin.sh > ~/bigcoin-mine/bigcoin/mine.log 2>&1 &
    administrator 1104502 0.0 309240 bash ./mine-bigcoin.sh
    administrator 1104510 1096 309240 ./bitcoind -datadir=…/bigcoin/data -daemon

``readlink /proc/1104502/fd/1`` → ``…/bigcoin-mine/bigcoin/mine.log``.  La sonde
004-a2 lisait bien le format du log, mais elle ne le trouvait pas : elle
essayait ``mining.log`` et ``miner.log`` (noms devinés) et ne suivait les
``fd`` que du démon (journal systemd).  Ici : le **script** d'abord, son
lanceur ensuite, le démon en dernier, et aucun nom de fichier deviné.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"

PS = """administrator 1104500 0.0 309300 bash -c cd ~/bigcoin-mine/bigcoin && nohup ./mine-bigcoin.sh > ~/bigcoin-mine/bigcoin/mine.log 2>&1 &
administrator 1104502 0.0 309240 bash ./mine-bigcoin.sh
administrator 1106304 0.0 308914 bash ./mine-bigcoin.sh
administrator 1104510 1096 309240 ./bitcoind -datadir=/home/administrator/bigcoin-mine/bigcoin/data -daemon
administrator 3643649 0.0 25 ./bigcoin-cli -datadir=/home/administrator/bigcoin-mine/bigcoin/data -rpcclienttimeout=3600 generatetoaddress 1 big1testaddress000000000000 18660
"""

LOG_PATH = "/home/administrator/bigcoin-mine/bigcoin/mine.log"
SCRIPT_PID = 1104502
LAUNCHER_PID = 1104500
DAEMON_PID = 1104510


def local_epoch_at(stamp: str, now: float | None = None) -> float:
    hour, minute, second = (int(part) for part in stamp.split(":"))
    base = time.localtime(time.time() if now is None else now)
    return time.mktime(
        (base.tm_year, base.tm_mon, base.tm_mday, hour, minute, second, 0, 0, -1)
    )


def test_plan_suit_script_puis_lanceur_puis_demon():
    from app.cpu_stats import plan_cpu_probes

    probes = plan_cpu_probes(PS)
    assert len(probes["nodes"]) == 1
    node = probes["nodes"][0]
    assert node["key"] == "bigcoin"
    # Le script (le plus ancien), son lanceur, le démon en dernier.
    assert node["solo_pids"]["script"][0] == SCRIPT_PID
    assert node["solo_pids"]["parent"] == [LAUNCHER_PID]
    assert node["solo_pids"]["daemon"] == [DAEMON_PID]
    assert node["pids"] == [SCRIPT_PID, LAUNCHER_PID, DAEMON_PID]
    assert [probe["pid"] for probe in probes["fds"]] == node["pids"]
    # Plus aucun nom de log deviné, ni ``*_pid`` de mineur script.
    assert probes["logs"] == []
    assert all("mining.log" not in (p.get("path") or "") for p in probes["logs"])
    assert all("miner.log" not in (p.get("path") or "") for p in probes["logs"])


def test_commande_de_sonde_suit_les_fd_dans_l_ordre():
    from app.cpu_stats import plan_cpu_probes
    from app.ssh_client import cpu_probe_command

    command = cpu_probe_command(plan_cpu_probes(PS))
    assert f"for p in {SCRIPT_PID} {LAUNCHER_PID} {DAEMON_PID}; do" in command
    assert "readlink -f /proc/$p/fd/1" in command
    assert "readlink -f /proc/$p/fd/2" in command
    assert "mining.log" not in command
    assert "miner.log" not in command
    # Le démon n'est pas sondé pour son journal avant le script.
    assert command.index(f"for p in {SCRIPT_PID}") < command.index("/bigcoin")

def probe_output(mtime: int) -> str:
    """Ce que la sonde rend sur la machine : fd/1 du script → ``mine.log``."""
    log = (FIX / "solo_big_log.txt").read_text()
    return (
        "### PROC_STAT\ncpu  1 2 3 4 5 6 7 8\n"
        "### POWER\n"
        "### CPUINFO\nprocessor : 0\ncpu MHz : 3600.000\nmodel name : AMD Ryzen 9 5950X\n"
        "### NODE bigcoin\n"
        '{"blocks": 20353, "difficulty": 0.003300662640699338, '
        '"networkhashps": 18400000000, "chain": "main"}\n'
        "### CHAIN bigcoin\n"
        '{"blocks": 20353, "chain": "main"}\n'
        "### NETPS bigcoin\n18400000000\n"
        f"### LOGI {SCRIPT_PID} {LOG_PATH}\n"
        f"{mtime}\n"
        f"{log}"
        f"### JLOG {DAEMON_PID}\n"
        "2026-11-17T21:56:00Z UpdateTip: new best=… height=20353\n"
    )


def test_bout_en_bout_du_ps_au_hashrate():
    """ps réel → sonde → sortie machine → hashrate 6952 H/s du log."""
    from app.cpu_stats import attach_cpu_hashrates, parse_cpu_report, plan_cpu_probes
    from app.external_miners import detect_solo_miners, parse_ps_rows

    probes = plan_cpu_probes(PS)
    now = local_epoch_at("21:56:30")
    report = parse_cpu_report(probe_output(int(now) - 5))

    assert report["log_pids"] == {LOG_PATH: SCRIPT_PID, f"journal:{DAEMON_PID}": DAEMON_PID}
    assert report["nodes"]["bigcoin"]["height"] == 20353

    miners = detect_solo_miners(parse_ps_rows(PS), {"bigcoin": "BIG"})
    assert len(miners) == 1 and miners[0]["coin"] == "BIG"
    attach_cpu_hashrates(miners, probes, report, now=now)

    miner = miners[0]
    assert miner["hashrate_hs"] == pytest.approx(6952.0)
    assert miner["workers"] == 11
    assert miner["algorithm"] == "yespower"
    assert miner["hashrate_source"] == "log du script (hashes/s/worker × workers)"
    assert miner["node"]["log"] == LOG_PATH
    assert miner["node"]["height"] == 20353
    assert miner["node"]["difficulty"] == pytest.approx(0.003300662640699338)
    assert miner["node"]["blocks_found"] == 0


def test_sans_fd_du_script_le_demon_ne_donne_toujours_pas_de_hashrate():
    """Régression : le journal du démon (JLOG) ne porte aucun hashrate."""
    from app.cpu_stats import attach_cpu_hashrates, parse_cpu_report, plan_cpu_probes
    from app.external_miners import detect_solo_miners, parse_ps_rows

    probes = plan_cpu_probes(PS)
    text = (
        f"### JLOG {SCRIPT_PID}\n"
        "2026-11-17T21:56:31Z UpdateTip: new best=… height=20353\n"
        f"### JLOG {DAEMON_PID}\n"
        "2026-11-17T21:56:31Z UpdateTip: new best=… height=20353\n"
    )
    report = parse_cpu_report(text)
    miners = detect_solo_miners(parse_ps_rows(PS), {"bigcoin": "BIG"})
    attach_cpu_hashrates(miners, probes, report, now=time.time())
    assert miners[0]["hashrate_hs"] is None
    assert "hashrate CPU inconnu" in miners[0]["hashrate_note"]
