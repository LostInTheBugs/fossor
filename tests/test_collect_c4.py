"""Correctif 2026.11.003-c4 — mineurs réels mal regroupés, coin et hashrate.

Les fixtures sont les lignes ``ps`` réelles des machines, **anonymisées** :
wallets, adresses et mots de passe remplacés, aucune IP privée, aucun nom
d'hôte réel.  Aucune connexion à un rig : tout passe par le texte figé ou par
les faux SSH du dossier ``tests``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SOLO = "Minage solo (nœud)"

# Catalogue tel qu'il est stocké en base : URL complète **avec** le schéma et
# le port, ce qui ne doit pas empêcher la correspondance.
HOSTS = {
    "stratum+tcp://pearl-eu2.luckypool.io:3360": "PRL",
    "stratum+tcp://pool.korsh.org:3333": "KSH",
    "stratum+tcp://pool.wamcoin.org:3333": "WAM",
}
TICKERS = ["PRL", "KSH", "WAM", "BIG", "OMS"]
NAMES = {
    "prl": "PRL", "pearl": "PRL", "korsh": "KSH", "wam": "WAM",
    "wamcoin": "WAM", "bigcoin": "BIG", "oms": "OMS",
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse(name: str, tickers=None):
    from app.stats_parser import parse_collect_stats

    return parse_collect_stats(
        fixture(name),
        coin_hosts=HOSTS,
        coin_tickers=TICKERS if tickers is None else tickers,
        coin_names=NAMES,
    )


def by_name(stats):
    return {m["name"]: m for m in stats["miners"]}


# ── 1. Parseur, machine par machine ───────────────────────────────────
def test_rig_hiveos_one_entry_per_miner():
    stats = parse("ps_rig_c4.txt")
    names = [m["name"] for m in stats["miners"]]
    assert names == ["SRBMiner-MULTI", "wam-miner", SOLO], names
    assert len(stats["miners"]) == 3
    miners = by_name(stats)

    srb = miners["SRBMiner-MULTI"]
    assert srb["coin"] == "PRL"
    assert srb["api_port"] == 21550
    assert srb["count"] == 1  # unbuffer + binaire = une seule entrée
    assert srb["cpu_pct"] == pytest.approx(34.1)
    assert srb["user"] == "root"
    assert srb["algorithm"] == "pearlhash"

    wam = miners["wam-miner"]
    assert wam["coin"] == "WAM"
    assert wam["count"] == 1  # SCREEN … bash -c exec + binaire
    assert wam["cpu_pct"] == pytest.approx(191.0)
    assert wam["kind"] == "cpu"
    assert wam["threads"] == "2"

    solo = miners[SOLO]
    assert solo["kind"] == "solo"
    assert solo["coin"] == "KSH"
    assert solo["workers"] == 1
    assert solo["count"] == 1
    assert solo["cpu_pct"] == pytest.approx(92.8)  # %CPU de korshd
    assert solo["user"] == "root"
    assert solo["binary"] == "korshd"

    # Ni le lanceur HiveOS ni le script de mineur ne deviennent des mineurs.
    assert "Script de minage" not in names
    assert all(m["name"] != "srbminer" for m in stats["miners"])


def test_ryzen_wrappers_and_solo_workers():
    stats = parse("ps_ryzen_c4.txt")
    names = [m["name"] for m in stats["miners"]]
    assert names == ["SRBMiner", "supryolo", SOLO], names
    assert len(stats["miners"]) == 3
    miners = by_name(stats)

    srb = miners["SRBMiner"]
    assert srb["count"] == 1  # script -qc + sh -c + binaire = une entrée
    assert srb["api_port"] == 21373
    assert srb["coin"] == "PRL"
    assert srb["cpu_pct"] == pytest.approx(1.6)
    assert srb["user"] == "root"

    cpu = miners["supryolo"]
    assert cpu["kind"] == "cpu"
    assert cpu["coin"] == "OMS"  # pool inconnu → préfixe du wallet
    assert cpu["count"] == 1
    assert cpu["threads"] == "10"

    solo = miners[SOLO]
    assert solo["coin"] == "BIG"
    assert solo["workers"] == 11  # onze bigcoin-cli generatetoaddress
    assert solo["cpu_pct"] == pytest.approx(1096.0)  # %CPU du démon bitcoind
    assert solo["user"] == "administrator"
    assert solo["binary"] == "bitcoind"
    assert "Script de minage" not in names  # absorbé par le minage solo


def test_athlon_renamed_srbminer_and_cpuminer():
    stats = parse("ps_athlon_c4.txt")
    names = [m["name"] for m in stats["miners"]]
    assert names == ["SRBMiner", "cpuminer"], names
    miners = by_name(stats)

    srb = miners["SRBMiner"]  # binaire renommé srbminer_compute
    assert srb["coin"] == "PRL"
    assert srb["api_port"] == 21550
    assert srb["user"] == "administrator"  # utilisateur complet, pas « adminis+ »
    assert srb["cpu_pct"] == pytest.approx(6.4)

    cpu = miners["cpuminer"]
    assert cpu["coin"] == "KSH"  # pool.korsh.org → KSH
    assert cpu["kind"] == "cpu"
    assert cpu["binary"] == "cpuminer-korsh"  # binaire renommé conservé
    assert cpu["cpu_pct"] == pytest.approx(198.0)
    assert cpu["user"] == "root"
    assert cpu["threads"] == "2"


def test_unknown_coin_without_wallet_ticker():
    stats = parse("ps_ryzen_c4.txt", tickers=("PRL", "KSH", "WAM", "BIG"))
    cpu = by_name(stats)["supryolo"]
    assert cpu["coin"] == ""  # OMS absent du catalogue → coin inconnu
    assert all(m["coin"] for m in stats["miners"] if m["name"] != "supryolo")


def test_no_duplicate_and_no_secret_in_output():
    for name in ("ps_rig_c4.txt", "ps_ryzen_c4.txt", "ps_athlon_c4.txt"):
        blob = json.dumps(parse(name), ensure_ascii=False)
        assert "prl1testwallet0000000000" not in blob
        assert "wam1testwallet000000" not in blob
        assert "--password x" not in blob
    assert "•••" in json.dumps(parse("ps_rig_c4.txt"), ensure_ascii=False)  # masqué
    stats = parse("ps_rig_c4.txt")
    keys = [(m["name"], m["cmd"]) for m in stats["miners"]]
    assert len(keys) == len(set(keys))


def test_hiveos_coin_alias_pearl_to_prl():
    blob = "### HIVE_STAT\n{\"params\": {\"meta\": {\"srbminer\": {\"coin\": \"PEARL\"}}}}\n### PS_MINERS\n"
    from app.stats_parser import parse_collect_stats

    assert parse_collect_stats(blob)["coin"] == "PRL"


def test_fixtures_carry_no_real_address():
    for path in FIXTURES.glob("ps_*_c4.txt"):
        text = path.read_text(encoding="utf-8")
        assert "192.168." not in text
        assert "10.0." not in text


# ── 2. Collecte → base → /api/metrics/live ────────────────────────────
def seed_machine(name: str, host: str = "192.0.2.30"):
    from app.database import SessionLocal
    from app.models import Coin, Device, Machine

    db = SessionLocal()
    try:
        machine = Machine(
            name=name, host=host, ssh_port=22, ssh_user="fossor",
            auth_type="password", usage="both", enabled=True,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(
            uuid=f"{abs(hash(name)) % 10 ** 8:08d}-1111-4111-8111-111111111111",
            machine_id=machine.id, kind="cpu", model="CPU fixture", index=0,
        ))
        for ticker, hosts, coin_name in (
            ("PRL", ["stratum+tcp://pearl-eu2.luckypool.io:3360"], "Pearl"),
            ("KSH", ["stratum+tcp://pool.korsh.org:3333"], "Korsh"),
            ("WAM", ["stratum+tcp://pool.wamcoin.org:3333"], "Wamcoin"),
            ("BIG", [], "Bigcoin"),
            ("OMS", [], "Oms"),
        ):
            if db.query(Coin).filter(Coin.ticker == ticker).first() is None:
                db.add(Coin(ticker=ticker, name=coin_name, pool_hosts=hosts))
        db.commit()
        return machine.id
    finally:
        db.close()


def _patch_ssh(monkeypatch, text: str):
    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": text}

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    # La détection initiale ne doit pas ouvrir de connexion.
    monkeypatch.setattr("app.devices.apply_detection", lambda *a, **k: {"devices": 1})
    async def no_detect(machine, actor="system"):
        return {"ok": False, "error": "détection désactivée en test"}

    monkeypatch.setattr("app.ssh_client.ssh_detect_hw", no_detect)


async def test_collect_athlon_reads_hashrate_over_ssh_relay(monkeypatch):
    """La réponse directe sans hashrate ne doit plus court-circuiter le repli."""
    from app import collector

    machine_id = seed_machine("athlon-3000g", host="192.0.2.31")
    _patch_ssh(monkeypatch, fixture("ps_athlon_c4.txt"))

    async def junk_direct(host, port):
        assert port == 21550
        return {"miner": "srbminer", "total_hashrate_hs": None}  # page étrangère

    relay_calls: list[int] = []

    async def relay(machine, port, actor="system"):
        relay_calls.append(port)
        return {
            "ok": True,
            "stdout": json.dumps({
                "mining": {
                    "algorithm": "pearlhash",
                    "hashrate": {"total": 1.25e9},
                    "shares": {"accepted": 12, "rejected": 0},
                }
            }),
        }

    monkeypatch.setattr(collector, "fetch_srbminer", junk_direct)
    monkeypatch.setattr("app.ssh_client.ssh_miner_api", relay)

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True
    assert relay_calls == [21550]

    from app.database import SessionLocal
    from app.models import Machine, MachineStatus

    db = SessionLocal()
    try:
        status = db.query(MachineStatus).one()
        assert status.total_hashrate_hs == pytest.approx(1.25e9)
        assert status.api_error is None
        machine = db.query(Machine).filter(Machine.id == machine_id).one()
        miners = json.loads(machine.external_miners)
        assert {m["name"] for m in miners} == {"SRBMiner", "cpuminer"}
        assert miners[0]["hashrate_hs"] == pytest.approx(1.25e9)
        assert miners[0]["user"] == "administrator"
        assert miners[0]["coin"] == "PRL"
    finally:
        db.close()


async def test_api_port_without_hashrate_is_reported(monkeypatch):
    from app import collector

    machine_id = seed_machine("athlon-down", host="192.0.2.32")
    _patch_ssh(monkeypatch, fixture("ps_athlon_c4.txt"))

    async def junk_direct(host, port):
        return {"note": "not a miner"}

    async def junk_relay(machine, port, actor="system"):
        return {"ok": True, "stdout": "{\"foo\": 1}"}

    monkeypatch.setattr(collector, "fetch_srbminer", junk_direct)
    monkeypatch.setattr("app.ssh_client.ssh_miner_api", junk_relay)

    await collector.collect_machine(machine_id)

    from app.database import SessionLocal
    from app.models import MachineStatus

    db = SessionLocal()
    try:
        status = db.query(MachineStatus).one()
        assert status.total_hashrate_hs is None
        assert status.api_error
        assert "sans hashrate" in status.api_error or "injoignable" in status.api_error
    finally:
        db.close()


async def test_live_groups_coins_per_machine(monkeypatch, client):
    """Rig → PRL + WAM + KSH, /api/metrics/live expose les groupes par coin."""
    from app import collector

    machine_id = seed_machine("rig-hiveos", host="192.0.2.33")
    _patch_ssh(monkeypatch, fixture("ps_rig_c4.txt"))

    async def api(host, port):
        return {
            "miner": "srbminer",
            "algo": "pearlhash",
            "coin": "PEARL",
            "total_hashrate_hs": 6.5e10,
            "device_hashrates_hs": [],
            "accepted": 3,
            "rejected": 0,
        }

    monkeypatch.setattr(collector, "fetch_srbminer", api)
    await collector.collect_machine(machine_id)

    response = await client.get("/api/metrics/live")
    assert response.status_code == 200
    payload = [m for m in response.json() if m["name"] == "rig-hiveos"][0]
    assert payload["coin"] == "PRL"  # PEARL replié sur PRL
    assert payload["api_error"] is None
    groups = {g["coin"]: g for g in payload["coin_groups"]}
    assert set(groups) == {"PRL", "WAM", "KSH"}
    assert groups["PRL"]["hashrate_hs"] == pytest.approx(6.5e10)
    assert groups["WAM"]["miners"] == 1
    assert groups["KSH"]["workers"] == 1
    names = {m["name"] for m in payload["external_miners"]}
    assert names == {"SRBMiner-MULTI", "wam-miner", SOLO}
    wam = [m for m in payload["external_miners"] if m["name"] == "wam-miner"][0]
    assert wam["cpu_pct"] == pytest.approx(191.0)
    assert wam["user"] == "root"
    assert wam["count"] == 1
