"""Régressions 026 : santé du CPU exclu et mineur externe BigCoin du Ryzen."""

from __future__ import annotations

from types import SimpleNamespace

from app import devices as devices_mod
from app.collector import _apply_profile_api
from app.devices import filter_external_miners
from app.health import evaluate
from app.srbminer_api import expand_miner_algos
from app.stats_parser import parse_collect_stats

# ps réel de la collecte du Ryzen (025), données fictives : bitcoind de
# l'administrateur + lanceur `script` + srbminer_compute de l'unité Fossor.
PS_RYZEN = """### PS_MINERS
administrator 3602 994 86400 ./bitcoind -datadir=/home/administrator/.bigcoin -daemon
fossor 1197806 0.0 900 /usr/bin/script -qfec /opt/fossor/miners/srbminer/3.8.3/srbminer_compute --algorithm pearlhash --pool pearl-eu2.luckypool.io:3360 --wallet prl1fictif000000000000000000000000000000000000000000000000 --password x --api-port 21373 /dev/null
fossor 1197807 1.6 890 /opt/fossor/miners/srbminer/3.8.3/srbminer_compute --algorithm pearlhash --pool pearl-eu2.luckypool.io:3360 --wallet prl1fictif000000000000000000000000000000000000000000000000 --password x --api-port 21373
"""

ALGOS = [
    {
        "algo": "pearlhash",
        "coin": "PRL",
        "pool": "pearl-eu2.luckypool.io:3360",
        "hashrate_hs": 216e12,
    }
]


def _ryzen_stats():
    stats = parse_collect_stats(PS_RYZEN)
    profile = SimpleNamespace(software="srbminer", api_kind="srbminer", api_port=21373)
    stats = _apply_profile_api(stats, profile)
    stats["miners"] = expand_miner_algos(stats["miners"], ALGOS, {})
    return stats


def test_ps_ryzen_bigcoin_reste_un_noeud_solo(monkeypatch):
    stats = _ryzen_stats()
    solo = [m for m in stats["miners"] if m.get("kind") == "solo"]
    assert len(solo) == 1
    bigcoin = solo[0]
    assert bigcoin["binary"] == "bitcoind"
    assert bigcoin["user"] == "administrator"
    assert bigcoin["cpu_pct"] == 994
    assert bigcoin["coin"] == "BIG"
    assert bigcoin["pool"] == ""
    assert bigcoin["api_port"] is None
    assert bigcoin["algorithm"] == ""
    for key in ("pool", "algorithm", "cmd"):
        assert "pearl" not in str(bigcoin.get(key) or "").lower()

    monkeypatch.setattr(
        devices_mod,
        "_profiles_here",
        lambda db, machine: [SimpleNamespace(software="srbminer", api_port=21373)],
    )
    kept = filter_external_miners(None, SimpleNamespace(id=1), stats["miners"])
    assert [(m["name"], m["binary"], m["kind"]) for m in kept] == [
        ("Minage solo (nœud)", "bitcoind", "solo")
    ]


def test_solo_bigcoin_yespower_garde_avec_un_profil_pearl(monkeypatch):
    """Le nœud BigCoin yespower n'est jamais « contaminé » par un profil Pearl."""
    monkeypatch.setattr(
        devices_mod,
        "_profiles_here",
        lambda db, machine: [
            SimpleNamespace(
                software="srbminer",
                api_port=21373,
                coin="PRL",
                pool="pearl-eu2.luckypool.io:3360",
            )
        ],
    )
    entry = {
        "name": "Minage solo (nœud)",
        "binary": "bitcoind",
        "kind": "solo",
        "coin": "BIG",
        "algorithm": "yespower",
        "pool": "",
        "api_port": None,
    }
    kept = filter_external_miners(None, SimpleNamespace(id=1), [entry])
    assert kept == [entry]


def test_sans_profil_sur_la_machine_rien_nest_ecarte(monkeypatch):
    """Sans profil Fossor ici, un solo porteur d'algo reste un mineur externe."""
    monkeypatch.setattr(devices_mod, "_profiles_here", lambda db, machine: [])
    entry = {
        "name": "Minage solo (nœud)",
        "binary": "bitcoind",
        "kind": "solo",
        "coin": "BIG",
        "algorithm": "yespower",
        "api_port": None,
    }
    assert filter_external_miners(None, SimpleNamespace(id=1), [entry]) == [entry]


def _machine(**overrides):
    machine = {
        "collected": True,
        "online": True,
        "api_error": None,
        "devices": [
            {
                "kind": "cpu",
                "enabled_for_mining": False,
                "occupied_by": "external",
                "occupied_external": True,
                "hashrate_hs": None,
                "temp_c": 55,
            },
            {
                "kind": "gpu",
                "index": 0,
                "enabled_for_mining": True,
                "occupied_by": "fossor_default",
                "occupied_external": False,
                "hashrate_hs": 5.0e6,
                "temp_c": 60,
            },
        ],
        "cpu": {
            "miners": [
                {"name": "srbminer", "kind": "solo", "coin": "BIG", "hashrate_hs": None}
            ]
        },
        "total_hashrate_hs": 5.0e6,
    }
    machine.update(overrides)
    return machine


def test_ryzen_cpu_exclu_et_externe_ne_degrade_pas_la_sante():
    result = evaluate(_machine())
    assert result["status"] == "green"


def test_cpu_fossor_qui_ne_mine_pas_reste_orange():
    machine = _machine(
        devices=[
            {
                "kind": "cpu",
                "enabled_for_mining": True,
                "occupied_by": "fossor_default",
                "occupied_external": False,
                "hashrate_hs": None,
                "temp_c": 50,
            }
        ],
        cpu={
            "miners": [
                {"name": "cpuminer", "kind": "cpu", "coin": "KSH", "hashrate_hs": None}
            ]
        },
        total_hashrate_hs=None,
    )
    result = evaluate(machine)
    assert result["status"] == "orange"
    assert result["reason"] == "CPU sans hashrate"


def test_athlon_deux_unites_fossor_sante_verte():
    machine = _machine(
        devices=[
            {
                "kind": "cpu",
                "enabled_for_mining": True,
                "occupied_by": "fossor_default",
                "occupied_external": False,
                "hashrate_hs": 2.5e6,
                "temp_c": 50,
            },
            {
                "kind": "gpu",
                "index": 0,
                "enabled_for_mining": True,
                "occupied_by": "fossor_default",
                "occupied_external": False,
                "hashrate_hs": 5.0e6,
                "temp_c": 60,
            },
        ],
        cpu={
            "miners": [
                {"name": "cpuminer", "kind": "cpu", "hashrate_hs": 2.5e6},
                {"name": "srbminer", "kind": "cpu", "hashrate_hs": 5.0e6},
            ]
        },
        external_miners=[],
        total_hashrate_hs=7.5e6,
    )
    assert evaluate(machine)["status"] == "green"
