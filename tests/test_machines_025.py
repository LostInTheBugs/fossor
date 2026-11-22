"""025 — la page Machines lit l'état courant, jamais une valeur périmée.

Trois règles, testées sans base réelle ni adresse réelle :

* une unité Fossor n'est jamais un « mineur externe », même si le ``ps``
  mélange le nœud BigCoin et le binaire Pearl (Ryzen) ;
* l'occupation vient de ``occupied_by``, la même source que Campagnes — le
  booléen ``occupied_external`` d'une collecte passée ne peut pas la contredire ;
* un hashrate dont la note dit « hashrate CPU inconnu » n'est jamais sommé au
  CPU (le Ryzen portait 216 TH/s, la valeur du GPU).
"""

from __future__ import annotations

from types import SimpleNamespace

from app import devices as D
from app.cpu_stats import cpu_state_with

MACHINE = SimpleNamespace(id=7, name="machine-fictive", uuid="00000000-0000-4000-8000-000000000007")


def test_unite_fossor_jamais_externe(monkeypatch):
    monkeypatch.setattr(D, "_profiles_here", lambda db, machine: [])
    entry = {
        "name": "srbminer",
        "cmd": "/opt/fossor/miners/srbminer/3.8.3/srbminer_compute --algorithm pearlhash",
        "api_port": 21550,
    }
    assert D.is_fossor_process(entry)
    assert D.filter_external_miners(None, MACHINE, [entry]) == []


def test_port_api_d_un_profil_fossor_jamais_externe(monkeypatch):
    """Le ``ps`` du Ryzen mélange bitcoind et l'unité Pearl : le port tranche."""
    monkeypatch.setattr(
        D, "_profiles_here", lambda db, machine: [SimpleNamespace(api_port=21373)]
    )
    entries = [
        {"name": "srbminer", "binary": "bitcoind", "api_port": 21373, "coin": "PRL"},
        {"name": "wam-miner", "coin": "WAM", "api_port": None},
        {"name": "Minage solo (nœud)", "coin": "KSH", "api_port": None},
    ]
    kept = D.filter_external_miners(None, MACHINE, entries)
    assert [entry["coin"] for entry in kept] == ["WAM", "KSH"]


def test_occupied_by_prime_sur_le_booleen_stocke():
    device = SimpleNamespace(
        occupied_by="fossor_default",
        occupied_campaign_id=None,
        occupied_external=True,  # valeur d'une ancienne collecte
    )
    occ = D.occupancy(device)
    assert occ["occupied_by"] == "fossor_default"
    assert occ["occupied_external"] is False


def test_campagne_fossor_affiche_son_numero():
    device = SimpleNamespace(
        occupied_by="fossor_campaign",
        occupied_campaign_id=12,
        occupied_external=False,
    )
    occ = D.occupancy(device)
    assert occ["occupied_label"] == "Campagne #12 (Fossor)"
    assert occ["occupied_external"] is False


def test_hashrate_cpu_inconnu_nest_jamais_somme():
    miner = {
        "name": "srbminer",
        "kind": "solo",
        "coin": "PRL",
        "threads": "",
        "hashrate_hs": 216162333806722.47,
        "hashrate_note": "hashrate CPU inconnu (source : nœud bigcoin, getmininginfo)",
        "node": {"key": "bigcoin", "height": 20943},
    }
    state = cpu_state_with({}, None, None, [miner])
    assert state["hashrate_hs"] is None
    assert state["hashrate_by_algo"] == []
    assert state["miners"][0]["hashrate_hs"] is None
    assert state["miners"][0]["note"].startswith("hashrate CPU inconnu")


def test_hashrate_cpu_mesure_reste_somme():
    miner = {
        "name": "cpuminer",
        "kind": "cpu",
        "coin": "KSH",
        "threads": "2",
        "hashrate_hs": 238380.0,
        "hashrate_source": "log journal:1486349",
        "hashrate_note": "",
    }
    state = cpu_state_with({}, None, None, [miner])
    assert state["hashrate_hs"] == 238380.0
    assert state["hashrate_by_algo"][0]["label"] == "KSH"
