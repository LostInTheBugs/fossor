"""§4.17 / §4.15 — faux Shelly (Gen1, Gen2), faux Home Assistant, règle heures chères.

Les serveurs HTTP tournent en local (127.0.0.1), dans le processus de test :
aucune prise, aucun Home Assistant réel n'est contacté. Les requêtes reçues sont
conservées telles quelles et servent de preuve.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import energy, energy_rules as ER, power_control as PC, rule_runner as RR

UTC = datetime.timezone.utc


class FakeHome:
    """Serveur qui enregistre chaque requête et répond comme un vrai appareil."""

    def __init__(self, kind: str):
        self.kind = kind
        self.requests: list[dict] = []
        self.state = {"on": False, "power_w": 123.4}
        self.headers: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                return

            def _record(self, method: str, body: bytes | None):
                owner.requests.append({
                    "method": method, "path": self.path,
                    "body": body.decode() if body else "",
                })
                owner.headers.append({k: v for k, v in self.headers.items()})
                if owner.kind == "shelly_gen1":
                    if self.path.startswith("/relay/0?turn=on"):
                        owner.state["on"] = True
                    elif self.path.startswith("/relay/0?turn=off"):
                        owner.state["on"] = False
                if owner.kind == "shelly_gen2" and self.path.startswith("/rpc/Switch.Set"):
                    payload = json.loads(body or b"{}")
                    owner.state["on"] = bool(payload.get("on"))
                if owner.kind == "home_assistant" and "/api/services/switch/turn_" in self.path:
                    owner.state["on"] = self.path.endswith("turn_on")
                payload = owner.payload(owner, self.path)
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._record("GET", None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self._record("POST", self.rfile.read(length) if length else b"")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def payload(owner, path: str) -> dict:
        on = owner.state["on"]
        power = owner.state["power_w"]
        if owner.kind == "shelly_gen1":
            if path.startswith("/status"):
                return {"relays": [{"ison": on}], "meters": [{"power": power}]}
            return {"ison": on}
        if owner.kind == "shelly_gen2":
            if path.startswith("/rpc/Switch.GetStatus"):
                return {"id": 0, "output": on, "apower": power}
            return {"was_on": not on}
        if "/api/services/switch/" in path:
            return {"changed": True}
        if path.startswith("/api/states/"):
            return {"entity_id": "switch.rig", "state": "on" if on else "off",
                    "attributes": {"power": power}}
        return {}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def shelly_gen1():
    fake = FakeHome("shelly_gen1")
    yield fake
    fake.close()


@pytest.fixture
def shelly_gen2():
    fake = FakeHome("shelly_gen2")
    yield fake
    fake.close()


@pytest.fixture
def home_assistant():
    fake = FakeHome("home_assistant")
    yield fake
    fake.close()


def _controller(url: str, kind: str, **kwargs):
    from app.models import PowerController

    return PowerController(
        uuid="77777777-7777-4777-8777-777777777777",
        machine_id=1, kind=kind, url=url, enabled=True,
        max_per_day=kwargs.get("max_per_day", 3),
        min_delay_s=kwargs.get("min_delay_s", 5),
        entity=kwargs.get("entity", ""), power_entity=kwargs.get("power_entity", ""),
        token_ref=kwargs.get("token_ref"),
    )


def ps_line(pid: int) -> str:
    """Ligne ``ps`` du processus, ou mention explicite si ps est absent."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,stat=,args=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        return out.stdout.strip()
    except FileNotFoundError:
        return f"ps indisponible — /proc/{pid} " + (
            "présent" if os.path.isdir(f"/proc/{pid}") else "absent"
        )


def is_alive(pid: int) -> bool:
    line = ps_line(pid)
    if line.startswith("ps indisponible"):
        return os.path.isdir(f"/proc/{pid}")
    return line.startswith(str(pid))


class TestFakeControllers:
    async def test_shelly_gen1_on_off_and_power(self, shelly_gen1):
        controller = _controller(shelly_gen1.url, "shelly_gen1")
        state = await PC.read_state(controller)
        assert state["state"] == "off"
        assert state["power_w"] == pytest.approx(123.4)
        assert state["source"] == PC.MEASURED_SOURCE

        await PC.perform(controller, "on", sleep=lambda s: asyncio.sleep(0))
        assert shelly_gen1.requests[-1]["method"] == "GET"
        assert shelly_gen1.requests[-1]["path"] == "/relay/0?turn=on"
        assert (await PC.read_state(controller))["state"] == "on"

        await PC.perform(controller, "off")
        assert shelly_gen1.requests[-1]["path"] == "/relay/0?turn=off"

    async def test_shelly_gen2_rpc(self, shelly_gen2):
        controller = _controller(shelly_gen2.url, "shelly_gen2")
        await PC.perform(controller, "on")
        sent = shelly_gen2.requests[-1]
        assert sent["method"] == "POST" and sent["path"] == "/rpc/Switch.Set"
        assert json.loads(sent["body"]) == {"id": 0, "on": True}
        state = await PC.read_state(controller)
        assert state["state"] == "on" and state["power_w"] == pytest.approx(123.4)

    async def test_power_cycle_off_wait_on(self, shelly_gen2):
        controller = _controller(shelly_gen2.url, "shelly_gen2")
        waits: list[int] = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        result = await PC.perform(controller, "cycle", cycle_wait_s=3, sleep=fake_sleep)
        assert waits == [3]
        paths = [r["path"] for r in shelly_gen2.requests]
        assert paths == ["/rpc/Switch.Set", "/rpc/Switch.Set"]
        assert [r["method"] for r in result["sent"]] == ["POST", "WAIT", "POST"]

    async def test_home_assistant_switch_and_token(self, home_assistant):
        from app.secrets import encrypt_secret

        controller = _controller(
            home_assistant.url, "home_assistant",
            entity="switch.rig", token_ref=encrypt_secret("jeton-de-test"),
        )
        await PC.perform(controller, "on")
        sent = home_assistant.requests[-1]
        assert sent["path"] == "/api/services/switch/turn_on"
        assert json.loads(sent["body"]) == {"entity_id": "switch.rig"}
        assert home_assistant.headers[-1]["Authorization"] == "Bearer jeton-de-test"
        state = await PC.read_state(controller)
        assert state["state"] == "on" and state["power_w"] == pytest.approx(123.4)

    async def test_kvm_ip_refuses_without_calling(self):
        controller = _controller("http://127.0.0.1:9", "kvm_ip")
        with pytest.raises(PC.PowerError, match="non pris en charge"):
            await PC.perform(controller, "on")


class TestPowerApiWithFakeShelly:
    async def _machine(self, db, name="rig-prise"):
        from app.models import Machine

        machine = Machine(name=name, host="192.0.2.70", usage="gpu", enabled=True,
                          platform_power_w=0)
        db.add(machine)
        db.commit()
        db.refresh(machine)
        return machine

    async def test_read_stores_measured_sample_used_by_cost(self, client, shelly_gen1):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            machine = await self._machine(db)
            machine_id = machine.id
            machine_uuid = machine.uuid
        finally:
            db.close()

        saved = await client.put(f"/api/machines/{machine_id}/power-controller", json={
            "kind": "shelly_gen1", "url": shelly_gen1.url,
            "max_per_day": 3, "min_delay_s": 5,
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["controller"]["has_token"] is False

        read = await client.post(f"/api/machines/{machine_id}/power/read")
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["power_w"] == pytest.approx(123.4)
        assert body["source"] == PC.MEASURED_SOURCE

        report = await client.get("/api/energy?period=24h")
        assert report.status_code == 200
        row = next(m for m in report.json()["machines"] if m["uuid"] == machine_uuid)
        assert row["power_source"] == "mesurée à la prise"
        assert row["measured_power_w"] == pytest.approx(123.4)
        assert report.json()["total"]["measured_machines"] == 1

    async def test_off_requires_confirmation_and_quota(self, client, shelly_gen1):
        from app.database import SessionLocal
        from app.models import MachineStatus

        db = SessionLocal()
        try:
            machine = await self._machine(db, name="rig-confirm")
            machine.simulation_disabled = True  # machine armée : action réelle
            machine_id = machine.id
            db.add(MachineStatus(machine_uuid=machine.uuid, ts=datetime.datetime.now(UTC).isoformat(),
                                 online=True, algo="", miner="", coin=""))
            db.commit()
        finally:
            db.close()

        await client.put(f"/api/machines/{machine_id}/power-controller", json={
            "kind": "shelly_gen1", "url": shelly_gen1.url, "max_per_day": 1, "min_delay_s": 5,
        })
        refused = await client.post(f"/api/machines/{machine_id}/power/off", json={})
        assert refused.status_code == 409
        assert "double confirmation" in refused.json()["detail"]
        assert shelly_gen1.requests == []  # aucun appel sortant sur un refus

        first = await client.post(f"/api/machines/{machine_id}/power/off", json={"confirm": True})
        assert first.status_code == 200, first.text
        assert shelly_gen1.requests[-1]["path"] == "/relay/0?turn=off"

        # Quota 1/24 h : la deuxième coupure est refusée avant tout appel.
        second = await client.post(f"/api/machines/{machine_id}/power/off", json={"confirm": True})
        assert second.status_code == 409
        assert "quota" in second.json()["detail"]

    async def test_fossor_host_refusal_on_api(self, client, shelly_gen1):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            machine = await self._machine(db, name="rig-fossor")
            machine.fossor_host = True
            db.commit()
            machine_id = machine.id
        finally:
            db.close()
        await client.put(f"/api/machines/{machine_id}/power-controller", json={
            "kind": "shelly_gen1", "url": shelly_gen1.url,
        })
        response = await client.post(f"/api/machines/{machine_id}/power/on", json={})
        assert response.status_code == 409
        assert "héberge Fossor" in response.json()["detail"]
        assert shelly_gen1.requests == []


class TestExpensiveHoursIntegration:
    """Règle réelle avec un faux mineur local, horloge et prix simulés.

    Le processus est un vrai sous-processus : ``ps`` prouve l'arrêt et la
    reprise. Aucun SSH, aucune machine de Fred.
    """

    async def test_enter_stop_exit_resume(self, tmp_path, monkeypatch):
        from app.database import SessionLocal
        from app.models import (Campaign, CampaignDevice, Device, Machine,
                                MinerProfile)

        script = tmp_path / "fake_miner.py"
        script.write_text(
            "import time, sys\n"
            "while True:\n"
            "    time.sleep(0.2)\n"
        )
        pidfile = tmp_path / "miner.pid"
        state = {"process": None, "starts": 0, "stops": 0}

        def start_miner():
            if state["process"] and state["process"].poll() is None:
                return
            state["process"] = subprocess.Popen([sys.executable, str(script)])
            state["starts"] += 1
            pidfile.write_text(str(state["process"].pid))

        def stop_miner():
            proc = state["process"]
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
                state["stops"] += 1

        async def runner(machine, profile, action, gpus=None):
            if action in ("stop", "restart"):
                stop_miner()
            if action in ("start", "restart"):
                start_miner()
            return {"ok": True, "action": action}

        monkeypatch.setattr(RR, "RUNNER", runner)

        db = SessionLocal()
        try:
            machine = Machine(name="rig-heures-cheres", host="192.0.2.80",
                              usage="gpu", enabled=True, platform_power_w=0)
            db.add(machine)
            db.commit()
            db.refresh(machine)
            profile = MinerProfile(
                uuid="88888888-8888-4888-8888-888888888888", name="profil-test",
                coin="PRL", wallet="wallet-de-test", pool="stratum+tcp://127.0.0.1:9",
                software="fake", binary_url="http://127.0.0.1:9/fake", sha256="0" * 64,
                binary_path="/opt/fossor/miners/fake", version="1", cmdline="fake --worker {worker}",
                api_port=0, threads=1, enabled=True,
            )
            db.add(profile)
            db.commit()
            db.refresh(profile)
            machine.default_miner_profile_id = profile.id
            device = Device(uuid="99999999-9999-4999-8999-999999999999",
                            machine_id=machine.id, kind="gpu", index=0, model="GPU test",
                            present=True, enabled_for_mining=True)
            db.add(device)
            db.commit()

            energy.set_tariff(db, {"mode": "fixed", "fixed_price": 0.40})
            ER.set_rule(db, {
                "enabled": True, "mode": "stop", "threshold_eur_kwh": 0.30,
                "trigger_negative_net": False, "simulation": False,
            })
            machine.simulation_disabled = True  # machine armée : action réelle
            db.commit()
            start_miner()
            pid = state["process"].pid
            print(f"### PS AVANT (pid {pid})\n{ps_line(pid)}")
            assert is_alive(pid)

            # Entrée en plage chère : le mineur est arrêté.
            high = datetime.datetime.now(UTC)
            result = await RR.evaluate(db, now=high, probe=lambda: 12.0)
            print("### DECISION", json.dumps(result["decision"], ensure_ascii=False, default=str))
            print("### EXECUTE", json.dumps(result["executed"], ensure_ascii=False, default=str))
            assert result["decision"]["active"] is True
            assert state["stops"] == 1
            assert state["process"].poll() is not None
            print(f"### PS APRES ARRET (pid {pid})\n{ps_line(pid)}")
            assert not is_alive(pid)

            # Fin de la plage : prix sous le seuil, reprise vérifiée.
            energy.set_tariff(db, {"mode": "fixed", "fixed_price": 0.10})
            low = high + datetime.timedelta(hours=2)
            result = await RR.evaluate(db, now=low, probe=lambda: 42.0)
            assert result["decision"]["active"] is False
            assert state["starts"] == 2
            new_pid = state["process"].pid
            print(f"### PS APRES REPRISE (pid {new_pid})\n{ps_line(new_pid)}")
            assert is_alive(new_pid)
            assert result["resumed"][0]["verified"] is True
            assert result["resumed"][0]["hashrate_hs"] == 42.0

            from app.models import EnergyRuleEvent

            kinds = [row.kind for row in db.query(EnergyRuleEvent).order_by(EnergyRuleEvent.id).all()]
            assert "enter" in kinds and "action" in kinds and "resume" in kinds
            events = db.query(EnergyRuleEvent).order_by(EnergyRuleEvent.id).all()
            assert any("hashrate vérifié" in (e.detail or "") for e in events)
        finally:
            stop_miner()
            db.close()

    async def test_campaign_device_is_protected_end_to_end(self):
        from app.database import SessionLocal
        from app.models import (Campaign, CampaignDevice, Device, Machine)

        db = SessionLocal()
        try:
            machine = Machine(name="rig-campagne", host="192.0.2.81", usage="gpu", enabled=True)
            db.add(machine)
            db.commit()
            db.refresh(machine)
            campaign = Campaign(uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", coin="PRL",
                                profile_id=1, start_at=datetime.datetime.now(UTC).isoformat(),
                                status="running")
            db.add(campaign)
            db.commit()
            db.refresh(campaign)
            device = Device(uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                            machine_id=machine.id, kind="gpu", index=0, model="GPU test",
                            present=True, enabled_for_mining=True)
            db.add(device)
            db.commit()
            db.add(CampaignDevice(campaign_id=campaign.id, machine_id=machine.id,
                                  device_uuid=device.uuid, kind="gpu", label="GPU test"))
            db.commit()

            energy.set_tariff(db, {"mode": "fixed", "fixed_price": 0.50})
            ER.set_rule(db, {"enabled": True, "mode": "stop", "threshold_eur_kwh": 0.30,
                             "simulation": True, "protect_campaigns": True})
            result = await RR.evaluate(db, now=datetime.datetime.now(UTC))
            entry = next(m for m in result["decision"]["machines"]
                         if m["machine_id"] == machine.id)
            assert entry["active"] is True
            assert entry["actions"] == []
            assert any("campagne" in u["why"] for u in entry["untouched"])
        finally:
            db.close()

    async def test_simulation_writes_nothing_out(self, monkeypatch):
        from app.database import SessionLocal
        from app.models import Device, Machine

        async def boom(*args, **kwargs):
            raise AssertionError("appel sortant interdit en simulation")

        monkeypatch.setattr(RR, "RUNNER", boom)
        monkeypatch.setattr(RR, "POWER_LIMIT_RUNNER", boom)

        db = SessionLocal()
        try:
            machine = Machine(name="rig-simu-rule", host="192.0.2.82", usage="gpu", enabled=True)
            db.add(machine)
            db.commit()
            db.refresh(machine)
            db.add(Device(uuid="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                          machine_id=machine.id, kind="gpu", index=0, model="GPU test",
                          present=True, enabled_for_mining=True))
            db.commit()
            energy.set_tariff(db, {"mode": "fixed", "fixed_price": 0.50})
            ER.set_rule(db, {"enabled": True, "mode": "stop", "threshold_eur_kwh": 0.30,
                             "simulation": True})
            result = await RR.evaluate(db, now=datetime.datetime.now(UTC))
            assert result["decision"]["active"] is True
            assert result["executed"] == []
            from app.models import EnergyRuleEvent

            events = db.query(EnergyRuleEvent).all()
            assert events and all(e.simulated for e in events)
        finally:
            db.close()
