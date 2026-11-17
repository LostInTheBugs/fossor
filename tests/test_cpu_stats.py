"""Parseurs du hashrate CPU et de l'état du CPU (correctif 2026.11.003-c7).

Fixtures tirées des vrais formats : dernières lignes de log cpuminer-opt,
wam-miner et xmrig, réponse ``/summary`` OMS, sortie de ``getmininginfo``,
``/proc/stat``, ``/sys/class/hwmon`` k10temp et coretemp, RAPL.
"""

from pathlib import Path

import pytest

from app import cpu_stats as cs

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class TestCpuState:
    def test_load_from_two_proc_stat_samples(self):
        blocks = cs.split_blocks(fixture("proc_stat_pair.txt"))
        load = cs.parse_load(blocks["PROC_STAT"], blocks["PROC_STAT2"])
        assert load == pytest.approx(67.5, abs=0.2)

    def test_load_needs_two_samples(self):
        blocks = cs.split_blocks(fixture("proc_stat_pair.txt"))
        assert cs.parse_load(blocks["PROC_STAT"], "") is None
        assert cs.parse_load(blocks["PROC_STAT"], blocks["PROC_STAT"]) is None

    def test_hwmon_amd_prefers_tctl(self):
        assert cs.parse_hwmon(fixture("hwmon_k10temp.txt")) == (68.0, "k10temp Tctl")

    def test_hwmon_intel_prefers_package(self):
        assert cs.parse_hwmon(fixture("hwmon_coretemp.txt")) == (57.0, "coretemp Package id 0")

    def test_hwmon_never_reports_a_gpu_or_nvme(self):
        temp, sensor = cs.parse_hwmon("### HWMON\nnvme|Composite|38900\namdgpu|edge|54000\n")
        assert temp is None and sensor == ""

    def test_rapl_sums_the_package_domain_only(self):
        blocks = cs.split_blocks(fixture("rapl_pair.txt"))
        # package : 21 820 000 - 12 000 000 = 9 820 000 uJ sur 1 s = 9,82 W
        assert cs.parse_rapl(blocks["POWER"], blocks["POWER2"]) == pytest.approx(9.82, abs=0.01)

    def test_rapl_without_package_domain_is_none(self):
        text = "### POWER\n/sys/class/powercap/intel-rapl:0:0/energy_uj|1\n"
        assert cs.parse_rapl(text, text.replace("|1", "|2")) is None

    def test_cpuinfo_cores_model_and_frequency(self):
        text = fixture("proc_stat_pair.txt").replace(
            "### PROC_STAT", "cpu MHz : 2200.000\nmodel name : AMD Ryzen 5 3600\nprocessor : 1"
        )
        info = cs.parse_cpuinfo(
            "processor : 0\nmodel name : AMD Ryzen 5 3600\ncpu MHz : 3800.000\n"
            "processor : 1\nmodel name : AMD Ryzen 5 3600\ncpu MHz : 4000.000\n"
        )
        assert info["cores"] == 2
        assert info["model"] == "AMD Ryzen 5 3600"
        assert info["freq_mhz"] == pytest.approx(3900.0)
        assert text  # fixture lisible

    def test_scaling_frequency_in_khz(self):
        assert cs.parse_scaling_freq("3600000\n3900000\n") == pytest.approx(3750.0)


class TestHashrateLogs:
    def test_cpuminer_opt_ttf_line(self):
        hashrate, line = cs.hashrate_from_text(fixture("cpu_athlon_log.txt"))
        assert hashrate == pytest.approx(237.8e6)
        assert "TTF @ 237.8 Mh/s" in line

    def test_wam_miner_khs_with_comma(self):
        hashrate, _ = cs.hashrate_from_text(fixture("cpu_wam_log.txt"))
        assert hashrate == pytest.approx(1210.0)

    def test_xmrig_speed_line_skips_the_job_line(self):
        hashrate, line = cs.hashrate_from_text(fixture("cpu_xmrig_log.txt"))
        assert hashrate == pytest.approx(3140.8)
        assert "speed 10s/60s/15m" in line

    def test_stale_log_is_a_stopped_miner_not_a_hashrate(self):
        result = cs.log_hashrate(fixture("cpu_wam_log.txt"), mtime=1_000, now=1_400)
        assert result["hashrate_hs"] is None
        assert result["state"] == "stale"
        assert "figé depuis 400 s" in result["note"]

    def test_fresh_log_gives_its_age(self):
        result = cs.log_hashrate(fixture("cpu_wam_log.txt"), mtime=1_000, now=1_100)
        assert result["hashrate_hs"] == pytest.approx(1210.0)
        assert result["age_s"] == 100

    def test_unreadable_log_says_why(self):
        result = cs.log_hashrate("connexion au pool…\n", mtime=2_000, now=2_010)
        assert result["hashrate_hs"] is None
        assert "aucun motif de hashrate" in result["note"]

    def test_solo_estimate_from_scan_interval(self):
        # 200k nonces entre 09:12:40 et 09:15:20 (160 s) = 1250 H/s
        assert cs.scan_hashrate(fixture("korsh_mining_log.txt")) == pytest.approx(1250.0)

    def test_blocks_found_counts_the_script_log(self):
        assert cs.blocks_found(fixture("korsh_mining_log.txt")) == 1


class TestMinerApis:
    def test_oms_summary_devices_hashrate(self):
        payload = cs.parse_api_payload(fixture("oms_summary.json"))
        assert payload["state"] == "MINING"
        assert payload["hashrate_hs"] == pytest.approx(1.498e9)

    def test_cpuminer_summary_flat_hashrate(self):
        payload = cs.parse_api_payload('{"hashrate": 237800000.0, "accepted": 31}')
        assert payload["hashrate_hs"] == pytest.approx(237.8e6)

    def test_html_answer_is_not_an_api(self):
        assert cs.parse_api_payload("<html>titre</html>") is None

    def test_mininginfo_getters(self):
        info = cs.parse_mininginfo(fixture("getmininginfo.json"))
        assert info["height"] == 21458
        assert info["network_hs"] == pytest.approx(18.4e9)
        assert info["chain"] == "main"

    def test_network_share(self):
        assert cs.network_share_pct(1250.0, 18.4e9) == pytest.approx(0.0000, abs=1e-4)
        assert cs.network_share_pct(1e9, 1e10) == pytest.approx(10.0)


class TestProbePlanning:
    def test_athlon_cpuminer_default_api_port(self):
        probes = cs.plan_cpu_probes(fixture("ps_athlon_c4.txt"))
        assert [probe["port"] for probe in probes["apis"]] == [4048]
        assert probes["logs"] == []
        # Le log du service systemd vient de /proc/<pid>/fd/1, pas d'un chemin en dur.
        assert [probe["pid"] for probe in probes["fds"]] == [1270]

    def test_rig_wam_log_from_the_screen_redirection(self):
        probes = cs.plan_cpu_probes(fixture("ps_rig_c4.txt"))
        paths = [probe["path"] for probe in probes["logs"]]
        assert "/root/wam/miner.log" in paths
        # SRBMiner GPU (sans drapeau CPU) n'est pas sondé : seul le mineur CPU compte.
        assert 21550 not in [probe["port"] for probe in probes["apis"]]

    def test_rig_korsh_node_client_and_script_log(self):
        probes = cs.plan_cpu_probes(fixture("ps_rig_c4.txt"))
        node = probes["nodes"][0]
        assert node["key"] == "korsh"
        assert node["cli"] == "/root/korsh/bin/korsh-cli"
        assert node["datadir"] == "/root/korsh/data"
        assert node["coin"] == "KSH"
        assert "/root/korsh/mining.log" in node["log_candidates"]
        assert "/root/korsh/mining.log" in [probe["path"] for probe in probes["logs"]]

    def test_ryzen_oms_log_and_bigcoin_node(self):
        probes = cs.plan_cpu_probes(fixture("ps_ryzen_c4.txt"))
        assert "/var/log/cpu-miner.log" in [probe["path"] for probe in probes["logs"]]
        assert 4068 in [probe["port"] for probe in probes["apis"]]
        keys = {node["key"]: node["coin"] for node in probes["nodes"]}
        assert keys == {"bigcoin": "BIG"}

    def test_hostile_path_is_refused(self):
        section = (
            "### PS_MINERS\n"
            "root 12 99 100 /usr/bin/wam-miner -o stratum+tcp://x:1 >> /root/wam/$(id).log\n"
        )
        probes = cs.plan_cpu_probes(section)
        assert all("$" not in probe["path"] for probe in probes["logs"])


class TestAttach:
    def _miners(self) -> list[dict]:
        return [
            {"name": "cpuminer-opt", "binary": "cpuminer-korsh", "kind": "cpu",
             "coin": "KSH", "threads": "2", "cmd": "/home/x/cpuminer-korsh --algo yespower"},
            {"name": "Minage solo (nœud)", "binary": "korshd", "kind": "solo", "coin": "KSH",
             "cmd": "korshd -datadir=… · 1 worker(s)"},
        ]

    def test_api_wins_over_log(self):
        probes = {
            "logs": [{"path": "/var/log/x.log", "family": "cpuminer"}],
            "apis": [{"port": 4048, "family": "cpuminer"}],
            "nodes": [],
        }
        report = {
            "logs": {"/var/log/x.log": {"mtime": 1000, "text": "TTF @ 1.0 Mh/s"}},
            "apis": {4048: {"hashrate_hs": 5.0e9, "state": "MINING"}},
            "nodes": {},
        }
        miners = cs.attach_cpu_hashrates(self._miners()[:1], probes, report, now=1010)
        assert miners[0]["hashrate_hs"] == pytest.approx(5.0e9)
        assert miners[0]["hashrate_source"] == "API :4048/summary"

    def test_log_used_when_api_is_silent(self):
        probes = {"logs": [{"path": "/var/log/x.log", "family": "cpuminer"}],
                  "apis": [{"port": 4048, "family": "cpuminer"}], "nodes": []}
        report = {"logs": {"/var/log/x.log": {"mtime": 1000, "text": "TTF @ 237.8 Mh/s\n"}},
                  "apis": {4048: None}, "nodes": {}}
        miners = cs.attach_cpu_hashrates(self._miners()[:1], probes, report, now=1010)
        assert miners[0]["hashrate_hs"] == pytest.approx(237.8e6)
        assert miners[0]["hashrate_source"] == "log /var/log/x.log"

    def test_nothing_works_gives_an_explicit_reason(self):
        probes = {"logs": [], "apis": [], "nodes": []}
        report = {"logs": {}, "apis": {}, "nodes": {}}
        miners = cs.attach_cpu_hashrates(self._miners()[:1], probes, report)
        assert miners[0]["hashrate_hs"] is None
        assert miners[0]["hashrate_note"].startswith("hashrate CPU inconnu (source :")

    def test_solo_node_estimate_and_share(self):
        probes = {"logs": [], "apis": [],
                  "nodes": [{"key": "korsh", "coin": "KSH",
                             "log_candidates": ["/root/korsh/mining.log"]}]}
        report = {"logs": {"/root/korsh/mining.log":
                           {"mtime": 1000, "text": fixture("korsh_mining_log.txt")}},
                  "apis": {},
                  "nodes": {"korsh": {"key": "korsh", "height": 21458,
                                      "network_hs": 18.4e9, "difficulty": 128.4}}}
        miners = cs.attach_cpu_hashrates(self._miners()[1:], probes, report, now=1010)
        assert miners[0]["hashrate_hs"] == pytest.approx(1250.0)
        assert miners[0]["node"]["height"] == 21458
        assert miners[0]["node"]["blocks_found"] == 1
        assert miners[0]["node"]["share_pct"] is not None

    def test_cpu_state_composes_every_source(self):
        report = {"state": {"load_pct": 67.5, "temp_c": None, "freq_mhz": 3900.0,
                            "power_w": None, "power_source": ""}}
        miners = [{"name": "x", "coin": "KSH", "hashrate_hs": 1.0e9, "threads": "2",
                   "hashrate_source": "API :4048/summary"}]
        cpu = cs.cpu_state_with(report, hive_cputemp=48.0, api_power_w=88.96, cpu_miners=miners)
        assert cpu["temp_c"] == 48.0
        assert cpu["temp_sensor"] == "HiveOS last_stat cputemp"
        assert cpu["power_w"] == pytest.approx(88.96)
        assert cpu["power_source"] == "API SRBMiner package_power"
        assert cpu["hashrate_hs"] == pytest.approx(1.0e9)
        assert cpu["miners"][0]["coin"] == "KSH"

    def test_power_absence_is_stated(self):
        cpu = cs.cpu_state_with({"state": {}}, cpu_miners=[])
        assert cpu["power_source"] == "non mesurée"
        assert cpu["power_w"] is None
