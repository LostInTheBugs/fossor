"""Bout en bout 2026.11.003-c7 : collecte → base → ``/api/metrics/live``.

Aucune connexion réelle : ``ssh_collect_stats`` et ``ssh_collect_cpu`` sont
remplacés par les fixtures (ps réel anonymisé + sections de la sonde CPU).
Chaque mineur CPU doit ressortir avec un hashrate **ou** une raison explicite,
et chaque CPU avec sa charge et sa température.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def cpu_probe_stdout(log_path: str, log_text: str, mtime: int, extra: str = "") -> str:
    """Sortie réaliste de ``collect_cpu`` : deux /proc/stat, hwmon, RAPL, log."""
    return (
        fixture("proc_stat_pair.txt")
        + "\n### CPUINFO\nprocessor : 0\nmodel name : AMD Ryzen 5 3600 6-Core\n"
        "cpu MHz : 3900.000\nprocessor : 1\nmodel name : AMD Ryzen 5 3600 6-Core\n"
        "cpu MHz : 3900.000\n"
        + "### FREQ\n3900000\n3900000\n"
        + fixture("hwmon_k10temp.txt")
        + "\n"
        + fixture("rapl_pair.txt")
        + f"\n### LOG {log_path}\n{mtime}\n{log_text}"
        + extra
    )


@pytest.fixture
def seeded_machine():
    """Machine + CPU device, sans détection SSH (aucune connexion réelle)."""
    from app.database import SessionLocal
    from app.models import Device, Machine

    db = SessionLocal()
    try:
        machine = Machine(
            name="ryzen-test", host="192.0.2.10", ssh_port=22, ssh_user="tester",
            auth_type="password", usage="both", enabled=True, uuid="uuid-c7-ryzen",
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(
            machine_id=machine.id, uuid="uuid-c7-cpu", kind="cpu", index=None,
            vendor="AMD", model="Ryzen 5 3600", present=True, enabled_for_mining=True,
        ))
        db.commit()
        return machine.id
    finally:
        db.close()


def patch_ssh(monkeypatch, ps_section: str, probe_stdout: str):
    from app import collector, ssh_client

    async def fake_collect_stats(machine, actor="system"):
        return {"ok": True, "stdout": ps_section}

    async def fake_collect_cpu(machine, probes, actor="system"):
        fake_collect_cpu.probes = probes
        return {"ok": True, "stdout": probe_stdout}

    async def fake_miner_api(machine, port, actor="system"):
        return {"ok": False, "error": "port fermé (test)"}

    async def no_api(host, port):
        return None

    async def no_detect(machine, actor="system"):
        return {"ok": False, "error": "pas de détection dans ce test"}

    monkeypatch.setattr(ssh_client, "ssh_collect_stats", fake_collect_stats)
    monkeypatch.setattr(ssh_client, "ssh_collect_cpu", fake_collect_cpu)
    monkeypatch.setattr(ssh_client, "ssh_miner_api", fake_miner_api)
    monkeypatch.setattr(ssh_client, "ssh_detect_hw", no_detect)
    monkeypatch.setattr(collector, "fetch_srbminer", no_api)
    return fake_collect_cpu


class TestCollectC7:
    async def test_cpu_miners_get_a_hashrate_or_a_reason(self, seeded_machine, monkeypatch):
        from app import collector
        from app.database import SessionLocal
        from app.models import Machine, MachineStatus

        ps = fixture("ps_ryzen_c4.txt")
        probe = cpu_probe_stdout(
            "/var/log/cpu-miner.log", fixture("cpu_wam_log.txt"), mtime=4_000_000_000
        )
        patch_ssh(monkeypatch, ps, probe)

        result = await collector.collect_machine(seeded_machine)
        assert result["online"] is True, result

        db = SessionLocal()
        try:
            machine = db.query(Machine).filter(Machine.id == seeded_machine).one()
            miners = json.loads(machine.external_miners)
            cpu_miners = [m for m in miners if m["kind"] in ("cpu", "solo")]
            assert cpu_miners, miners
            for miner in cpu_miners:
                assert miner["hashrate_hs"] or miner["hashrate_note"], miner
            status = db.query(MachineStatus).one()
            cpu = json.loads(status.cpu_json)
            assert cpu["load_pct"] == pytest.approx(67.5, abs=0.2)
            assert cpu["temp_c"] == 68.0
            assert cpu["freq_mhz"] == pytest.approx(3900.0)
            assert cpu["power_w"] == pytest.approx(9.82, abs=0.01)
            assert cpu["power_source"] == "rapl"
            assert cpu["miners"], "au moins un mineur CPU dans l'état du CPU"
        finally:
            db.close()

    async def test_live_api_exposes_cpu_and_coin_hashrates(self, seeded_machine, monkeypatch, client):
        from app import collector

        ps = fixture("ps_ryzen_c4.txt")
        probe = cpu_probe_stdout(
            "/var/log/cpu-miner.log", fixture("cpu_wam_log.txt"), mtime=4_000_000_000
        )
        patch_ssh(monkeypatch, ps, probe)
        await collector.collect_machine(seeded_machine)

        response = await client.get("/api/metrics/live")
        assert response.status_code == 200
        payload = response.json()
        machine = next(m for m in payload if m["name"] == "ryzen-test")

        cpu = machine["cpu"]
        assert cpu["load_pct"] == pytest.approx(67.5, abs=0.2)
        assert cpu["temp_c"] == 68.0
        assert cpu["freq_mhz"] == pytest.approx(3900.0)
        # Le CPU est bien le device cpu de la machine.
        cpu_device = next(d for d in machine["devices"] if d["kind"] == "cpu")
        assert cpu_device["load_pct"] == pytest.approx(67.5, abs=0.2)
        assert cpu_device["temp_c"] == 68.0

        # Chaque mineur CPU : hashrate ou raison explicite.
        imported = [m for m in machine["external_miners"] if m["kind"] in ("cpu", "solo")]
        assert imported
        for miner in imported:
            assert miner["hashrate_hs"] or miner["hashrate_note"]

    async def test_wam_log_hashrate_feeds_the_coin_card(self, seeded_machine, monkeypatch, client):
        """Le rig : log wam-miner frais → carte WAM avec un vrai hashrate."""
        from app import collector

        ps = (
            "### PS_MINERS\n"
            "root 697 0.0 264328 SCREEN -dmS wam bash -c exec /root/wam/wam-miner "
            "-o stratum+tcp://pool.wamcoin.org:3333 -u wam1testwallet000.rig -t 2 "
            ">> /root/wam/miner.log 2>&1\n"
            "root 699 191 264328 /root/wam/wam-miner -o stratum+tcp://pool.wamcoin.org:3333 "
            "-u wam1testwallet000.rig -t 2\n"
        )
        probe = cpu_probe_stdout(
            "/root/wam/miner.log", fixture("cpu_wam_log.txt"), mtime=4_000_000_000
        )
        patch_ssh(monkeypatch, ps, probe)
        await collector.collect_machine(seeded_machine)

        response = await client.get("/api/metrics/live")
        machine = next(m for m in response.json() if m["name"] == "ryzen-test")
        groups = {group["coin"]: group for group in machine["coin_groups"]}
        assert "WAM" in groups, machine["coin_groups"]
        assert groups["WAM"]["hashrate_hs"] == pytest.approx(1210.0)
        miner = next(m for m in machine["external_miners"] if m["coin"] == "WAM")
        assert miner["hashrate_source"] == "log /root/wam/miner.log"

    async def test_stale_log_is_reported_not_shown(self, seeded_machine, monkeypatch, client):
        """Un log figé (mineur arrêté) ne doit pas afficher son dernier hashrate."""
        from app import collector

        ps = (
            "### PS_MINERS\n"
            "root 699 0.0 264328 /root/wam/wam-miner -o stratum+tcp://pool.wamcoin.org:3333 "
            "-u wam1testwallet000.rig -t 2 >> /root/wam/miner.log\n"
        )
        probe = cpu_probe_stdout(
            "/root/wam/miner.log", fixture("cpu_wam_log.txt"), mtime=1_000
        )
        patch_ssh(monkeypatch, ps, probe)
        await collector.collect_machine(seeded_machine)

        response = await client.get("/api/metrics/live")
        machine = next(m for m in response.json() if m["name"] == "ryzen-test")
        miner = machine["external_miners"][0]
        assert miner["hashrate_hs"] is None
        assert "figé" in miner["hashrate_note"]

    async def test_probe_plan_never_sends_a_hostile_path(self, seeded_machine, monkeypatch):
        from app import collector
        from app.ssh_client import cpu_probe_command

        ps = (
            "### PS_MINERS\n"
            "root 12 99 100 /usr/bin/cpuminer -o stratum+tcp://x:1 >> /root/x/$(id).log\n"
        )
        fake = patch_ssh(monkeypatch, ps, "")
        await collector.collect_machine(seeded_machine)
        command = cpu_probe_command(fake.probes)
        assert "$(id)" not in command
        assert "for p in 12" in command  # la redirection est résolue par /proc/<pid>/fd/1
