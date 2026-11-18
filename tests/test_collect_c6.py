"""Correctif 2026.11.003-c6 — base vide, coin déduit, SRBMiner 3.8.3.

Le point de départ est une base **vide** comme la production : la table
``coins`` n'a aucune ligne, seuls les mineurs réels (fixtures ``ps_*_c4``) et
la charge réelle de SRBMiner-MULTI 3.8.3 (fixtures ``srbminer_383_*``) sont
connues.  Aucun rig, aucune vraie machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PS_RYZEN = (FIXTURES / "ps_ryzen_c4.txt").read_text()
PS_ATHLON = (FIXTURES / "ps_athlon_c4.txt").read_text()
PS_RIG = (FIXTURES / "ps_rig_c4.txt").read_text()
API_RYZEN = json.loads((FIXTURES / "srbminer_383_ryzen.json").read_text())
API_ATHLON = json.loads((FIXTURES / "srbminer_383_athlon.json").read_text())

# La collecte réelle joint nvidia-smi : c'est lui qui donne le bus PCI des
# cartes, donc la carte sur laquelle le hashrate de l'API se rattache.
STDOUT_RYZEN = PS_RYZEN.replace(
    "### NVSMI", "### NVSMI\n0, 00000000:05:00.0, 66, 300, 60, 100", 1
)
STDOUT_ATHLON = PS_ATHLON.replace(
    "### NVSMI",
    "### NVSMI\n0, 00000000:08:00.0, 61, 129, 55, 99\n"
    "1, 00000000:09:00.0, 63, 131, 57, 98\n"
    "2, 00000000:0a:00.0, 64, 133, 58, 97",
    1,
)

RYZEN_TOTAL = 215354020158174.75
ATHLON_TOTAL = 71389744120115.31


# ── 1.1 base vide → coins créés ───────────────────────────────────────
def test_empty_database_creates_base_coins():
    """Base vide comme la prod : le démarrage crée PRL, KSH, WAM et BIG."""
    from app.coins import seed_builtin_coins
    from app.database import SessionLocal
    from app.models import Coin

    db = SessionLocal()
    try:
        assert db.query(Coin).count() == 0      # catalogue vide au départ
        assert seed_builtin_coins(db) == 4      # ce que fait app.main.startup
        assert seed_builtin_coins(db) == 0      # idempotent, aucun doublon
        coins = {coin.ticker: coin for coin in db.query(Coin).all()}
    finally:
        db.close()

    assert {"PRL", "KSH", "WAM", "BIG"} <= set(coins)
    assert coins["PRL"].algo == "pearlhash"
    assert coins["PRL"].kind == "gpu"
    assert "pearl-eu2.luckypool.io" in coins["PRL"].pool_hosts
    assert coins["KSH"].algo == "yespower"
    assert coins["WAM"].algo == "randomx"
    assert coins["BIG"].algo == "yespower"


def test_builtin_table_is_independent_from_the_catalogue():
    from app.coins import coin_for_algo, coin_for_pool

    assert coin_for_pool("stratum+tcp://pearl-eu2.luckypool.io:3360") == "PRL"
    assert coin_for_pool("pearl-us1.luckypool.io:3360") == "PRL"
    assert coin_for_pool("stratum+tcp://pool.korsh.org:3333") == "KSH"
    assert coin_for_pool("pool.wamcoin.org:3333") == "WAM"
    assert coin_for_pool("stratum.cryptopickaxe.co.uk:6030") == ""
    assert coin_for_algo("pearlhash") == "PRL"
    assert coin_for_algo("yespower") == ""


def test_every_miner_gets_a_coin_without_any_catalogue():
    """Sans catalogue, la table intégrée déduit le coin de chaque mineur."""
    from app.external_miners import detect_running_miners

    ryzen = [miner["coin"] for miner in detect_running_miners(PS_RYZEN)["miners"]]
    assert "PRL" in ryzen
    assert "BIG" in ryzen

    athlon = {miner["coin"] for miner in detect_running_miners(PS_ATHLON)["miners"]}
    assert {"PRL", "KSH"} <= athlon

    rig = {miner["coin"] for miner in detect_running_miners(PS_RIG)["miners"]}
    assert {"PRL", "WAM", "KSH"} <= rig


def test_machine_coin_only_comes_from_hiveos():
    """Une machine à plusieurs mineurs n'a pas « un » coin, mais une liste."""
    from app.stats_parser import parse_collect_stats

    blob = "### PS_MINERS\n" + PS_ATHLON.split("### PS_MINERS\n", 1)[1]
    stats = parse_collect_stats(blob)
    assert stats["coin"] == ""
    assert {"PRL", "KSH"} <= set(stats["coins"])


# ── 1.2 parseur SRBMiner 3.8.3 ────────────────────────────────────────
def test_parse_srbminer_383_ryzen():
    from app.stats_parser import parse_srbminer_api

    api = parse_srbminer_api(API_RYZEN)
    assert api is not None
    assert api["algo"] == "pearlhash"
    assert api["coin"] == "PRL"
    assert api["total_hashrate_hs"] == pytest.approx(RYZEN_TOTAL)
    assert api["accepted"] == 12610
    assert api["rejected"] == 0
    assert api["uptime_s"] == 381818
    assert len(api["gpus"]) == 1
    gpu = api["gpus"][0]
    assert gpu["pci_bus"] == "05:00.0"
    assert gpu["hashrate_hs"] == pytest.approx(RYZEN_TOTAL)
    assert gpu["power_w"] == 300
    assert gpu["temp_c"] == 66
    assert api["total_power_w"] == 300
    assert api["algos"][0]["pool"] == "pearl-eu2.luckypool.io:3360"
    # Le wallet de la réponse ne doit jamais sortir du parseur.
    assert "prl1testwallet" not in json.dumps(api)


def test_parse_srbminer_383_athlon_three_gpus():
    from app.stats_parser import parse_srbminer_api

    api = parse_srbminer_api(API_ATHLON)
    assert [gpu["pci_bus"] for gpu in api["gpus"]] == ["08:00.0", "09:00.0", "0a:00.0"]
    assert api["total_hashrate_hs"] == pytest.approx(ATHLON_TOTAL)
    assert [gpu["power_w"] for gpu in api["gpus"]] == [129, 131, 133]
    assert api["accepted"] == 4205
    assert api["rejected"] == 5
    assert "prl1testwallet" not in json.dumps(api)


def test_merge_attaches_hashrates_by_pci_bus():
    """Les devices sont dans le désordre : le rattachement suit le bus PCI."""
    from app.stats_parser import merge_srbminer, parse_srbminer_api

    api = parse_srbminer_api(API_ATHLON)
    stats = {
        "devices": [
            {"kind": "gpu", "pci_bus": "0a:00.0", "hashrate_hs": None,
             "power_w": None, "temp_c": None},
            {"kind": "gpu", "pci_bus": "08:00.0", "hashrate_hs": None,
             "power_w": None, "temp_c": None},
            {"kind": "gpu", "pci_bus": "09:00.0", "hashrate_hs": None,
             "power_w": None, "temp_c": None},
            {"kind": "cpu", "pci_bus": "", "hashrate_hs": None},
        ],
        "cpu_miners": [],
        "coin": "",
        "algo": "",
        "total_hashrate_hs": None,
    }
    merged = merge_srbminer(stats, api)
    by_bus = {d["pci_bus"]: d for d in merged["devices"] if d["kind"] == "gpu"}
    assert by_bus["08:00.0"]["hashrate_hs"] == pytest.approx(23802117844002.44)
    assert by_bus["09:00.0"]["hashrate_hs"] == pytest.approx(23791588222100.02)
    assert by_bus["0a:00.0"]["hashrate_hs"] == pytest.approx(23796038054012.85)
    assert by_bus["0a:00.0"]["power_w"] == 133
    assert merged["total_hashrate_hs"] == pytest.approx(ATHLON_TOTAL)
    assert merged["total_power_w"] == pytest.approx(393.0)


def test_merge_keeps_hiveos_format_compatible():
    """Le format plat déjà géré (HiveOS / anciens SRBMiner) reste lisible."""
    from app.stats_parser import merge_srbminer, parse_srbminer_api

    api = parse_srbminer_api({
        "algorithm": "pearlhash",
        "coin": "PEARL",
        "hashrate": {"total": 8.5e9, "devices": [4.2e9, 4.3e9]},
        "shares": {"accepted": 10, "rejected": 0},
    })
    stats = {
        "devices": [
            {"kind": "gpu", "pci_bus": "01:00.0", "hashrate_hs": None},
            {"kind": "gpu", "pci_bus": "02:00.0", "hashrate_hs": None},
        ],
        "coin": "",
        "algo": "",
        "total_hashrate_hs": None,
    }
    merged = merge_srbminer(stats, api)
    assert merged["total_hashrate_hs"] == pytest.approx(8.5e9)
    assert [d["hashrate_hs"] for d in merged["devices"]] == [4.2e9, 4.3e9]


# ── Bout en bout : collecte → base → /api/metrics/live ────────────────
def seed_machine(name: str, gpus: list[str]) -> int:
    from app.database import SessionLocal
    from app.models import Device, Machine

    db = SessionLocal()
    try:
        machine = Machine(
            name=name, host="192.0.2.61", ssh_port=22, ssh_user="fossor",
            auth_type="password", usage="both", enabled=True,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(
            uuid=f"33333333-3333-4333-8333-{machine.id:012d}",
            machine_id=machine.id, kind="cpu", model="Ryzen 5 3600", index=0,
        ))
        for position, bus in enumerate(gpus):
            db.add(Device(
                uuid=f"44444444-4444-4444-8444-{machine.id:04d}{position:08d}",
                machine_id=machine.id, kind="gpu", model="RTX 4080", index=position,
                pci_bus=bus, vendor="NVIDIA",
            ))
        db.commit()
        return machine.id
    finally:
        db.close()


async def test_collection_then_live_api_on_empty_database(monkeypatch, client):
    machine_id = seed_machine("ryzen-c6", ["05:00.0"])

    from app import collector
    from app.stats_parser import parse_srbminer_api

    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": STDOUT_RYZEN}

    async def fake_api(host, port):
        assert port == 21373
        return parse_srbminer_api(API_RYZEN)

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    monkeypatch.setattr(collector, "fetch_srbminer", fake_api)

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True

    response = await client.get("/api/metrics/live")
    assert response.status_code == 200
    payload = response.json()
    machine = next(row for row in payload if row["name"] == "ryzen-c6")
    assert machine["coin"] == ""               # pas de HiveOS : pas de coin machine
    assert machine["coins"] == ["PRL", "BIG"]   # la liste des coins
    assert machine["total_hashrate_hs"] == pytest.approx(RYZEN_TOTAL)
    assert machine["api_error"] is None

    groups = {group["coin"]: group for group in machine["coin_groups"]}
    assert groups["PRL"]["hashrate_hs"] == pytest.approx(RYZEN_TOTAL)
    assert groups["PRL"]["algo"] == "pearlhash"
    assert "BIG" in groups

    gpu = next(d for d in machine["devices"] if d["kind"] == "gpu")
    assert gpu["hashrate_hs"] == pytest.approx(RYZEN_TOTAL)
    assert gpu["power_w"] == 300
    assert gpu["temp_c"] == 66

    wallet = API_RYZEN["algorithms"][0]["pool"]["wallet"]
    assert wallet not in response.text


async def test_athlon_three_gpus_end_to_end(monkeypatch, client):
    """3 GPU, bus 8/9/10 : une ligne de métrique par carte, au bon bus."""
    machine_id = seed_machine("athlon-c6", ["08:00.0", "09:00.0", "0a:00.0"])

    from app import collector
    from app.stats_parser import parse_srbminer_api

    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": STDOUT_ATHLON}

    async def fake_api(host, port):
        return parse_srbminer_api(API_ATHLON)

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    monkeypatch.setattr(collector, "fetch_srbminer", fake_api)

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True

    response = await client.get("/api/metrics/live")
    machine = next(
        row for row in response.json() if row["name"] == "athlon-c6"
    )
    assert "PRL" in machine["coins"]
    gpus = {d["pci_bus"]: d for d in machine["devices"] if d["kind"] == "gpu"}
    assert gpus["08:00.0"]["hashrate_hs"] == pytest.approx(23802117844002.44)
    assert gpus["09:00.0"]["hashrate_hs"] == pytest.approx(23791588222100.02)
    assert gpus["0a:00.0"]["hashrate_hs"] == pytest.approx(23796038054012.85)
    group = next(g for g in machine["coin_groups"] if g["coin"] == "PRL")
    assert group["hashrate_hs"] == pytest.approx(ATHLON_TOTAL)
