"""Unit tests for the pure ``collect_stats`` / SRBMiner parsers (no SSH, no DB)."""

import json
from pathlib import Path

import pytest

from app.stats_parser import (
    format_hashrate,
    hashrate_to_hs,
    merge_srbminer,
    parse_collect_stats,
    parse_ps_miners,
    parse_srbminer_api,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ── HiveOS snapshot ───────────────────────────────────────────────────
def test_hiveos_snapshot_maps_gpus_by_pci_bus():
    stats = parse_collect_stats(fixture("collect_stats_hiveos.txt"))

    assert stats["miner"] == "srbminer"
    assert stats["coin"] == "PRL"  # PEARL (HiveOS) replié sur le ticker du catalogue
    assert stats["algo"] == "pearlhash"
    assert stats["api_port"] == 21550
    assert stats["uptime_s"] == 183455  # /proc/uptime wins over miner uptime
    assert stats["accepted"] == 8969
    assert stats["rejected"] == 0

    gpus = [d for d in stats["devices"] if d["kind"] == "gpu"]
    assert len(gpus) == 5
    # index 0 of gpu-stats.json is the CPU/iGPU (bus 00:02.0): never a GPU
    assert [g["pci_bus"] for g in gpus] == [
        "01:00.0", "02:00.0", "04:00.0", "05:00.0", "06:00.0",
    ]
    assert "00:02.0" not in [g["pci_bus"] for g in gpus]

    first = gpus[0]
    assert first["temp_c"] == 42
    assert first["fan_pct"] == 100
    assert first["power_w"] == pytest.approx(93)  # HiveOS wins over nvidia-smi
    assert first["hashrate_hs"] == pytest.approx(68538879630 * 1000)
    assert stats["total_hashrate_hs"] == pytest.approx(328947000000 * 1000)
    assert stats["total_power_w"] == pytest.approx(93 + 99 + 91 + 97 + 120)


def test_hiveos_cpu_row_is_cpu_and_has_no_hashrate():
    stats = parse_collect_stats(fixture("collect_stats_hiveos.txt"))
    cpus = [d for d in stats["devices"] if d["kind"] == "cpu"]
    assert len(cpus) == 1
    assert cpus[0]["temp_c"] == 54
    assert cpus[0]["hashrate_hs"] is None
    assert cpus[0]["pci_bus"] == ""


def test_hiveos_secrets_are_never_parsed():
    raw = fixture("collect_stats_hiveos.txt")
    # The fixture really carries the two HiveOS secrets…
    assert "S3CR3T-HIVE-PASS" in raw
    assert "RIG-FAKE-ABC123" in raw

    blob = json.dumps(parse_collect_stats(raw))
    # …and the parser drops them: not a value, not a key.
    assert "S3CR3T-HIVE-PASS" not in blob
    assert "RIG-FAKE-ABC123" not in blob
    assert "passwd" not in blob
    assert "rig_id" not in blob


# ── Ubuntu + nvidia-smi ───────────────────────────────────────────────
def test_ubuntu_nvsmi_snapshot_without_hiveos():
    stats = parse_collect_stats(fixture("collect_stats_ubuntu.txt"))

    assert stats["miner"] == "srbminer"
    assert stats["api_port"] == 21550
    assert stats["uptime_s"] == 38211
    assert stats["total_hashrate_hs"] is None  # no HiveOS: API will fill it

    gpus = [d for d in stats["devices"] if d["kind"] == "gpu"]
    assert [g["pci_bus"] for g in gpus] == ["01:00.0", "02:00.0"]
    assert gpus[0]["temp_c"] == 61
    assert gpus[0]["power_w"] == pytest.approx(148.32)
    assert gpus[0]["fan_pct"] == 55
    assert gpus[0]["load_pct"] == 100
    assert gpus[0]["hashrate_hs"] is None


def test_machine_without_gpu_has_no_devices():
    stats = parse_collect_stats(fixture("collect_stats_no_gpu.txt"))
    assert stats["devices"] == []
    assert stats["miner"] == ""
    assert stats["api_port"] is None
    assert stats["total_hashrate_hs"] is None
    assert stats["total_power_w"] is None
    assert stats["uptime_s"] == 9650


# ── Units ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value,units,expected", [
    (68538879630, "khs", 6.853887963e13),
    (1, "mhs", 1e6),
    (2.5, "GH/s", 2.5e9),
    (0.5, "THS", 5e11),
    (1, "", 1.0),
    (1, None, 1.0),
])
def test_hashrate_to_hs(value, units, expected):
    assert hashrate_to_hs(value, units) == pytest.approx(expected)


def test_hashrate_to_hs_rejects_missing_values():
    assert hashrate_to_hs(None, "khs") is None
    assert hashrate_to_hs("[N/A]", "khs") is None


@pytest.mark.parametrize("hs,expected", [
    (None, "—"),
    (0, "0 H/s"),
    (999, "999 H/s"),
    (1e3, "1.0 kH/s"),
    (6.85e13, "68.5 TH/s"),
    (3.28947e14, "329 TH/s"),
    (1e18, "1000 PH/s"),
])
def test_format_hashrate(hs, expected):
    assert format_hashrate(hs) == expected


# ── Process list ──────────────────────────────────────────────────────
def test_ps_miners_finds_api_port_and_ignores_grep():
    ps = parse_ps_miners(
        "/opt/srbminer/srbminer --api-port 21550\n"
        "/usr/bin/Xmrig --api-port=21373\n"
        "grep -Ei srbminer|xmrig\n"
    )
    assert ps["miner"] == "srbminer"
    assert ps["api_port"] == 21550
    assert len(ps["miners"]) == 2


def test_ps_miners_empty_section():
    ps = parse_ps_miners("")
    assert ps == {"miner": "", "api_port": None, "miners": [], "cpu_miners": []}


# ── SRBMiner HTTP API ─────────────────────────────────────────────────
def test_srbminer_api_fixture():
    api = parse_srbminer_api(fixture("srbminer_api.json"))
    assert api is not None
    assert api["algo"] == "pearlhash"
    assert api["coin"] == "PEARL"
    assert api["accepted"] == 1420
    assert api["rejected"] == 3
    assert api["uptime_s"] == 38211
    assert api["total_hashrate_hs"] == pytest.approx(1.3694e14)
    assert api["device_hashrates_hs"] == [6.854e13, 6.84e13]


def test_srbminer_api_rejects_garbage():
    assert parse_srbminer_api("not json at all") is None
    assert parse_srbminer_api("[]") is None
    assert parse_srbminer_api({"id": 1, "result": []}) is not None  # tolerant, empty


def test_merge_srbminer_fills_missing_hashrates():
    stats = parse_collect_stats(fixture("collect_stats_ubuntu.txt"))
    merged = merge_srbminer(stats, parse_srbminer_api(fixture("srbminer_api.json")))
    gpus = [d for d in merged["devices"] if d["kind"] == "gpu"]
    assert [g["hashrate_hs"] for g in gpus] == [6.854e13, 6.84e13]
    assert merged["total_hashrate_hs"] == pytest.approx(1.3694e14)


def test_merge_srbminer_never_overwrites_hiveos_hashrates():
    stats = parse_collect_stats(fixture("collect_stats_hiveos.txt"))
    before = [d["hashrate_hs"] for d in stats["devices"] if d["kind"] == "gpu"]
    merge_srbminer(stats, parse_srbminer_api(fixture("srbminer_api.json")))
    after = [d["hashrate_hs"] for d in stats["devices"] if d["kind"] == "gpu"]
    assert before == after


def test_merge_srbminer_with_none_is_a_noop():
    stats = {"devices": [], "algo": "x"}
    assert merge_srbminer(stats, None) is stats
