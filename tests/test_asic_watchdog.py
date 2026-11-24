"""Tests §6.8 — section ASIC du watchdog (aucun ASIC réel contacté)."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import asic, asic_client, asic_watchdog, boost_runner, pilotage, watchdog
from app.database import SessionLocal
from app.models import Boost, Device, Machine, UsageEnum, WatchdogEvent


def _machine(db, driver="axeos", nominal=1.4e12):
    machine = Machine(
        name=f"bitaxe-test-{driver}", host="192.0.2.10", machine_kind="asic",
        asic_driver=driver, asic_expected_hashrate_hs=nominal,
        ssh_port=0, ssh_user="", usage=UsageEnum.none, enabled=True,
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def _config(**asic):
    return watchdog.normalize({"asic": dict(asic)})


def _state(total=4, inactive=1, hashrate=1.39e12, uptime=10):
    return {"online": True, "domains_total": total, "domains_inactive": inactive,
            "hashrate_hs": hashrate, "uptime_s": uptime}


def _run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _real_mode(monkeypatch):
    monkeypatch.setattr(watchdog, "_is_simulated", lambda db, machine: False)


def _noop_probe(session, target):
    return _state(inactive=0, hashrate=1.4e12)


def test_domaines_incomplets_pendant_n_minutes_puis_redemarrage(db):
    machine = _machine(db)
    calls = []

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=0, hashrate=1.4e12)

    async def noop(_seconds):
        return None

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10, verify_seconds=15)
    assert _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg,
                                       restart=restart, probe=probe, sleep=noop)) == []
    events = _run(asic_watchdog.evaluate(
        db, machine, _state(), now=start + timedelta(minutes=10), config=cfg,
        restart=restart, probe=probe, sleep=noop))
    assert calls == [machine.id]
    assert events[0]["action"] == "restart_asic"
    rows = db.query(WatchdogEvent).filter(WatchdogEvent.machine_id == machine.id).all()
    assert any(row.action == "restart_asic" for row in rows)
    assert any(row.rule == "asic_verify" and row.result == "restored" for row in rows)


def test_domaines_complets_ne_declenchent_rien(db):
    machine = _machine(db)
    cfg = _config(restart=True)
    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    events = _run(asic_watchdog.evaluate(
        db, machine, _state(inactive=0, hashrate=1.4e12), now=start, config=cfg))
    assert events == []
    assert db.query(WatchdogEvent).filter(WatchdogEvent.machine_id == machine.id).count() == 0


def test_limite_trois_redemarrages_puis_intervention_manuelle(db):
    machine = _machine(db)
    calls = []

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=1, hashrate=1.39e12)  # jamais rétabli

    async def noop(_seconds):
        return None

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10, min_delay_minutes=15, max_per_day=3,
                  verify_seconds=15)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg,
                                restart=restart, probe=probe, sleep=noop))
    for minutes in (10, 30, 50, 70):
        _run(asic_watchdog.evaluate(
            db, machine, _state(), now=start + timedelta(minutes=minutes),
            config=cfg, restart=restart, probe=probe, sleep=noop))
    assert len(calls) == 3
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1)) == 3
    assert asic_watchdog.is_red(db, machine.id) is True
    rows = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.result == "manual").all()
    assert rows, "la notification « intervention manuelle nécessaire » doit être journalisée"
    assert asic_watchdog.MANUAL_TEXT in rows[0].detail


def test_delai_minimum_entre_deux_redemarrages(db):
    machine = _machine(db)
    calls = []

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=1)

    async def noop(_seconds):
        return None

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10, min_delay_minutes=15, verify_seconds=15)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg,
                                restart=restart, probe=probe, sleep=noop))
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start + timedelta(minutes=10),
                                config=cfg, restart=restart, probe=probe, sleep=noop))
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start + timedelta(minutes=20),
                                config=cfg, restart=restart, probe=probe, sleep=noop))
    assert len(calls) == 1


def test_verification_ratee_puis_tentative_suivante(db):
    machine = _machine(db)
    calls = []

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=1)

    async def noop(_seconds):
        return None

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10, min_delay_minutes=15, verify_seconds=15)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg,
                                restart=restart, probe=probe, sleep=noop))
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start + timedelta(minutes=10),
                                config=cfg, restart=restart, probe=probe, sleep=noop))
    failed = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.rule == "asic_verify").all()
    assert failed and failed[-1].result == "failed"
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1)) == 1
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start + timedelta(minutes=30),
                                config=cfg, restart=restart, probe=probe, sleep=noop))
    assert len(calls) == 2


def test_simulation_produit_une_action_simulee(db, monkeypatch):
    machine = _machine(db)
    calls = []
    monkeypatch.setattr(watchdog, "_is_simulated", lambda db, machine: True)

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=0, hashrate=1.4e12)

    async def noop(_seconds):
        return None

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg, restart=restart))
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start + timedelta(minutes=10),
                                config=cfg, restart=restart, probe=probe, sleep=noop))
    assert calls == []
    row = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.action == "restart_asic").one()
    assert row.simulated is True
    verified = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.rule == "asic_verify").one()
    assert verified.result == "restored" and verified.simulated is True


def test_iceriver_notification_seule(db):
    machine = _machine(db, driver="iceriver")
    calls = []

    async def restart(target):
        calls.append(target.id)

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg, restart=restart))
    events = _run(asic_watchdog.evaluate(
        db, machine, _state(), now=start + timedelta(minutes=10), config=cfg, restart=restart))
    assert calls == []
    assert events[0]["result"] == "unsupported"
    row = db.query(WatchdogEvent).filter(WatchdogEvent.machine_id == machine.id).one()
    assert row.action == "notify" and asic_watchdog.UNSUPPORTED in row.detail


def test_boost_en_cours_bloque_le_redemarrage(db):
    machine = _machine(db)
    db.add(Boost(uuid="boost-test-68", coin="BTC", status="running", machine_ids=str(machine.id)))
    db.commit()
    calls = []

    async def restart(target):
        calls.append(target.id)

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg, restart=restart))
    events = _run(asic_watchdog.evaluate(
        db, machine, _state(), now=start + timedelta(minutes=10), config=cfg, restart=restart))
    assert calls == []
    assert events and events[0]["result"] == "blocked"


def test_defauts_de_configuration():
    cfg = watchdog.normalize({"asic": {"max_per_day": 9, "restart": "oui"}})
    assert cfg["asic"]["max_per_day"] == 5
    assert cfg["asic"]["restart"] is True
    assert cfg["asic"]["domains_minutes"] == 10
    assert cfg["asic"]["hashrate_pct"] == 70.0
    assert cfg["asic"]["min_delay_minutes"] == 15
    low = watchdog.normalize({"asic": {"max_per_day": 0}})
    assert low["asic"]["max_per_day"] == 1
    assert watchdog.normalize({})["asic"]["restart"] is False


def test_endpoint_axeos_post_system_restart_contre_un_faux_serveur():
    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(("POST", self.path))
            body = b"System will restart shortly."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            seen.append(("GET", self.path))
            self.send_response(405)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from app import asic_client

        result = _run(asic_client.restart_axeos(f"127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
    assert seen == [("POST", "/api/system/restart")]
    assert result["status"] == 200 and result["path"] == "/api/system/restart"


def _simulated_restart_event(db, machine, moment):
    db.add(WatchdogEvent(
        ts=asic_watchdog._iso(moment), machine_id=machine.id, machine_name=machine.name,
        device_id=0, device_label="ASIC", rule=asic_watchdog.RULE_NAMES[0],
        measure="domaines inactifs", action="restart_asic", result="ok",
        simulated=True, detail="action simulée (mode simulation) : aucun appel réseau",
    ))
    db.commit()


def test_trois_redemarrages_simules_ne_bloquent_pas_un_reel(db):
    """Le plafond et le délai ne comptent que les redémarrages réels (6.8-fix)."""
    machine = _machine(db)
    calls = []

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=0, hashrate=1.4e12)

    async def noop(_seconds):
        return None

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    for minutes in (8, 24, 40):
        _simulated_restart_event(db, machine, start + timedelta(minutes=minutes))
    cfg = _config(restart=True, domains_minutes=10, min_delay_minutes=15, max_per_day=3,
                  verify_seconds=15)
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1)) == 0
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1),
                                            simulated=True) == 3
    assert asic_watchdog.last_restart(db, machine.id, start + timedelta(hours=1)) is None
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg,
                                restart=restart, probe=probe, sleep=noop))
    events = _run(asic_watchdog.evaluate(
        db, machine, _state(), now=start + timedelta(minutes=10), config=cfg,
        restart=restart, probe=probe, sleep=noop))
    assert calls == [machine.id], "un simulé ne doit pas consommer le plafond réel"
    assert events[0]["action"] == "restart_asic"
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1)) == 1
    assert asic_watchdog.is_red(db, machine.id) is False


def test_simulation_plafond_rouge_et_intervention_manuelle(db, monkeypatch):
    """Chaque mode compte ses propres tentatives ; plafond simulé → rouge + manuel."""
    machine = _machine(db)
    calls = []
    monkeypatch.setattr(watchdog, "_is_simulated", lambda db, machine: True)

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=1, hashrate=1.39e12)  # toujours 3/4 actifs

    async def noop(_seconds):
        return None

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10, min_delay_minutes=15, max_per_day=3,
                  verify_seconds=15)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg,
                                restart=restart, probe=probe, sleep=noop))
    for minutes in (10, 30, 50):
        _run(asic_watchdog.evaluate(
            db, machine, _state(), now=start + timedelta(minutes=minutes), config=cfg,
            restart=restart, probe=probe, sleep=noop))
    assert calls == []
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1),
                                            simulated=True) == 3
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1)) == 0
    assert asic_watchdog.is_red(db, machine.id) is True
    rows = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.result == "manual").all()
    assert rows, "la notification « intervention manuelle » doit être journalisée en simulation"
    assert rows[0].simulated is True
    assert asic_watchdog.MANUAL_TEXT in rows[0].detail


def test_simulation_verifie_l_etat_et_compte_l_echec(db, monkeypatch):
    """En simulation la vérification tourne sur l'état lu (une lecture) et l'échec compte."""
    machine = _machine(db)
    calls = []
    sleeps = []
    monkeypatch.setattr(watchdog, "_is_simulated", lambda db, machine: True)

    async def restart(target):
        calls.append(target.id)

    async def probe(session, target):
        return _state(inactive=1, hashrate=1.39e12)

    async def noop(seconds):
        sleeps.append(seconds)

    start = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    cfg = _config(restart=True, domains_minutes=10, verify_seconds=90)
    _run(asic_watchdog.evaluate(db, machine, _state(), now=start, config=cfg,
                                restart=restart, probe=probe, sleep=noop))
    events = _run(asic_watchdog.evaluate(
        db, machine, _state(), now=start + timedelta(minutes=10), config=cfg,
        restart=restart, probe=probe, sleep=noop))
    assert calls == []
    assert events[0]["action"] == "restart_asic"
    rows = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.rule == "asic_verify").all()
    assert rows and rows[-1].result == "failed" and rows[-1].simulated is True
    assert sleeps == [], "rien n'a redémarré : une seule lecture, sans attente"
    assert asic_watchdog.restarts_in_window(db, machine.id, start + timedelta(hours=1),
                                            simulated=True) == 1


# ── 6.8-réel : armement, driver, preuve par l'uptime ──────────────────
def test_asic_s_arme_comme_une_machine_et_reste_hors_campagnes(db):
    machine = _machine(db)
    with pytest.raises(ValueError):
        pilotage.set_machine_real(db, machine, True, "NON")
    assert pilotage.set_machine_real(db, machine, True, "PILOTER") == "Pilotage réel"
    assert machine.simulation_disabled is True
    assert pilotage.machine_armed(machine) is True
    assert pilotage.simulated_for(db, machine) is False
    assert pilotage.label(db, machine) == "Pilotage réel"
    assert machine not in pilotage.pilotable_machines(db)
    assert pilotage.set_machine_real(db, machine, False, None).startswith("Simulation")


def test_asic_jamais_dans_le_pool_de_boost(db):
    machine = _machine(db)
    device = Device(
        uuid=str(uuid.uuid4()), machine_id=machine.id, kind="gpu", index=0,
        model="carte factice", boost_pool=True, present=True,
        enabled_for_mining=True, role="default_and_spec",
    )
    db.add(device)
    db.commit()
    payload = boost_runner.pool_payload(db)
    assert device.uuid not in {row["uuid"] for row in payload["devices"]}
    assert machine not in pilotage.pilotable_machines(db)


def _axeos_server(*, reboot: bool = True, outage: float = 20.0):
    """Faux AxeOS : ``POST /api/system/restart`` coupe puis remet l'uptime à zéro."""
    clock = {"boot": time.monotonic() - 5000.0, "down_until": 0.0, "posts": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if time.monotonic() < clock["down_until"]:
                self.send_response(503)
                self.end_headers()
                return
            info = {
                "hashRate": 1400.0, "hashRate_1h": 1400.0, "expectedHashrate": 1400.0,
                "uptimeSeconds": int(time.monotonic() - clock["boot"]),
                "hashrateMonitor": {"asics": [{"domains": [430.0, 430.0, 430.0, 430.0]}]},
            }
            body = json.dumps(info).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if self.path != "/api/system/restart":
                self.send_response(404)
                self.end_headers()
                return
            clock["posts"] += 1
            clock["down_until"] = time.monotonic() + (outage if reboot else 5.0)
            if reboot:
                clock["boot"] = clock["down_until"]
            body = b"System will restart shortly."
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, clock


async def _axeos_probe(session, target):
    return asic.parse_axeos(await asic_client.fetch_axeos(target.host))


def test_redemarrage_reel_prouve_par_uptime_puis_retabli(db):
    server, clock = _axeos_server(reboot=True, outage=20.0)
    try:
        machine = _machine(db)
        machine.host = f"127.0.0.1:{server.server_port}"
        db.commit()
        cfg = _config(restart=True, restart_wait_seconds=60, uptime_max_s=120,
                      verify_seconds=15)
        outcome = _run(asic_watchdog._do_restart(
            db, machine, cfg, datetime.now(timezone.utc), rule=asic_watchdog.RULE_MANUAL,
            measure="redémarrage manuel", reason="test réel", probe=_axeos_probe,
            simulated=False))
    finally:
        server.shutdown()
        server.server_close()
    assert clock["posts"] == 1, "le POST AxeOS doit partir une seule fois"
    assert outcome["result"] == "ok" and outcome["verified"] is True
    assert outcome["proof"].startswith("uptime_s=")
    assert float(outcome["proof"].split("=")[1]) <= 120
    rows = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.rule == "asic_verify").all()
    assert rows and rows[-1].result == "restored" and rows[-1].simulated is False
    assert "uptime_s=" in rows[-1].detail


def test_uptime_inchange_echec_meme_si_domaines_ok(db):
    server, clock = _axeos_server(reboot=False)
    try:
        machine = _machine(db)
        machine.host = f"127.0.0.1:{server.server_port}"
        db.commit()
        cfg = _config(restart=True, restart_wait_seconds=30, uptime_max_s=300,
                      verify_seconds=15)
        outcome = _run(asic_watchdog._do_restart(
            db, machine, cfg, datetime.now(timezone.utc), rule=asic_watchdog.RULE_MANUAL,
            measure="redémarrage manuel", reason="test sans redémarrage",
            probe=_axeos_probe, simulated=False))
    finally:
        server.shutdown()
        server.server_close()
    assert clock["posts"] == 1
    assert outcome["verified"] is False
    assert "uptime_s=" in outcome["proof"] and "non prouvé" in outcome["proof"]
    rows = db.query(WatchdogEvent).filter(
        WatchdogEvent.machine_id == machine.id, WatchdogEvent.rule == "asic_verify").all()
    assert rows[-1].result == "failed" and "uptime_s=" in rows[-1].detail


def test_restart_asic_iceriver_leve_non_pris_en_charge():
    with pytest.raises(asic_client.AsicUnsupported):
        _run(asic_client.restart_asic("iceriver", "192.0.2.10"))
    with pytest.raises(asic_client.AsicUnsupported):
        _run(asic_client.restart_asic("cgminer", "192.0.2.10"))
