"""ASIC (§4.20) — pilotes et santé.

Les fixtures AxeOS et IceRiver sont des réponses **réelles** (Bitaxe AxeOS
v2.12.2 Gamma 602 ; KS0 Ultra ``ICM168_3_2_14_ks0ultra``, ``post=4`` de
``/user/userpanel``), champs sensibles à ``REDACTED``.  Seul cgminer est écrit
d'après sa documentation publique, sans sortie réelle : ses tests le déclarent
(``format_verified``) au lieu de faire croire à une mesure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import asic, health

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def axeos_state() -> dict:
    payload = json.loads((FIXTURES / "axeos-system-info.json").read_text())
    return asic.parse_axeos(payload)


@pytest.fixture()
def iceriver_payload() -> dict:
    return json.loads((FIXTURES / "iceriver-userpanel-post4.json").read_text())


@pytest.fixture()
def iceriver_state(iceriver_payload) -> dict:
    return asic.parse_iceriver(iceriver_payload)


def test_axeos_real_fixture_hashrate_pct_and_domain(axeos_state):
    assert axeos_state["driver"] == "axeos"
    assert axeos_state["hashrate_hs"] == pytest.approx(1280.7592773e9, rel=1e-6)
    assert axeos_state["hashrate_1h_hs"] == pytest.approx(1376.1875e9, rel=1e-6)
    assert axeos_state["expected_hashrate_hs"] == pytest.approx(1836e9)
    assert axeos_state["nominal_pct"] == pytest.approx(75.0, abs=0.2)
    assert axeos_state["domains_total"] == 4
    assert axeos_state["domains_inactive"] == 1
    assert axeos_state["power_w"] == pytest.approx(30.4, rel=1e-3)
    assert axeos_state["temp_c"] == pytest.approx(41.75)
    assert axeos_state["pool"] == "public-pool.io"
    assert axeos_state["shares_accepted"] == 2468
    assert axeos_state["shares_rejected"] == 5
    assert axeos_state["read_only"] is True
    # La fixture est expurgée : l'adresse de pool vaut « REDACTED » et reste
    # inchangée ; le masquage des vraies adresses est couvert plus bas.
    assert axeos_state["pool_user_masked"] in ("REDACTED", "")


def test_axeos_health_flags_inactive_domain(axeos_state):
    verdict = asic.health(axeos_state)
    assert verdict["status"] == "orange"
    assert "domaine de hachage inactif (1/4)" in verdict["reason"]
    assert "75 % du nominal" in verdict["reason"]


def test_axeos_without_inactive_domain_is_green(axeos_state):
    clean = dict(axeos_state, domains_inactive=0, nominal_pct=98.0)
    assert asic.health(clean)["status"] == "green"
    low = dict(axeos_state, domains_inactive=0, nominal_pct=40.0)
    verdict = asic.health(low)
    assert verdict["status"] == "orange"
    assert "40 % du nominal" in verdict["reason"]


def test_health_offline_and_missing_state():
    assert asic.health(None)["status"] == "orange"
    assert asic.health({"online": False})["status"] == "red"


def test_cgminer_parser_is_declared_unverified():
    payload = {
        "SUMMARY": [{"Elapsed": 3600, "GHS 5s": 14000.0, "Accepted": 12, "Rejected": 1}],
        "STATS": [{"Type": "Antminer S19", "Temperature1": 62, "fan1": 4200}],
        "POOLS": [{
            "URL": "stratum+tcp://pool.example:3333",
            "Stratum Active": True,
            "User": "bc1qexampleworker",
            "Accepted": 12,
            "Rejected": 1,
        }],
    }
    state = asic.parse_cgminer(payload, payload, payload)
    assert state["driver"] == "cgminer"
    assert state["hashrate_hs"] == pytest.approx(14e12)
    assert state["model"] == "Antminer S19"
    assert state["temp_c"] == pytest.approx(62)
    assert state["pool"] == "stratum+tcp://pool.example:3333"
    assert "…" in state["pool_user_masked"]
    assert "bc1qexampleworker" not in state["pool_user_masked"]
    assert state["format_verified"] is False


# ── IceRiver KS0 Ultra : fixture réelle ``/user/userpanel`` ``post=4`` ──
def test_iceriver_real_fixture_hashrate_nominal_and_health(iceriver_state):
    assert iceriver_state["driver"] == "iceriver"
    assert iceriver_state["format_verified"] is True
    # ``rtpow`` en temps réel, ``avgpow`` en moyenne : les deux en H/s.
    assert iceriver_state["hashrate_hs"] == pytest.approx(271e9)
    assert iceriver_state["hashrate_1h_hs"] == pytest.approx(245e9)
    assert iceriver_state["nominal_hs"] == pytest.approx(400e9)  # KS0 Ultra
    assert iceriver_state["expected_hashrate_hs"] == pytest.approx(400e9)
    assert iceriver_state["nominal_pct"] == pytest.approx(61.0, abs=0.5)
    verdict = asic.health(iceriver_state)
    assert verdict["status"] == "orange"
    assert verdict["reason"] == "ventilateurs à 0 tr/min"


def test_iceriver_real_fixture_model_boards_runtime_and_pool(iceriver_state):
    assert iceriver_state["model"] == "KS0 Ultra"
    assert iceriver_state["firmware"] == "ICM168_3_2_14_ks0ultra_miner"
    assert iceriver_state["coin"] == "KAS"
    assert iceriver_state["algorithm"] == "kHeavyHash"
    assert iceriver_state["runtime"] == "00:05:20:20"
    assert iceriver_state["uptime_s"] == 19220  # j:h:m:s -> 0 j 5 h 20 min 20 s
    assert iceriver_state["freq_mhz"] == pytest.approx(875)
    assert iceriver_state["chipnum"] == pytest.approx(18)
    assert iceriver_state["fans"] == [0, 0, 0, 0]
    assert iceriver_state["fan_rpm"] == 0
    assert iceriver_state["fan_state"] is False
    assert iceriver_state["intemp_c"] == pytest.approx(36)
    assert iceriver_state["outtemp_c"] == pytest.approx(29)
    board = iceriver_state["boards"][0]
    assert board["freq_mhz"] == pytest.approx(875)
    assert board["avgpow_hs"] == pytest.approx(245.86e9)
    assert board["chipnum"] == pytest.approx(18)
    assert iceriver_state["pool"] == "stratum+tcp://kas.eu1.humpool.com:18083"
    assert iceriver_state["pool_connected"] is True
    assert iceriver_state["shares_accepted"] == pytest.approx(296)
    assert iceriver_state["shares_rejected"] == pytest.approx(1)
    assert iceriver_state["reject_pct"] == pytest.approx(0.342466)


def test_iceriver_state_carries_no_wallet_and_no_redaction(iceriver_state, iceriver_payload):
    dumped = json.dumps(iceriver_state, ensure_ascii=False)
    assert "REDACTED" not in dumped
    assert "user" not in dumped
    assert "pass" not in dumped
    # La fixture porte bien des identifiants : le parseur ne les recopie pas.
    assert any(pool.get("user") for pool in iceriver_payload["data"]["pools"])
    assert "pools" not in iceriver_state


def test_iceriver_without_fans_zero_falls_back_to_nominal(iceriver_state):
    spinning = dict(iceriver_state, fans_zero=False, fan_state=True, fan_rpm=1877)
    verdict = asic.health(spinning)
    assert verdict["status"] == "orange"
    assert verdict["reason"] == "hashrate à 61 % du nominal"
    healthy = dict(iceriver_state, fans_zero=False, fan_state=True, nominal_pct=95.0)
    assert asic.health(healthy)["status"] == "green"


def test_iceriver_disconnected_pool_is_orange(iceriver_state):
    state = dict(iceriver_state, fans_zero=False, nominal_pct=95.0, pool_connected=False)
    verdict = asic.health(state)
    assert verdict["status"] == "orange"
    assert verdict["reason"] == "pool déconnecté"


def test_driver_recognition_and_short_rates(iceriver_payload):
    assert asic.driver_from_payload({"hashRate": 1, "expectedHashrate": 2}) == "axeos"
    assert asic.driver_from_payload({"SUMMARY": []}) == "cgminer"
    assert asic.driver_from_payload({"data": {"rtpow": "1T"}}) == "iceriver"
    assert asic.driver_from_payload(iceriver_payload) == "iceriver"
    assert asic.state_from_payload(iceriver_payload)["model"] == "KS0 Ultra"
    assert asic.driver_from_payload({}) == ""
    assert asic.parse_short_rate("37.68G") == pytest.approx(37.68e9)
    assert asic.parse_short_rate("1.18T") == pytest.approx(1.18e12)
    assert asic.parse_rate("271", "G") == pytest.approx(271e9)
    assert asic.parse_runtime("00:05:20:20") == 19220


# ── Santé CPU multi-mineurs (§0.3) ──────────────────────────────────
def _cpu_machine(miners: list[dict]) -> dict:
    return {
        "collected": True,
        "online": True,
        "total_hashrate_hs": sum(m["hashrate_hs"] or 0 for m in miners),
        "api_error": "",
        "devices": [{
            "kind": "cpu",
            "index": None,
            "present": True,
            "enabled_for_mining": True,
            # Deux algos tournent : le device CPU n'a **pas** de hashrate unique.
            "hashrate_hs": None,
            "temp_c": 55,
        }],
        "cpu": {"miners": miners, "temp_c": 55},
    }


def test_cpu_two_miners_both_fresh_is_green():
    machine = _cpu_machine([
        {"name": "wam", "coin": "WAM", "hashrate_hs": 1200.0},
        {"name": "ksh", "coin": "KSH", "hashrate_hs": 6250.0},
    ])
    verdict = health.evaluate(machine)
    assert verdict["status"] == "green", verdict
    assert verdict["reason"] == ""


def test_cpu_miner_without_hashrate_is_orange_and_named():
    machine = _cpu_machine([
        {"name": "wam", "coin": "WAM", "hashrate_hs": 1200.0},
        {"name": "ksh", "coin": "KSH", "hashrate_hs": None},
    ])
    verdict = health.evaluate(machine)
    assert verdict["status"] == "orange"
    assert verdict["reason"] == "CPU : KSH sans hashrate"


def test_cpu_without_any_hashrate_is_orange():
    machine = _cpu_machine([
        {"name": "wam", "coin": "WAM", "hashrate_hs": None},
        {"name": "ksh", "coin": "KSH", "hashrate_hs": None},
    ])
    verdict = health.evaluate(machine)
    assert verdict["status"] == "orange"
    assert verdict["reason"] == "CPU sans hashrate"


def test_asic_machine_goes_through_asic_health(axeos_state):
    machine = {"kind": "asic", "asic": axeos_state, "collected": True, "online": True, "devices": []}
    verdict = health.evaluate(machine)
    assert verdict["status"] == "orange"
    assert "domaine de hachage inactif" in verdict["reason"]
