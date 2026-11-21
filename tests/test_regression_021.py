"""Non-régression 021 — mineurs lancés par Fossor et relais d'API.

L'athlon exécute deux unités Fossor sous le compte SSH ``fossor`` : un cpuminer
sans API (KSH) et un SRBMiner avec API (PRL). La collecte écartait toute ligne
de ce compte, donc le cpuminer disparaissait (``hashrate_by_algo=[]``) et le
relais SSH interrogeait ``/`` au lieu de ``/summary``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.cpu_stats import attach_cpu_hashrates, cpu_state_with, plan_cpu_probes
from app.external_miners import detect_running_miners
from app.ssh_client import WHITELISTED_COMMANDS

PS = """USER PID %CPU ELAPSED COMMAND
fossor 1486349 198 66651 /opt/fossor/miners/custom/1.0/cpuminer-korsh --algo yespower --param-n 256 --param-r 8 --url stratum+tcp://pool.korsh.org:3333 --user kshtestaddressfossor000000000000000000000.athlon-korsh --threads 2
fossor 1487634 6.2 66628 /opt/fossor/miners/srbminer/3.8.3/srbminer_compute --algorithm pearlhash --wallet prl1ptestaddressfossor00000000000000000000000000000000000000000 --worker athlon-3000g --pool stratum+tcp://pearl-eu2.luckypool.io:3360 --api-enable --api-port 21550
"""

HOSTS = {"pool.korsh.org": "KSH", "pearl-eu2.luckypool.io": "PRL"}
JOURNAL = """[2026-11-21 08:40:01] TTF @ 231.76 Mh/s
[2026-11-21 08:40:01] Net hash rate (est) 3.33 MH/s
"""


def _miners():
    return detect_running_miners(
        PS, coin_hosts=HOSTS, coin_tickers=["KSH", "PRL"]
    )


def test_unites_fossor_detectees_meme_compte_ssh():
    """Un processus du compte ``fossor`` est un mineur, pas la sonde."""
    result = _miners()
    by_binary = {m["binary"]: m for m in result["miners"]}
    assert "cpuminer-korsh" in by_binary, result["miners"]
    assert "srbminer_compute" in by_binary, result["miners"]
    assert by_binary["cpuminer-korsh"]["kind"] == "cpu"
    assert by_binary["srbminer_compute"]["kind"] == "gpu"
    # Le port d'API vient du srbminer, jamais du cpuminer qui n'en a pas.
    assert result["api_port"] == 21550
    assert by_binary["cpuminer-korsh"]["api_port"] in (None, 0)


def test_hashrate_cpu_lu_dans_le_journal_de_lunite():
    """Profil CPU sans API : le journal de l'unité donne des kH/s, pas des Mh/s."""
    miners = _miners()["miners"]
    probes = {
        "logs": [],
        "apis": [],
        "nodes": {},
        "fds": [{"pid": 1486349, "family": "cpuminer"}],
    }
    now = time.time()
    report = {
        "state": {},
        "apis": {},
        "nodes": {},
        "logs": {"journal:1486349": {"text": JOURNAL, "mtime": int(now)}},
        "log_pids": {"journal:1486349": 1486349},
    }
    attach_cpu_hashrates(miners, probes, report, now=now)
    cpu = cpu_state_with(
        report, None, None, [m for m in miners if m["kind"] == "cpu"]
    )
    assert 231_000 < cpu["hashrate_hs"] < 232_500, cpu["hashrate_hs"]
    assert [item["label"] for item in cpu["hashrate_by_algo"]] == ["KSH"]
    assert "kH/s" in cpu["hashrate_label"]


def test_relais_ssh_interroge_summary_du_port_du_profil():
    command = WHITELISTED_COMMANDS["miner_api"].format(port=21550)
    first = command.split("||")[0]
    assert "127.0.0.1:21550/summary" in first, command
    assert "curl -sf" in first, command


@pytest.mark.asyncio
async def test_controle_api_utilise_le_port_demande(monkeypatch):
    """Le contrôle direct puis le relais reçoivent le port du profil (21550)."""
    from app import collector
    import app.ssh_client as S

    seen: dict[str, int] = {}

    async def direct(host, port):
        seen["direct"] = port
        return None

    async def relay(machine, port):
        seen["relay"] = port
        return {"ok": False, "error": "API du mineur injoignable"}

    monkeypatch.setattr(collector, "fetch_srbminer", direct)
    monkeypatch.setattr(S, "ssh_miner_api", relay)
    api, error = await collector._fetch_miner_api(
        SimpleNamespace(host="192.0.2.10"), 21550, "srbminer"
    )
    assert seen == {"direct": 21550, "relay": 21550}
    assert api is None and "injoignable" in error


def test_analyse_sans_candidat_refusee_sans_detail():
    from app import analyses as A

    with pytest.raises(A.AnalysisError):
        A.parse_document({"analyses": [{"ts": "2026-11-21T08:00:00Z", "candidates": []}]})
    entries = A.parse_document(
        {
            "analyses": [
                {
                    "ts": "2026-11-21T08:00:00Z",
                    "candidates": [],
                    "reason": "aucun candidat ne passe la grille",
                }
            ]
        }
    )
    assert entries[0]["detail"] == "aucun candidat ne passe la grille"
