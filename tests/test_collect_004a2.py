"""004-a2 — hashrate du solo BIG au format réel du log de ``mine-bigcoin.sh``.

Format relevé sur la machine le 17/11/2026 (aucune adresse dans ces lignes) :

    [21:53:30] mining ... height=20353 difficulty=0.003300662640699338 \
    algo=yespower 629 hashes/s/worker (~6919 total over 11 workers)

Le script écrit dans ``mine.log``, retrouvé par ``readlink /proc/<pid>/fd/1``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"
LOG_PATH = "/home/administrator/bigcoin-mine/mine.log"


def local_epoch_at(stamp: str, now: float | None = None) -> float:
    """Epoch local du jour à HH:MM:SS (l'horodatage du script est local)."""
    hour, minute, second = (int(part) for part in stamp.split(":"))
    base = time.localtime(time.time() if now is None else now)
    return time.mktime(
        (base.tm_year, base.tm_mon, base.tm_mday, hour, minute, second, 0, 0, -1)
    )


def test_script_hashrate_reproduces_the_real_log():
    from app.cpu_stats import script_hashrate

    text = (FIX / "solo_big_log.txt").read_text()
    result = script_hashrate(text, now=local_epoch_at("21:56:30"))

    assert result["hashrate_hs"] == pytest.approx(6952.0)
    assert result["workers"] == 11
    assert result["algo"] == "yespower"
    assert result["height"] == 20353
    assert result["difficulty"] == pytest.approx(0.003300662640699338)
    assert result["state"] == "ok"
    assert result["age_s"] == 90
    assert "6952 total over 11 workers" in result["line"]


def test_script_hashrate_rejects_a_frozen_log():
    from app.cpu_stats import script_hashrate

    text = (FIX / "solo_big_log.txt").read_text()
    stale = script_hashrate(text, now=local_epoch_at("21:56:30") + 3600)
    assert stale["hashrate_hs"] is None
    assert stale["state"] == "stale"
    assert "figé" in stale["note"]

    # Le mtime frais suffit quand l'horodatage est vieux : fichier vivant.
    fresh = script_hashrate(text, mtime=int(time.time()) - 5)
    assert fresh["hashrate_hs"] == pytest.approx(6952.0)


def test_unknown_format_yields_no_hashrate():
    from app.cpu_stats import script_hashrate

    result = script_hashrate("2026-11-16T22:00:00Z 524288 nonces")
    assert result["hashrate_hs"] is None
    assert result["state"] == "unknown"


def test_attach_solo_sets_workers_algo_and_source():
    from app.cpu_stats import _attach_solo

    text = (FIX / "solo_big_log.txt").read_text()
    miner = {"kind": "solo", "coin": "BIG", "workers": 3, "hashrate_hs": None,
             "hashrate_source": "", "hashrate_note": ""}
    probes = {"nodes": [{"coin": "BIG", "key": "bigcoin",
                         "log_candidates": [LOG_PATH], "pids": []}]}
    report = {
        "logs": {LOG_PATH: {"mtime": int(time.time()) - 5, "text": text}},
        "log_pids": {},
        "nodes": {"bigcoin": {"height": 20353, "network_hs": 18.4e9}},
        "apis": {},
    }

    _attach_solo(miner, probes, report)

    assert miner["hashrate_hs"] == pytest.approx(6952.0)
    assert miner["workers"] == 11
    assert miner["algorithm"] == "yespower"
    assert miner["hashrate_source"].startswith("log du script")
    assert miner["node"]["algo"] == "yespower"
    assert miner["node"]["difficulty"] == pytest.approx(0.003300662640699338)
    assert miner["node"]["log"] == LOG_PATH


async def test_solo_big_end_to_end(client, monkeypatch):
    """Collecte → base → /api/metrics/live : 6952 H/s, 11 workers, yespower."""
    from app import collector, ssh_client
    from app.database import SessionLocal
    from app.models import Device, Machine

    mtime = int(time.time()) - 5
    ps = (FIX / "ps_ryzen_c4.txt").read_text()
    probe = (
        "### CPUINFO\nprocessor : 0\nmodel name : AMD Ryzen 5 3600\ncpu MHz : 3900.000\n"
        "### NODE bigcoin\n"
        '{"blocks": 20353, "networkhashps": 18400000000, "chain": "main"}\n'
        "### NETPS bigcoin\n18400000000\n"
        # Le log réel n'est pas deviné depuis le datadir : il vient de
        # /proc/<pid du script>/fd/1 (pid d'un client, rattaché au nœud).
        f"### LOGI 3643649 {LOG_PATH}\n{mtime}\n"
        + (FIX / "solo_big_log.txt").read_text()
    )

    async def fake_collect_stats(machine, actor="system"):
        return {"ok": True, "stdout": ps}

    async def fake_collect_cpu(machine, probes, actor="system"):
        return {"ok": True, "stdout": probe}

    async def no_api(machine, port):
        return None, "port fermé (test)"

    async def no_detect(machine, actor="system"):
        return {"ok": False, "error": "pas de détection dans ce test"}

    monkeypatch.setattr(ssh_client, "ssh_collect_stats", fake_collect_stats)
    monkeypatch.setattr(ssh_client, "ssh_collect_cpu", fake_collect_cpu)
    monkeypatch.setattr(ssh_client, "ssh_detect_hw", no_detect)
    monkeypatch.setattr(collector, "_fetch_miner_api", no_api)

    db = SessionLocal()
    try:
        machine = Machine(
            name="big-a2", host="192.0.2.90", ssh_port=22, ssh_user="tester",
            auth_type="password", usage="cpu", enabled=True, uuid="uuid-a2-big",
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(
            machine_id=machine.id, uuid="uuid-a2-cpu", kind="cpu", index=None,
            vendor="Fake", model="Faux processeur", present=True,
            enabled_for_mining=True,
        ))
        db.commit()
        machine_id = machine.id
    finally:
        db.close()

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True, result

    live = (await client.get("/api/metrics/live")).json()
    rows = live if isinstance(live, list) else live.get("machines")
    machines = {row["name"]: row for row in rows}
    solo = next(m for m in machines["big-a2"]["external_miners"] if m["kind"] == "solo")

    assert solo["coin"] == "BIG"
    assert solo["hashrate_hs"] == pytest.approx(6952.0)
    assert solo["workers"] == 11
    assert solo["algorithm"] == "yespower"
    assert solo["hashrate_source"].startswith("log du script")
    assert solo["node"]["height"] == 20353
    assert solo["node"]["algo"] == "yespower"
    assert solo["node"]["log"] == LOG_PATH
