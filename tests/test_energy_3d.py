"""§4.15 / §4.17 — tarifs, heures chères, garde-fous de coupure, correctif KVM."""

from __future__ import annotations

import datetime

import pytest

from app import energy, energy_rules as ER, power_control as PC

UTC = datetime.timezone.utc


def _ranges() -> dict:
    return energy.normalize_tariff({
        "mode": "ranges",
        "ranges": [
            {"start": "00:00", "end": "06:00", "price": 0.10, "label": "HC", "days": "all"},
            {"start": "06:00", "end": "22:00", "price": 0.20, "label": "HP", "days": "weekday"},
            {"start": "06:00", "end": "22:00", "price": 0.15, "label": "HP WE", "days": "weekend"},
            {"start": "22:00", "end": "24:00", "price": 0.10, "label": "HC", "days": "all"},
        ],
    })


class TestTariffPrices:
    def test_fixed(self):
        tariff = energy.normalize_tariff({"mode": "fixed", "fixed_price": 0.25})
        assert energy.price_at(datetime.datetime(2026, 11, 16, 12, tzinfo=UTC), tariff) == 0.25

    def test_ranges_weekday_and_weekend(self):
        tariff = _ranges()
        # lundi 16/11/2026 12:00 UTC = 13:00 Paris (ouvré)
        assert energy.price_at(datetime.datetime(2026, 11, 16, 12, 0, tzinfo=UTC), tariff) == 0.20
        # samedi 21/11/2026 12:00 UTC = 13:00 Paris (week-end)
        assert energy.price_at(datetime.datetime(2026, 11, 21, 12, 0, tzinfo=UTC), tariff) == 0.15

    def test_ranges_cross_midnight(self):
        tariff = _ranges()
        # 23:30 UTC lundi = 00:30 Paris mardi → HC
        info = energy.price_info(datetime.datetime(2026, 11, 16, 23, 30, tzinfo=UTC), tariff)
        assert info["price_eur_kwh"] == 0.10
        assert info["local_time"] == "00:30"

    def test_ranges_require_both_day_types(self):
        with pytest.raises(ValueError, match="24 h sans chevauchement"):
            energy.normalize_tariff({"mode": "ranges", "ranges": [
                {"start": "00:00", "end": "24:00", "price": 0.2, "days": "weekday"},
            ]})

    def test_tempo_blue_and_red(self):
        blue = energy.normalize_tariff({"mode": "tempo", "tempo": {"day": "blue"}})
        assert energy.price_at(datetime.datetime(2026, 11, 16, 12, tzinfo=UTC), blue) == 0.1609
        assert energy.price_at(datetime.datetime(2026, 11, 16, 3, tzinfo=UTC), blue) == 0.1296
        red = energy.normalize_tariff({"mode": "tempo", "tempo": {"day": "red"}})
        assert energy.price_at(datetime.datetime(2026, 11, 16, 12, tzinfo=UTC), red) == 0.7562
        assert energy.price_at(datetime.datetime(2026, 11, 16, 3, tzinfo=UTC), red) == 0.1568

    def test_tempo_auto_colour_cached_for_the_day(self):
        today = datetime.datetime.now(energy.PARIS).date().isoformat()
        tariff = energy.normalize_tariff({
            "mode": "tempo",
            "tempo": {
                "day": "blue",
                "auto": {"enabled": True, "date": today, "day": "red",
                         "source": "api-couleur-tempo.fr"},
            },
        })
        info = energy.price_info(datetime.datetime.now(UTC), tariff)
        assert info["day_colour"] == "red"
        assert "auto" in info["day_source"]
        # Une autre date n'utilise pas le cache : la saisie manuelle reprend.
        old = energy.effective_tempo_day(tariff, datetime.datetime(2020, 1, 1, 12, tzinfo=UTC))
        assert old[0] == "blue"

    def test_tempo_payload_parsing(self):
        assert energy.parse_tempo_payload({"couleurJourJ": "ROUGE"}) == "red"
        assert energy.parse_tempo_payload({"couleurJourJ": "BLEU"}) == "blue"
        assert energy.parse_tempo_payload({"couleurJour": "blanc"}) == "white"
        assert energy.parse_tempo_payload({"inconnu": 1}) == ""

    async def test_current_price_api(self, client):
        saved = await client.put("/api/energy/tariff", json={
            "tariff": {"mode": "fixed", "fixed_price": 0.42},
        })
        assert saved.status_code == 200, saved.text
        body = (await client.get("/api/energy/tariff")).json()
        assert body["current"]["price_eur_kwh"] == pytest.approx(0.42)
        assert body["current"]["source"]


class TestRuleDecisions:
    def _rule(self, **kwargs) -> dict:
        base = {"enabled": True, "mode": "stop", "threshold_eur_kwh": 0.30,
                "trigger_negative_net": True, "simulation": True}
        base.update(kwargs)
        return ER.normalize_rule(base)

    def _machines(self) -> list[dict]:
        return [{
            "machine_id": 1, "name": "rig-a", "net_eur_day": 0.5,
            "devices": [
                {"uuid": "gpu-1", "kind": "gpu", "index": 0},
                {"uuid": "gpu-2", "kind": "gpu", "index": 1, "external": True},
                {"uuid": "gpu-3", "kind": "gpu", "index": 2, "campaign": True},
            ],
        }]

    def test_trigger_by_threshold(self):
        decision = ER.decide(self._rule(), price_eur_kwh=0.35, machines=self._machines())
        assert decision["active"] is True
        assert "au-dessus du seuil" in decision["reason"]
        actions = decision["machines"][0]["actions"]
        assert [a["uuid"] for a in actions] == ["gpu-1"]
        untouched = {u["uuid"]: u["why"] for u in decision["machines"][0]["untouched"]}
        assert "mineur externe" in untouched["gpu-2"]
        assert "campagne" in untouched["gpu-3"]

    def test_below_threshold_and_positive_net_does_nothing(self):
        decision = ER.decide(self._rule(), price_eur_kwh=0.10, machines=self._machines())
        assert decision["active"] is False
        assert decision["machines"][0]["actions"] == []

    def test_negative_net_only_touches_its_machine(self):
        machines = self._machines()
        machines[0]["net_eur_day"] = -0.4
        machines = machines + [{
            "machine_id": 2, "name": "rig-b", "net_eur_day": 1.2,
            "devices": [{"uuid": "gpu-9", "kind": "gpu", "index": 0}],
        }]
        decision = ER.decide(self._rule(), price_eur_kwh=0.10, machines=machines)
        assert decision["active"] is True
        assert decision["machines"][0]["active"] is True
        assert decision["machines"][1]["active"] is False
        assert "net/jour négatif" in decision["reason"]

    def test_reduce_keeps_priority_order(self):
        rule = self._rule(mode="reduce", keep_devices=1, priority=["gpu-3", "gpu-1", "gpu-2"])
        machines = self._machines() + []
        machines[0]["devices"][2]["campaign"] = False
        decision = ER.decide(rule, price_eur_kwh=0.35, machines=machines)
        actions = {a["uuid"]: a["action"] for a in decision["machines"][0]["actions"]}
        assert actions["gpu-3"] == "keep"
        assert actions["gpu-1"] == "stop"
        assert "gpu-2" not in actions  # externe : hors règle

    def test_campaigns_are_protected_unless_option_says_otherwise(self):
        rule = self._rule(protect_campaigns=False)
        decision = ER.decide(rule, price_eur_kwh=0.35, machines=self._machines())
        uuids = [a["uuid"] for a in decision["machines"][0]["actions"]]
        assert "gpu-3" in uuids

    def test_power_limit_is_bounded_by_the_card(self):
        assert ER.clamp_power_limit(100, 125, 300) == (125.0, True)
        assert ER.clamp_power_limit(400, 125, 300) == (300.0, True)
        assert ER.clamp_power_limit(200, 125, 300) == (200.0, False)
        with pytest.raises(ValueError, match="bornes"):
            ER.clamp_power_limit(100, None, 300)
        assert ER.power_limit_argv(1, 125.4) == ["nvidia-smi", "-i", "1", "-pl", "125"]

    def test_power_limit_decision_carries_bounds(self):
        rule = self._rule(mode="power_limit", power_limit_w=100)
        machines = self._machines()
        machines[0]["devices"][0].update({
            "power_limit_min_w": 125, "power_limit_max_w": 300, "power_limit_default_w": 250,
        })
        decision = ER.decide(rule, price_eur_kwh=0.35, machines=machines)
        action = decision["machines"][0]["actions"][0]
        assert action["action"] == "power_limit"
        assert action["watts"] == 125.0 and action["clamped"] is True
        assert action["original_w"] == 250
        assert ER.restore_actions(decision)[0]["watts"] == 250

    def test_power_limit_without_bounds_is_refused(self):
        rule = self._rule(mode="power_limit")
        decision = ER.decide(rule, price_eur_kwh=0.35, machines=self._machines())
        assert decision["machines"][0]["actions"] == []
        assert any("bornes" in u["why"] for u in decision["machines"][0]["untouched"])


class TestPowerGuards:
    def _rows(self):
        from app.database import SessionLocal
        from app.models import Machine, PowerController

        db = SessionLocal()
        machine = Machine(name="rig-power", host="192.0.2.60", usage="gpu", enabled=True)
        db.add(machine)
        db.commit()
        db.refresh(machine)
        controller = PowerController(
            uuid="55555555-5555-4555-8555-555555555555",
            machine_id=machine.id, kind="shelly_gen1", url="http://127.0.0.1:9",
            enabled=True, max_per_day=3, min_delay_s=60,
        )
        db.add(controller)
        db.commit()
        return db, machine, controller

    def test_fossor_host_is_forbidden(self):
        db, machine, controller = self._rows()
        try:
            machine.fossor_host = True
            db.commit()
            with pytest.raises(PC.PowerError, match="héberge Fossor"):
                PC.check_guards(db, machine, controller, "on")
        finally:
            db.close()

    def test_quota_and_delay(self):
        db, machine, controller = self._rows()
        try:
            now = datetime.datetime.now(UTC)
            for offset in (3600, 7200, 10800):
                PC.journal(db, machine.id, "off", result="ok", detail="",
                           simulated=False, now=now - datetime.timedelta(seconds=offset))
            with pytest.raises(PC.PowerError, match="quota atteint"):
                PC.check_guards(db, machine, controller, "off", confirm=True, now=now)
            # Une action ancienne : quota reparti à zéro dans la fenêtre ? Non —
            # on efface le journal pour ne tester que le délai.
            from app.models import PowerAction

            db.query(PowerAction).delete()
            db.commit()
            PC.journal(db, machine.id, "on", result="ok", detail="", simulated=False,
                       now=now - datetime.timedelta(seconds=10))
            with pytest.raises(PC.PowerError, match="délai minimal"):
                PC.check_guards(db, machine, controller, "off", confirm=True, now=now)
        finally:
            db.close()

    def test_double_confirmation_on_online_machine(self):
        db, machine, controller = self._rows()
        try:
            with pytest.raises(PC.PowerError, match="double confirmation"):
                PC.check_guards(db, machine, controller, "off", confirm=False, online=True)
            PC.check_guards(db, machine, controller, "off", confirm=True, online=True)
        finally:
            db.close()

    def test_kvm_ip_is_not_supported(self):
        config = PC.normalize_controller({"kind": "kvm_ip", "url": "http://127.0.0.1:9"})
        assert config["supported"] is False
        assert any(k == "kvm_ip" for k in PC.KINDS)

    async def test_simulated_action_sends_nothing(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.models import Machine, PowerController

        db = SessionLocal()
        machine = Machine(name="rig-simu", host="192.0.2.61", usage="gpu", enabled=True)
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(PowerController(
            uuid="66666666-6666-4666-8666-666666666666",
            machine_id=machine.id, kind="shelly_gen1", url="http://127.0.0.1:9",
            enabled=True, max_per_day=3, min_delay_s=60,
        ))
        db.commit()
        machine_id = machine.id
        db.close()

        async def boom(*args, **kwargs):
            raise AssertionError("appel sortant interdit en simulation")

        monkeypatch.setattr(PC, "perform", boom)
        response = await client.post(
            f"/api/machines/{machine_id}/power/off",
            json={"simulated": True, "confirm": True},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["simulated"] is True
        assert body["commands"][0]["path"] == "/relay/0?turn=off"


class TestVmKvmDetection:
    def test_ryzen_without_nested_virt_is_not_a_vm_host(self):
        from app.hw_parser import parse_virt

        # Sortie relevée sur le Ryzen 5 3600 de Fred : pas de /dev/kvm, qemu seul.
        virt = parse_virt("VIRSH=1\nKVM_DOMAIN=0\n")
        assert virt["kvm"] is False
        assert virt["vm_capable"] is False
        virt = parse_virt("KVM=1\nVIRSH=1\nKVM_DOMAIN=0\n")
        assert virt["vm_capable"] is False

    def test_kvm_host_from_capabilities_and_pve(self):
        from app.hw_parser import parse_virt

        assert parse_virt("KVM=1\nVIRSH=1\nKVM_DOMAIN=1\n")["vm_capable"] is True
        assert parse_virt("KVM=1\nPVERSION=1\n")["vm_capable"] is True

    async def test_vm_creation_refuses_before_any_write(self, monkeypatch):
        from app import spec_vm as V, vm_remote as VR

        calls: list[str] = []

        async def runner(host, command, stdin=None, timeout=0):
            calls.append(command)
            return {"ok": True, "status": 0,
                    "stdout": "KVM=1\nVIRSH=1\nKVM_DOMAIN=0\n", "stderr": ""}

        monkeypatch.setattr(VR, "RUNNER", runner)
        model = V.validate_vm_model({
            "host_id": 1,
            "base_image_url": "http://127.0.0.1:9/debian.qcow2",
            "base_image_sha256": "a" * 64,
        })
        # Clé ed25519 fictive mais syntaxiquement valide (aucune identité réelle).
        import base64
        import struct

        blob = (struct.pack(">I", 11) + b"ssh-ed25519"
                + struct.pack(">I", 32) + b"\x01" * 32)
        key = "ssh-ed25519 " + base64.b64encode(blob).decode() + " fossor@test"
        with pytest.raises(VR.VmRemoteError, match="libvirt sans KVM"):
            await VR.create(
                object(), model, "fossor-test",
                ssh_public_key=key, private_key="cle",
                simulated=False,
            )
        assert len(calls) == 1  # la sonde seule : aucune écriture
