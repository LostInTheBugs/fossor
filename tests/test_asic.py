"""ASIC (§4.20) — pilotes et santé.

La fixture AxeOS est une réponse **réelle** d'un des Bitaxe de Fred
(AxeOS v2.12.2, Gamma 602, champs sensibles à ``REDACTED``).  cgminer et
IceRiver sont écrits d'après leur documentation publique, sans sortie réelle :
les tests le déclarent (``format_verified``) au lieu de faire croire à une
mesure.
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


def test_iceriver_parser_uses_overview_format():
    payload = {
        "error": 0,
        "data": {
            "model": "KS0",
            "unit": "G",
            "rtpow": "150G",
            "avgpow": "145G",
            "fans": [1877, 1901, 0, 0],
            "pools": [{
                "accepted": 33240.0,
                "rejected": 7.0,
                "addr": "stratum+tcp://pool.example:1207",
                "connect": True,
                "user": "kaspa:qexampleworker",
            }],
            "boards": [{"chiptmp": 67.161591, "intmp": 43.0}],
        },
    }
    state = asic.parse_iceriver(payload)
    assert state["driver"] == "iceriver"
    assert state["model"] == "KS0"
    assert state["hashrate_hs"] == pytest.approx(150e9)
    assert state["coin"] == "KAS"
    assert state["algorithm"] == "kHeavyHash"
    assert state["temp_c"] == pytest.approx(67.161591)
    assert state["fan_rpm"] == 1901
    assert state["shares_accepted"] == 33240
    assert state["format_verified"] is False


def test_driver_recognition_and_short_rates():
    assert asic.driver_from_payload({"hashRate": 1, "expectedHashrate": 2}) == "axeos"
    assert asic.driver_from_payload({"SUMMARY": []}) == "cgminer"
    assert asic.driver_from_payload({"data": {"rtpow": "1T"}}) == "iceriver"
    assert asic.driver_from_payload({}) == ""
    assert asic.parse_short_rate("37.68G") == pytest.approx(37.68e9)
    assert asic.parse_short_rate("1.18T") == pytest.approx(1.18e12)


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
