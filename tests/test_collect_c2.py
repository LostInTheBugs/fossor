"""Correctif 2026.11.003-c2: collecte -> base -> API, mineurs CPU/GPU.

Fixtures texte entièrement fictives (IP 192.0.2.x, wallets ``prl1test…``).
Aucune connexion à un rig, à la prod ou à une vraie machine.
"""

from __future__ import annotations

import json

import pytest

RAW = """### HIVE_STAT
### HIVE_GPU
### NVSMI
0, 00000000:01:00.0, 55, 120.5, 60, 100
1, 00000000:02:00.0, 51, 110.0, 55, 98
### PS_MINERS
root       4242 340.0 7200 srbminer-multi --algorithm pearlhash --pool pearl-eu2.luckypool.io:3360 --wallet prl1testwallet0000000000000000000000000000000000000000000000 --password s3cret --api-port 21550
root       5151 350.0 7200 /usr/bin/cpuminer-opt -a yespower -o stratum+tcp://korsh.luckypool.io:3333 -u prl1kshtestwallet000000000000000000000000000000 --password hunter2 -t 4
fred       6001 120.5  900 /opt/wam-miner -a yespower -o wamcoin.luckypool.io:8080 -u prl1wamtestwallet0000000000000000000000000000 --password topsecret -t 2
fred       8001  99.0   30 bash /home/fred/mine-x.sh
### UPTIME
12345.67 99999.00
"""

SRB_API = {
    "algorithm": "pearlhash",
    "coin": "PRL",
    "hashrate": {"total": 8.5e9, "devices": [4.2e9, 4.3e9]},
    "shares": {"accepted": 10, "rejected": 0},
}

HOSTS = {"pearl": "PRL", "korsh": "KSH", "wamcoin": "WAM"}


def seed_machine(host="192.0.2.10", name="rig-c2"):
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
            uuid="11111111-1111-4111-8111-111111111111",
            machine_id=machine.id, kind="cpu", model="Ryzen 5 3600", index=0,
        ))
        for position, bus in enumerate(("01:00.0", "02:00.0")):
            db.add(Device(
                uuid=f"22222222-2222-4222-8222-00000000000{position}",
                machine_id=machine.id, kind="gpu",
                model=f"Radeon RX 60{position}0", index=position,
                pci_bus=bus, vendor="AMD",
            ))
        for ticker, hosts in (
            ("PRL", ["pearl.luckypool.io", "pearl"]),
            ("KSH", ["korsh.luckypool.io", "korsh"]),
            ("WAM", ["wamcoin.luckypool.io", "wamcoin"]),
        ):
            if db.query(Coin).filter(Coin.ticker == ticker).first() is None:
                db.add(Coin(ticker=ticker, name=ticker, pool_hosts=hosts))
        db.commit()
        return machine.id
    finally:
        db.close()


def test_parse_raw_detects_gpu_and_cpu_miners():
    from app.stats_parser import parse_collect_stats

    stats = parse_collect_stats(RAW, coin_hosts=HOSTS)
    names = [m["name"] for m in stats["miners"]]
    assert "SRBMiner-MULTI" in names
    assert "cpuminer-opt" in names
    assert "wam-miner" in names
    assert "Script de minage" in names
    assert len(stats["cpu_miners"]) == 3  # cpuminer, wam-miner, script bash
    assert stats["coin"] == "PRL"
    by_name = {m["name"]: m for m in stats["miners"]}
    assert by_name["cpuminer-opt"]["algorithm"] == "yespower"
    assert by_name["cpuminer-opt"]["threads"] == "4"
    assert by_name["cpuminer-opt"]["coin"] == "KSH"
    assert by_name["wam-miner"]["coin"] == "WAM"
    assert by_name["cpuminer-opt"]["user"] == "root"
    assert by_name["cpuminer-opt"]["cpu_pct"] == pytest.approx(350.0)
    assert "hunter2" not in json.dumps(stats)
    assert "prl1testwallet0000000000" not in json.dumps(stats)
    cpu = [d for d in stats["devices"] if d["kind"] == "cpu"][0]
    assert cpu["occupied_external"] is True


async def test_collect_stores_miners_and_api(tmp_path, monkeypatch, client):
    machine_id = seed_machine()
    from app import collector

    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": RAW}

    async def fake_api(host, port):
        assert port == 21550
        return {
            "miner": "srbminer",
            "algo": "pearlhash",
            "coin": "PRL",
            "total_hashrate_hs": 8.5e9,
            "device_hashrates_hs": [4.2e9, 4.3e9],
            "accepted": 10,
            "rejected": 0,
        }

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    monkeypatch.setattr(collector, "fetch_srbminer", fake_api)

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True

    from app.database import SessionLocal
    from app.models import Device, Machine, MachineStatus, Metric, RawCollection

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).one()
        miners = json.loads(machine.external_miners)
        assert len(miners) == 4
        assert {m["name"] for m in miners} >= {"SRBMiner-MULTI", "cpuminer-opt", "wam-miner"}
        blob = json.dumps(miners)
        assert "s3cret" not in blob and "hunter2" not in blob and "topsecret" not in blob
        assert "prl1testwallet0000000000" not in blob
        cpu = db.query(Device).filter(Device.machine_id == machine_id, Device.kind == "cpu").one()
        gpus = db.query(Device).filter(Device.machine_id == machine_id, Device.kind == "gpu").all()
        assert cpu.occupied_external is True
        assert all(g.occupied_external for g in gpus)
        status = db.query(MachineStatus).one()
        assert status.api_error is None
        assert status.total_hashrate_hs == pytest.approx(8.5e9)
        gpu_metrics = [
            m for m in db.query(Metric).all()
            if m.device_uuid and m.device_uuid.startswith("22222222")
        ]
        assert sorted(m.hashrate_hs for m in gpu_metrics) == pytest.approx([4.2e9, 4.3e9])
        raw_row = db.query(RawCollection).one()
        assert "### PS_MINERS" in raw_row.stdout
        assert "hunter2" not in raw_row.stdout and "prl1testwallet0000000000" not in raw_row.stdout
    finally:
        db.close()

    response = await client.get("/api/metrics/live")
    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["external_miners"], payload
    assert payload["api_error"] is None
    assert payload["total_hashrate_hs"] == pytest.approx(8.5e9)
    occupied = {d["kind"]: d["occupied_external"] for d in payload["devices"]}
    assert occupied == {"cpu": True, "gpu": True}
    assert "prl1testwallet0000000000" not in json.dumps(payload)


async def test_api_unreachable_is_reported(monkeypatch):
    machine_id = seed_machine(name="rig-c2-down", host="192.0.2.11")
    from app import collector

    async def fake_ssh(machine, actor=None):
        return {"ok": True, "stdout": RAW}

    async def no_direct(host, port):
        return None

    async def no_ssh(machine, port, actor="system"):
        return {"ok": False, "error": "API du mineur injoignable"}

    monkeypatch.setattr("app.ssh_client.ssh_collect_stats", fake_ssh)
    monkeypatch.setattr(collector, "fetch_srbminer", no_direct)
    monkeypatch.setattr("app.ssh_client.ssh_miner_api", no_ssh)
    await collector.collect_machine(machine_id)

    from app.database import SessionLocal
    from app.models import MachineStatus

    db = SessionLocal()
    try:
        status = db.query(MachineStatus).one()
        assert status.api_error == "API du mineur injoignable"
    finally:
        db.close()


async def test_patterns_are_configurable(client):
    response = await client.get("/api/metrics/miner-patterns")
    assert response.status_code == 200
    assert any(p["name"] == "cpuminer-opt" for p in response.json()["patterns"])

    saved = await client.put(
        "/api/metrics/miner-patterns",
        json={"patterns": [{"name": "Mineur maison", "pattern": "mine-x\\.sh"}]},
    )
    assert saved.status_code == 200
    assert saved.json()["patterns"] == [{"name": "Mineur maison", "pattern": "mine-x\\.sh"}]

    from app.stats_parser import parse_collect_stats

    stats = parse_collect_stats(
        RAW, patterns=[{"name": "Mineur maison", "pattern": "mine-x\\.sh"}]
    )
    assert [m["name"] for m in stats["miners"]] == ["Mineur maison"]
