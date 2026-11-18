"""Restes de la 2026.11.003-c9 (§2.4) : fréquence, puissance 0, deux algos, BIG."""

import json
import time
import uuid as uuid_lib
from pathlib import Path

import pytest

from app.collector import _first_number
from app.cpu_stats import _attach_solo, cpu_state_with, nonces_hashrate

FIXTURES = Path(__file__).parent / "fixtures"
MACHINE = {
    "name": "remains", "host": "192.0.2.78", "ssh_port": 22, "ssh_user": "root",
    "auth_type": "password", "secret": "secret", "usage": "cpu", "enabled": True,
}


def test_frequency_keys_always_present():
    state = cpu_state_with({"state": {"freq_mhz": 3600.0, "freq_source": "cpuinfo"}})
    assert state["freq_mhz"] == 3600.0
    assert state["freq_ghz"] == 3.6
    empty = cpu_state_with({"state": {}})
    assert empty["freq_mhz"] is None
    assert empty["freq_ghz"] is None
    assert "freq_source" in empty


def test_api_power_zero_means_not_measured():
    # RAPL présent : 0,0 W de SRBMiner ne doit pas écraser la mesure.
    state = cpu_state_with(
        {"state": {"power_w": 44.0, "power_source": "rapl"}}, api_power_w=0.0
    )
    assert state["power_w"] == 44.0
    assert state["power_source"] == "rapl"
    # Aucune autre source : 0,0 W de l'API n'est pas une mesure — l'estimation
    # par TDP (2026.11.004-c5) prend le relais et se déclare estimée.
    state = cpu_state_with({"state": {}}, api_power_w=0.0)
    assert state["power_w"] == 65.0
    assert state["power_source"] == "estimation générique 65 W"
    assert state["power_estimated"] is True
    # Une vraie valeur de l'API garde la priorité.
    state = cpu_state_with({"state": {"power_w": 44.0}}, api_power_w=88.6)
    assert state["power_w"] == 88.6
    assert state["power_source"] == "API SRBMiner package_power"
    # 0,0 W d'un device n'est pas une mesure non plus.
    assert _first_number(0.0, 42.0) == 42.0


def _miner(coin, hs, algo=""):
    return {"kind": "cpu", "coin": coin, "name": f"{coin}-miner", "algorithm": algo,
            "hashrate_hs": hs, "hashrate_source": "log", "threads": "8"}


def test_two_algorithms_are_never_summed():
    state = cpu_state_with({}, cpu_miners=[_miner("WAM", 1170.0), _miner("KSH", 6250.0)])
    assert state["hashrate_hs"] is None
    assert [item["label"] for item in state["hashrate_by_algo"]] == ["WAM", "KSH"]
    assert state["hashrate_label"] == "WAM 1.17 kH/s · KSH 6.25 kH/s"
    single = cpu_state_with({}, cpu_miners=[_miner("KSH", 6250.0)])
    assert single["hashrate_hs"] == 6250.0
    assert single["hashrate_label"] == "KSH 6.25 kH/s"


def test_solo_big_nonces_is_only_the_unverified_fallback():
    # FORMAT NON VÉRIFIÉ : aucun relevé réel de ce motif, dernier recours.
    text = "22:00:00 524288 nonces\n22:00:20 524288 nonces"
    assert nonces_hashrate(text, workers=1) == pytest.approx(26214.4)
    assert nonces_hashrate(text, workers=2) == pytest.approx(52428.8)
    assert nonces_hashrate("aucune ligne exploitable", workers=2) is None


def test_solo_big_reads_the_real_log_format():
    text = (FIXTURES / "solo_big_log.txt").read_text()
    miner = {"kind": "solo", "coin": "BIG", "workers": 2, "hashrate_hs": None,
             "hashrate_source": "", "hashrate_note": ""}
    probes = {"nodes": [{"coin": "BIG", "key": "big",
                         "log_candidates": ["/var/log/mine-bigcoin.log"], "pids": []}]}
    report = {
        "logs": {"/var/log/mine-bigcoin.log": {
            "mtime": int(time.time()) - 5, "text": text}},
        "log_pids": {},
        "nodes": {"big": {"height": 1200, "network_hs": 1e6}},
        "apis": {},
    }
    _attach_solo(miner, probes, report)
    assert miner["hashrate_hs"] == pytest.approx(6952.0)
    assert miner["workers"] == 11
    assert "hashes/s/worker" in miner["hashrate_source"]
    assert miner["node"]["height"] == 1200


async def test_live_api_exposes_frequency_and_breakdown(client):
    response = await client.post("/api/machines", json=MACHINE)
    assert response.status_code in (200, 201), response.text
    from app.database import SessionLocal
    from app.models import Machine, MachineStatus

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.name == "remains").first()
        assert machine.uuid
        db.add(MachineStatus(
            machine_uuid=machine.uuid, online=True,
            cpu_json=json.dumps({
                "freq_mhz": 3600.0, "freq_ghz": 3.6, "freq_source": "cpuinfo",
                "power_w": 88.6, "power_source": "API SRBMiner package_power",
                "hashrate_by_algo": [
                    {"label": "WAM", "hashrate_hs": 1170.0, "human": "1.17 kH/s"},
                    {"label": "KSH", "hashrate_hs": 6250.0, "human": "6.25 kH/s"},
                ],
                "hashrate_label": "WAM 1.17 kH/s · KSH 6.25 kH/s",
            }),
        ))
        db.commit()
    finally:
        db.close()
    live = (await client.get("/api/metrics/live")).json()
    row = next(item for item in live if item["name"] == "remains")
    assert row["cpu"]["freq_mhz"] == 3600.0
    assert row["cpu"]["freq_ghz"] == 3.6
    assert row["cpu_freq_mhz"] == 3600.0
    assert row["cpu"]["hashrate_label"] == "WAM 1.17 kH/s · KSH 6.25 kH/s"
    assert row["cpu"].get("hashrate_hs") is None
