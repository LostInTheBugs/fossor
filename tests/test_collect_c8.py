"""Correctif 2026.11.003-c8 — défauts vus en prod après la c7.

Aucune connexion réelle : les fixtures reproduisent les formes exactes
rencontrées (``/proc/<pid>/fd/1``, ``/proc/<pid>/cwd``, ``cpu MHz``,
``energy_uj`` root, ``cpu_devices[0].package_power``).  Le bout en bout pousse
trois machines jusqu'à ``/api/metrics/live``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ── 1.2 — /proc/<pid>/fd : fichier et socket ──────────────────────────
def test_probe_reads_fd_with_sudo_and_falls_back_to_journald():
    from app.ssh_client import cpu_probe_command

    command = cpu_probe_command({"fds": [{"pid": 1270, "family": "cpuminer"}]})
    assert "RD readlink -f /proc/$p/fd/1" in command
    assert "RD readlink -f /proc/$p/fd/2" in command
    assert 'RD stat -c %Y "$c"' in command
    assert 'RD tail -n 80 "$c"' in command
    # socket/pty (screen, unité systemd) → journald par _PID
    assert "journalctl _PID=$p -n 80 --no-pager" in command


def test_log_redirected_by_the_launcher_is_read():
    from app.cpu_stats import attach_cpu_hashrates, parse_cpu_report

    mtime = int(time.time()) - 5
    stdout = (
        "### CPUINFO\nprocessor : 0\nmodel name : AMD Athlon 3000G\ncpu MHz : 3500.000\n"
        "### LOGI 1270 /var/log/athlon-korsh-miner.log\n"
        f"{mtime}\n"
        "[2026-11-16 10:00:00] TTF @ 237.8 Mh/s\n"
    )
    report = parse_cpu_report(stdout)
    assert report["log_pids"]["/var/log/athlon-korsh-miner.log"] == 1270

    probes = {
        "logs": [],
        "apis": [{"port": 4048, "family": "cpuminer", "pid": 1270}],
        "nodes": [],
        "fds": [{"pid": 1270, "family": "cpuminer"}],
    }
    miners = [{"kind": "cpu", "binary": "cpuminer-korsh", "name": "cpuminer",
               "cpu_pct": 198.0, "pid": 1270}]
    attach_cpu_hashrates(miners, probes, report, now=mtime + 5)
    assert miners[0]["hashrate_hs"] == pytest.approx(237.8e6)
    assert miners[0]["hashrate_source"] == "log /var/log/athlon-korsh-miner.log"


def test_socket_fd_is_read_from_journald():
    from app.cpu_stats import attach_cpu_hashrates, parse_cpu_report

    stdout = (
        "### CPUINFO\nprocessor : 0\nmodel name : AMD Ryzen 5 3600\ncpu MHz : 3900.000\n"
        "### JLOG 4242\n"
        "[2026-11-16 10:00:00] speed 10s/60s/15m 1200.0 1180.0 1100.0 H/s max\n"
    )
    report = parse_cpu_report(stdout)
    assert "journal:4242" in report["logs"]
    probes = {"logs": [], "apis": [], "nodes": [],
              "fds": [{"pid": 4242, "family": "xmrig"}]}
    miners = [{"kind": "cpu", "binary": "xmrig", "name": "xmrig", "cpu_pct": 400.0}]
    attach_cpu_hashrates(miners, probes, report, now=time.time())
    assert miners[0]["hashrate_hs"] == pytest.approx(1200.0)
    assert miners[0]["hashrate_source"] == "log journal:4242"


# ── 1.3 — nœud solo : cwd + getmininginfo ─────────────────────────────
BIG_PS = (
    "### PS_MINERS\n"
    "administrator 1104500 0.0 309300 bash -c cd ~/bigcoin-mine/bigcoin && "
    "nohup ./mine-bigcoin.sh > ~/bigcoin-mine/bigcoin/mine.log 2>&1 &\n"
    "administrator 1104502 0.0 309240 bash ./mine-bigcoin.sh\n"
    "administrator 1104510 1096 309240 ./bitcoind -datadir=/home/administrator/"
    "bigcoin-mine/bigcoin/data -daemon\n"
    "administrator 3643649 0.0 25 ./bigcoin-cli -datadir=/home/administrator/"
    "bigcoin-mine/bigcoin/data -rpcclienttimeout=3600 generatetoaddress 1 "
    "big1testaddress 18660\n"
)


def test_relative_cli_is_resolved_from_proc_cwd():
    from app.cpu_stats import plan_cpu_probes
    from app.ssh_client import cpu_probe_command

    probes = plan_cpu_probes(BIG_PS)
    node = next(n for n in probes["nodes"] if n["key"] == "bigcoin")
    assert node["pid"] == 3643649
    assert node["datadir"].endswith("/bigcoin/data")
    command = cpu_probe_command(probes)
    assert "C=$(RD readlink -f /proc/3643649/cwd 2>/dev/null)" in command
    assert 'P="$C/./bigcoin-cli"' in command
    assert 'RD "$P" -datadir="$D" getmininginfo' in command


def test_getmininginfo_feeds_height_and_network_share():
    from app.cpu_stats import attach_cpu_hashrates, parse_cpu_report, plan_cpu_probes

    mtime = int(time.time()) - 3
    stdout = (
        "### NODE bigcoin\n"
        '{"blocks": 21458, "networkhashps": 18400000000, "difficulty": 12.5, '
        '"chain": "main"}\n'
        "### NETPS bigcoin\n18400000000\n"
        "### LOGI 1104502 /home/administrator/bigcoin-mine/bigcoin/mine.log\n"
        f"{mtime}\n"
        "2026-11-16T10:00:00Z scan 200000\n2026-11-16T10:00:10Z scan 200000\n"
    )
    report = parse_cpu_report(stdout)
    probes = plan_cpu_probes(BIG_PS)
    miners = [{"kind": "solo", "coin": "BIG", "name": "Minage solo (nœud)"}]
    attach_cpu_hashrates(miners, probes, report, now=mtime + 5)
    node = miners[0]["node"]
    assert node["height"] == 21458
    assert node["network_hs"] == pytest.approx(18.4e9)
    assert miners[0]["hashrate_hs"] == pytest.approx(20000.0)
    # network_share_pct arrondit à 4 décimales (20 kH/s sur 18,4 GH/s).
    assert node["share_pct"] == pytest.approx(0.0001)


# ── 1.4 — solo KORSH stable, sans client transitoire ──────────────────
RIG_PS = (
    "### PS_MINERS\n"
    "root 4063 92.8 264275 /root/korsh/bin/korshd -datadir=/root/korsh/data -daemon\n"
    "root 4992 0.0 264260 SCREEN -dmS korsh-miner /root/korsh/korsh-miner.sh\n"
    "root 4993 0.0 264260 /bin/bash /root/korsh/korsh-miner.sh\n"
)


def test_solo_korsh_survives_without_the_generatetoaddress_client():
    from app.external_miners import detect_running_miners

    detected = detect_running_miners(RIG_PS)
    solo = [m for m in detected["miners"] if m["kind"] == "solo"]
    assert len(solo) == 1, detected["miners"]
    assert solo[0]["coin"] == "KSH"
    assert solo[0]["binary"] == "korshd"
    assert solo[0]["cpu_pct"] == pytest.approx(92.8)


def test_solo_script_is_detected_when_screen_runs_it():
    from app.external_miners import detect_running_miners

    ps = (
        "### PS_MINERS\n"
        "administrator 1104502 0.0 309240 SCREEN -dmS bigcoin-miner "
        "/home/administrator/bigcoin-mine/mine-bigcoin.sh\n"
    )
    solo = [m for m in detect_running_miners(ps)["miners"] if m["kind"] == "solo"]
    assert solo and solo[0]["coin"] == "BIG"


# ── 1.5 — mineur à l'arrêt : « inactif », pas « inconnu » ─────────────
def test_zero_cpu_miner_is_reported_inactive_with_its_log_date():
    from app.cpu_stats import attach_cpu_hashrates

    mtime = int(time.time()) - 7200
    probes = {
        "logs": [{"path": "/var/log/cpu-miner.log", "family": "oms", "pid": 913316}],
        "apis": [{"port": 4068, "family": "oms", "pid": 913316}],
        "nodes": [],
        "fds": [],
    }
    report = {
        "state": {},
        "logs": {"/var/log/cpu-miner.log": {"mtime": mtime, "text": "starting…\n"}},
        "log_pids": {"/var/log/cpu-miner.log": 913316},
        "apis": {},
        "nodes": {},
    }
    miners = [{"kind": "cpu", "binary": "supryolo", "name": "supryolo", "cpu_pct": 0.0}]
    attach_cpu_hashrates(miners, probes, report, now=mtime + 60)
    note = miners[0]["hashrate_note"]
    assert note.startswith("mineur inactif (0 % CPU, log sans hashrate récent)")
    assert "log modifié le" in note
    assert miners[0]["log_mtime"]


# ── 1.6 — fréquence et puissance ──────────────────────────────────────
def test_cpuinfo_parser_accepts_tabs_and_case():
    from app.cpu_stats import parse_cpuinfo

    text = (
        "processor\t: 0\nmodel name\t: AMD Ryzen 5 3600 6-Core\ncpu MHz\t\t: 3900.000\n"
        "processor\t: 1\nmodel name\t: AMD Ryzen 5 3600 6-Core\nCPU MHz\t\t: 4100.000\n"
    )
    parsed = parse_cpuinfo(text)
    assert parsed["cores"] == 2
    assert parsed["freq_mhz"] == pytest.approx(4000.0)
    assert parsed["model"].startswith("AMD Ryzen")


def test_rapl_is_read_through_sudo_in_the_command():
    from app.ssh_client import cpu_probe_command

    command = cpu_probe_command({})
    assert 'RD cat "$f"' in command
    assert "for f in /sys/class/powercap/*/energy_uj" in command
    assert "### POWER2" in command
    assert "### POWERH" in command


def test_rapl_pair_gives_package_power():
    from app.cpu_stats import parse_cpu_report

    stdout = (
        "### POWER\n"
        "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj|1000000000\n"
        "### POWER2\n"
        "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj|1020000000\n"
    )
    report = parse_cpu_report(stdout)
    assert report["state"]["power_w"] == pytest.approx(20.0)
    assert report["state"]["power_source"] == "rapl"


def test_amd_energy_hwmon_is_the_third_power_source():
    from app.cpu_stats import parse_cpu_report

    report = parse_cpu_report("### POWERH\namd_energy|power1_input|88960000\n")
    assert report["state"]["power_w"] == pytest.approx(88.96)
    assert report["state"]["power_source"] == "hwmon amd_energy power1_input"


def test_srbminer_package_power_is_read_from_cpu_devices():
    from app.srbminer_api import parse_srbminer_api

    api = parse_srbminer_api(fixture("srbminer_383_ryzen.json"))
    assert api is not None
    assert api["cpu_package_power_w"] == pytest.approx(88.96)


def test_srbminer_power_wins_and_is_labelled():
    from app.cpu_stats import cpu_state_with

    state = cpu_state_with({"state": {"freq_mhz": 3900.0}}, None, 88.96, [])
    assert state["power_w"] == pytest.approx(88.96)
    assert state["power_source"] == "API SRBMiner package_power"
    assert state["freq_mhz"] == pytest.approx(3900.0)


# ── Bout en bout : collecte → base → /api/metrics/live ────────────────
def probe_athlon(mtime: int) -> str:
    return (
        fixture("proc_stat_pair.txt")
        + "### CPUINFO\nprocessor : 0\nmodel name : AMD Athlon 3000G\ncpu MHz : 3500.000\n"
        "### HWMON\n" + fixture("hwmon_k10temp.txt")
        + "### POWER\n"
        "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj|1000000\n"
        "### POWER2\n"
        "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj|1020000\n"
        "### LOGI 1270 /var/log/athlon-korsh-miner.log\n"
        f"{mtime}\n[2026-11-16 10:00:00] TTF @ 237.8 Mh/s\n"
    )


def probe_ryzen(mtime: int) -> str:
    return (
        "### CPUINFO\nprocessor : 0\nmodel name : AMD Ryzen 5 3600\ncpu MHz : 3900.000\n"
        "### POWERH\namd_energy|power1_input|50000000\n"
        "### NODE bigcoin\n"
        '{"blocks": 21458, "networkhashps": 18400000000, "chain": "main"}\n'
        "### NETPS bigcoin\n18400000000\n"
        "### LOGI 913316 /var/log/cpu-miner.log\n"
        f"{mtime - 7200}\nstarting…\n"
        "### LOGI 1104502 /home/administrator/bigcoin-mine/bigcoin/mine.log\n"
        f"{mtime}\n"
        "2026-11-16T10:00:00Z scan 200000\n2026-11-16T10:00:10Z scan 200000\n"
    )


def probe_rig(mtime: int) -> str:
    return (
        "### CPUINFO\nprocessor : 0\nmodel name : AMD Ryzen 7 3700X\ncpu MHz : 3600.000\n"
        "### NODE korsh\n"
        '{"blocks": 99000, "networkhashps": 18400000000, "chain": "main"}\n'
        "### NETPS korsh\n18400000000\n"
        f"### LOGI 4993 /root/korsh/mining.log\n{mtime}\nTTF @ 1.2 Mh/s\n"
    )


@pytest.fixture
def seeded_machines():
    from app.database import SessionLocal
    from app.models import Device, Machine

    ids: dict[str, int] = {}
    db = SessionLocal()
    try:
        for name in ("athlon-c8", "ryzen-c8", "rig-c8"):
            machine = Machine(
                name=name, host="192.0.2.11", ssh_port=22, ssh_user="tester",
                auth_type="password", usage="both", enabled=True,
                uuid=f"uuid-c8-{name}",
            )
            db.add(machine)
            db.commit()
            db.refresh(machine)
            db.add(Device(
                machine_id=machine.id, uuid=f"uuid-c8-cpu-{name}", kind="cpu",
                index=None, vendor="Fake", model="Faux processeur", present=True,
                enabled_for_mining=True,
            ))
            db.commit()
            ids[name] = machine.id
        return ids
    finally:
        db.close()


def patch_collection(monkeypatch):
    from app import collector, ssh_client
    from app.srbminer_api import parse_srbminer_api

    ps_by_name = {
        "athlon-c8": fixture("ps_athlon_c4.txt"),
        "ryzen-c8": fixture("ps_ryzen_c4.txt"),
        "rig-c8": RIG_PS,
    }
    mtime = int(time.time()) - 5
    probe_by_name = {
        "athlon-c8": probe_athlon(mtime),
        "ryzen-c8": probe_ryzen(mtime),
        "rig-c8": probe_rig(mtime),
    }

    async def fake_collect_stats(machine, actor="system"):
        return {"ok": True, "stdout": ps_by_name[machine.name]}

    async def fake_collect_cpu(machine, probes, actor="system"):
        fake_collect_cpu.probes = probes
        return {"ok": True, "stdout": probe_by_name[machine.name]}

    async def fake_miner_api(machine, port, api_kind=""):
        if machine.name == "ryzen-c8":
            return parse_srbminer_api(fixture("srbminer_383_ryzen.json")), None
        return None, "port fermé (test)"

    async def no_detect(machine, actor="system"):
        return {"ok": False, "error": "pas de détection dans ce test"}

    monkeypatch.setattr(ssh_client, "ssh_collect_stats", fake_collect_stats)
    monkeypatch.setattr(ssh_client, "ssh_collect_cpu", fake_collect_cpu)
    monkeypatch.setattr(ssh_client, "ssh_detect_hw", no_detect)
    monkeypatch.setattr(collector, "_fetch_miner_api", fake_miner_api)
    return mtime


class TestCollectC8:
    async def test_three_machines_end_to_end(self, seeded_machines, monkeypatch, client):
        from app import collector

        patch_collection(monkeypatch)
        for machine_id in seeded_machines.values():
            result = await collector.collect_machine(machine_id)
            assert result["online"] is True, result

        response = await client.get("/api/metrics/live")
        assert response.status_code == 200
        machines = {m["name"]: m for m in response.json()}

        # athlon : cpuminer KSH avec un hashrate lu dans son log redirigé.
        athlon = machines["athlon-c8"]
        cpuminer = next(
            m for m in athlon["external_miners"]
            if m["kind"] == "cpu" and "cpuminer" in (m["binary"] or "")
        )
        assert cpuminer["hashrate_hs"] == pytest.approx(237.8e6)
        assert cpuminer["hashrate_source"].startswith("log /var/log/athlon-korsh-miner.log")
        assert athlon["cpu"]["freq_mhz"] == pytest.approx(3500.0)
        assert athlon["cpu"]["power_source"] == "rapl"

        # ryzen : solo BIG (hauteur, réseau, part), supryolo inactif, 88,96 W.
        ryzen = machines["ryzen-c8"]
        solo = next(m for m in ryzen["external_miners"] if m["kind"] == "solo")
        assert solo["coin"] == "BIG"
        assert solo["node"]["height"] == 21458
        assert solo["node"]["network_hs"] == pytest.approx(18.4e9)
        assert solo["node"]["share_pct"] > 0
        supryolo = next(
            m for m in ryzen["external_miners"] if "supryolo" in (m["binary"] or "")
        )
        assert supryolo["hashrate_note"].startswith("mineur inactif (0 % CPU")
        assert ryzen["cpu"]["freq_mhz"] == pytest.approx(3900.0)
        assert ryzen["cpu"]["power_w"] == pytest.approx(88.96)
        assert ryzen["cpu"]["power_source"] == "API SRBMiner package_power"

        # rig : solo KSH présent, même sans client transitoire.
        rig = machines["rig-c8"]
        solo = next(m for m in rig["external_miners"] if m["kind"] == "solo")
        assert solo["coin"] == "KSH"
        assert solo["node"]["height"] == 99000
        assert rig["cpu"]["freq_mhz"] == pytest.approx(3600.0)
        assert rig["cpu"]["hashrate_hs"] == pytest.approx(1.2e6)

    async def test_raw_collection_contains_the_cpu_probe(self, seeded_machines, monkeypatch):
        from app import collector
        from app.database import SessionLocal
        from app.models import RawCollection

        patch_collection(monkeypatch)
        await collector.collect_machine(seeded_machines["athlon-c8"])

        db = SessionLocal()
        try:
            raw = db.query(RawCollection).one()
            assert raw.stdout.startswith("### CPU_PROBE")
            assert "### PROC_STAT" in raw.stdout
            assert "### CPUINFO" in raw.stdout
            # toujours masquée : aucun wallet en clair.
            assert "prl1testwallet000000000000000000" not in raw.stdout
        finally:
            db.close()
