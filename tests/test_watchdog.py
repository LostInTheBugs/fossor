"""Watchdog 6.1 (§10.1) — seuils, gradation et garde-fous.

Aucune machine n'est contactée : l'exécution (SSH, VM, contrôleur) est
remplacée par un espion.  L'intégration sur cible jetable vit dans
``tests/test_watchdog_integration.py``.

Chaque garde-fou du brief a son test, groupé par classe ; le nom des tests
sert de matrice dans ``RAPPORT-watchdog-tests.md``.
"""

from __future__ import annotations

import json
import re
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import watchdog as W
from app.database import SessionLocal
from app.models import (
    Campaign,
    CampaignDevice,
    Device,
    Machine,
    MachineStatus,
    Metric,
    MinerProfile,
    PowerController,
    SpecVm,
    WatchdogEvent,
    WatchdogState,
)

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


# ── Fabrique ──────────────────────────────────────────────────────────
@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _machine(db, name="rig-a", **kw):
    values = dict(
        uuid=str(uuid.uuid4()), name=name, host="192.0.2.10", ssh_port=22,
        ssh_user="root", auth_type="password", machine_kind="host",
        usage="both", enabled=True,
    )
    values.update(kw)
    machine = Machine(**values)
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def _device(db, machine, *, kind="gpu", model="RTX 3070", bus: str | None = "01:00.0", token=None, **kw):
    device = Device(
        uuid=str(uuid.uuid4()), machine_id=machine.id, kind=kind, model=model,
        pci_bus=bus, present=True, enabled_for_mining=True,
        default_token_id=token, **kw,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def _profile(db, name="gpu-pearl", **kw):
    values = dict(
        uuid=str(uuid.uuid4()), name=name, coin="PRL",
        wallet="prl1testwalletfossor0000000000000000000000000000000000",
        pool="stratum+tcp://pool.exemple.tld:3360", software="srbminer",
        binary_url="http://192.0.2.1/srbminer.tar.gz", sha256="0" * 64,
        binary_path="/opt/fossor/miners/srbminer", version="2.0.0",
        cmdline="/opt/fossor/miners/srbminer --wallet {wallet} --pool {pool}",
        enabled=True,
    )
    values.update(kw)
    profile = MinerProfile(**values)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _status(db, machine, *, accepted=100, online=True, ts=T0, error=None):
    row = MachineStatus(
        machine_uuid=machine.uuid, ts=W._iso(ts), online=online, error=error,
        miner="srbminer", algo="pearlhash", coin="PRL", accepted=accepted,
    )
    db.add(row)
    db.commit()
    return row


def _metrics(db, machine, device, values, *, end=T0, step_min=20):
    for index, value in enumerate(values):
        db.add(Metric(
            machine_uuid=machine.uuid, device_uuid=device.uuid,
            ts=W._iso(end - timedelta(minutes=step_min * (len(values) - index))),
            hashrate_hs=value,
        ))
    db.commit()


def _save(db, *, rule=None, **over):
    """Config où **seule** la règle nommée est active (les autres coupées)."""
    config = W.normalize({})
    for name in W.RULE_NAMES:
        config["rules"][name]["enabled"] = (name == rule)
    for key, value in over.items():
        if key in ("escalation_minutes", "grace_minutes", "max_per_hour", "enabled"):
            config[key] = value
        else:
            config["rules"][rule][key] = value
    return W.save_config(db, config)


def _stats(hashrate=None, temp=None, *, bus="01:00.0", kind="gpu"):
    return {"devices": [{"kind": kind, "pci_bus": bus,
                         "hashrate_hs": hashrate, "temp_c": temp}]}


def _spy(monkeypatch, module, name="ssh_run_command"):
    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(module, name, fake)
    return calls


# ── Configuration : valeurs par défaut sûres ──────────────────────────
class TestConfiguration:
    def test_defauts_surs(self):
        config = W.normalize(None)
        assert config["enabled"] is True
        assert config["max_per_hour"] == 3
        assert config["grace_minutes"] == 10
        assert config["escalation_minutes"] == 5
        assert config["actions"] == {
            "notify": True, "restart_miner": True, "restart_vm": True,
            "reboot": False, "power_cycle": False,
        }
        assert config["rules"]["hashrate_low"]["pct"] == 50.0
        assert config["rules"]["hashrate_low"]["minutes"] == 10
        assert config["rules"]["temperature"]["gpu_c"] == 85.0
        assert config["rules"]["temperature"]["cpu_c"] == 90.0

    def test_cles_inconnues_ignorees_et_bornes(self):
        config = W.normalize({
            "pirate": "x",
            "rules": {"hashrate_low": {"pct": 0, "bidon": 3, "minutes": -4}},
            "max_per_hour": 0,
        })
        assert "pirate" not in config
        assert "bidon" not in config["rules"]["hashrate_low"]
        assert config["rules"]["hashrate_low"]["pct"] == 1.0
        assert config["rules"]["hashrate_low"]["minutes"] == 0
        assert config["max_per_hour"] == 1

    def test_surcharge_machine_ne_touche_pas_les_autres(self):
        with SessionLocal() as db:
            rig_a = _machine(db, "rig-a")
            rig_b = _machine(db, "rig-b")
            W.save_machine_override(db, rig_a.id, {"rules": {"hashrate_low": {"pct": 70}}})
            config = W.get_config(db)
            assert W.machine_config(config, rig_a)["rules"]["hashrate_low"]["pct"] == 70.0
            assert W.machine_config(config, rig_b)["rules"]["hashrate_low"]["pct"] == 50.0


# ── Seuils ────────────────────────────────────────────────────────────
class TestSeuilHashrate:
    async def _prepare(self, db):
        machine = _machine(db)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=10, pct=50.0, escalation_minutes=0)
        return machine, device

    @pytest.mark.asyncio
    async def test_sous_50_pct_pendant_10_min_declenche(self, db):
        machine, _ = await self._prepare(db)
        assert await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0)) == []
        assert await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=5), stats=_stats(400.0)) == []
        events = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=11), stats=_stats(400.0))
        assert len(events) == 1, events
        assert events[0]["rule"] == "hashrate_low"
        assert events[0]["action"] == "restart_miner"
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert row.rule == "hashrate_low" and row.result == "ok"
        assert row.simulated is True
        assert "50 % de 1000" in row.measure

    @pytest.mark.asyncio
    async def test_a_50_pct_pile_ne_declenche_pas(self, db):
        machine, _ = await self._prepare(db)
        for offset in (0, 5, 11, 30):
            events = await W.evaluate_machine(
                db, machine, now=T0 + timedelta(minutes=offset), stats=_stats(500.0))
            assert events == [], events

    @pytest.mark.asyncio
    async def test_retablissement_remet_le_compteur(self, db):
        machine, _ = await self._prepare(db)
        await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        await W.evaluate_machine(db, machine, now=T0 + timedelta(minutes=2), stats=_stats(1000.0))
        events = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=3), stats=_stats(400.0))
        assert events == []
        assert db.query(WatchdogState).filter(WatchdogState.rule == "hashrate_low").count() == 1

    @pytest.mark.asyncio
    async def test_historique_court_ne_declenche_rien(self, db):
        machine = _machine(db)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 3)  # < 6 échantillons
        assert W.reference_hashrate(db, device.uuid, T0) is None
        _save(db, rule="hashrate_low", minutes=0, escalation_minutes=0)
        events = await W.evaluate_machine(db, machine, now=T0, stats=_stats(10.0))
        assert events == []


class TestSeuilShares:
    @pytest.mark.asyncio
    async def test_aucune_share_pendant_20_min(self, db):
        machine = _machine(db)
        _status(db, machine, accepted=10)
        _save(db, rule="no_shares", minutes=20, escalation_minutes=0)
        assert await W.evaluate_machine(db, machine, now=T0) == []
        assert await W.evaluate_machine(db, machine, now=T0 + timedelta(minutes=19)) == []
        events = await W.evaluate_machine(db, machine, now=T0 + timedelta(minutes=21))
        assert len(events) == 1 and events[0]["rule"] == "no_shares", events

    @pytest.mark.asyncio
    async def test_une_share_recue_remet_le_compteur(self, db):
        machine = _machine(db)
        row = _status(db, machine, accepted=10)
        _save(db, rule="no_shares", minutes=20, escalation_minutes=0)
        await W.evaluate_machine(db, machine, now=T0)
        row.accepted = 11
        row.ts = W._iso(T0 + timedelta(minutes=5))
        db.commit()
        assert await W.evaluate_machine(db, machine, now=T0 + timedelta(minutes=21)) == []

    @pytest.mark.asyncio
    async def test_regle_desactivee_ne_declenche_rien(self, db):
        machine = _machine(db)
        _status(db, machine, accepted=10)
        _save(db, rule=None)
        events = await W.evaluate_machine(db, machine, now=T0 + timedelta(minutes=120))
        assert events == []
        assert db.query(WatchdogEvent).count() == 0


class TestSeuilTemperature:
    @pytest.mark.asyncio
    async def test_gpu_au_dessus_pendant_5_min(self, db):
        machine = _machine(db)
        profile = _profile(db)
        _device(db, machine, token=profile.id)
        _save(db, rule="temperature", minutes=5, escalation_minutes=0)
        assert await W.evaluate_machine(db, machine, now=T0, stats=_stats(temp=90.0)) == []
        events = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=6), stats=_stats(temp=90.0))
        assert len(events) == 1 and events[0]["rule"] == "temperature", events
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert "90 °C > 85 °C" in row.measure

    @pytest.mark.asyncio
    async def test_cpu_a_son_propre_seuil(self, db):
        machine = _machine(db)
        profile = _profile(db)
        _device(db, machine, kind="cpu", bus=None, token=profile.id)
        _save(db, rule="temperature", minutes=0, escalation_minutes=0)
        # 86 °C passe le seuil GPU (85) mais pas le seuil CPU (90).
        events = await W.evaluate_machine(
            db, machine, now=T0, stats=_stats(temp=86.0, bus="", kind="cpu"))
        assert events == [], events


class TestMineurAbsent:
    @pytest.mark.asyncio
    async def test_unite_introuvable_declenche(self, db):
        machine = _machine(db)
        profile = _profile(db)
        _device(db, machine, token=profile.id)
        machine.default_miner_profile_id = profile.id
        db.commit()
        _save(db, rule="miner_absent", escalation_minutes=0)
        events = await W.evaluate_machine(
            db, machine, now=T0, stats={}, raw_stdout="root 1 0.0 /usr/bin/autre-chose")
        assert len(events) == 1 and events[0]["rule"] == "miner_absent", events
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert "fossor-miner-gpu-pearl.service" in row.measure
        assert row.action == "restart_miner"
        assert row.detail.endswith("(simulé)") or "simulé" in row.detail

    @pytest.mark.asyncio
    async def test_unite_vue_ne_declenche_pas(self, db):
        machine = _machine(db)
        profile = _profile(db)
        _device(db, machine, token=profile.id)
        machine.default_miner_profile_id = profile.id
        db.commit()
        _save(db, rule="miner_absent", escalation_minutes=0)
        from app import miners as M
        binary = M.binary_path({"software": profile.software,
                                "version": profile.version,
                                "binary_path": profile.binary_path})
        stdout = f"root 42 99.0 {binary} --pool x"
        events = await W.evaluate_machine(db, machine, now=T0, stats={}, raw_stdout=stdout)
        assert events == []


class TestMachineInjoignable:
    @pytest.mark.asyncio
    async def test_injoignable_pendant_10_min(self, db):
        machine = _machine(db)
        _save(db, rule="unreachable", minutes=10, escalation_minutes=0)
        assert await W.evaluate_machine(db, machine, now=T0, offline="connexion refusée") == []
        assert await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=5), offline="connexion refusée") == []
        events = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=11), offline="connexion refusée")
        assert len(events) == 1 and events[0]["rule"] == "unreachable", events

    @pytest.mark.asyncio
    async def test_regle_desactivee_ne_declenche_rien(self, db):
        machine = _machine(db)
        _save(db, rule=None)
        events = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=90), offline="timeout")
        assert events == []
        assert db.query(WatchdogEvent).count() == 0

    @pytest.mark.asyncio
    async def test_retour_en_ligne_remet_le_delai(self, db):
        machine = _machine(db)
        _save(db, rule="unreachable", minutes=10, escalation_minutes=0)
        await W.evaluate_machine(db, machine, now=T0, offline="timeout")
        await W.evaluate_machine(db, machine, now=T0 + timedelta(minutes=11), offline="timeout")
        # La machine répond de nouveau : le compteur doit repartir de zéro.
        await W.evaluate_machine(db, machine, now=T0 + timedelta(minutes=12), stats={})
        events = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=13), offline="timeout")
        assert events == [], events


# ── Gradation et délais ───────────────────────────────────────────────
class TestGradation:
    def _config(self, **actions):
        config = W.normalize({})
        config["actions"].update(actions)
        return config

    def test_paliers_et_refus(self, db):
        machine = _machine(db)
        profile = _profile(db)
        _device(db, machine, token=profile.id)
        state = types.SimpleNamespace(stage=0, last_action_at=None)
        action, detail = W.choose_action(db, machine, None, state, self._config(), T0)
        assert action == "restart_miner" and detail == "fossor-miner-gpu-pearl.service"

        state.stage = 1
        action, detail = W.choose_action(db, machine, None, state, self._config(), T0)
        assert action == "notify" and "sans campagne" in detail

        state.stage = 2
        action, detail = W.choose_action(
            db, machine, None, state, self._config(reboot=True), T0)
        assert action == "notify" and "non validé" in detail

        machine.watchdog_reboot_ok = True
        db.commit()
        action, _ = W.choose_action(db, machine, None, state, self._config(reboot=True), T0)
        assert action == "reboot"

        state.stage = 3
        action, detail = W.choose_action(
            db, machine, None, state, self._config(power_cycle=True), T0)
        assert action == "notify" and "power cycle" in detail

    def test_palier_vm_exige_une_campagne_vm(self, db):
        machine = _machine(db)
        profile = _profile(db)
        campaign = Campaign(
            uuid=str(uuid.uuid4()), coin="PRL", profile_id=profile.id,
            start_at=W._iso(T0), status="running", execution_mode="vm",
        )
        db.add(campaign)
        db.commit()
        db.add(CampaignDevice(campaign_id=campaign.id, machine_id=machine.id))
        db.commit()
        state = types.SimpleNamespace(stage=1, last_action_at=None)
        action, detail = W.choose_action(db, machine, None, state, self._config(), T0)
        assert action == "restart_vm" and "campagne" in detail

    @pytest.mark.asyncio
    async def test_delai_entre_paliers(self, db):
        machine = _machine(db)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=10, pct=50.0,
              escalation_minutes=5, grace_minutes=0)

        assert await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0)) == []
        first = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=11), stats=_stats(400.0))
        assert [event["action"] for event in first] == ["restart_miner"]
        # 1 min plus tard : le délai d'escalade n'est pas écoulé, aucune action.
        assert await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=12), stats=_stats(400.0)) == []
        second = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=17), stats=_stats(400.0))
        assert [event["action"] for event in second] == ["notify"], second
        assert db.query(WatchdogEvent).filter(
            WatchdogEvent.action == "restart_miner").count() == 1


class TestGrace:
    @pytest.mark.asyncio
    async def test_grace_apres_relance(self, db):
        machine = _machine(db)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0,
              escalation_minutes=0, grace_minutes=10)
        first = await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        assert [event["action"] for event in first] == ["restart_miner"]
        # Relance à l'instant : la règle est toujours en défaut mais la grâce
        # interdit une seconde action.
        events = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=1), stats=_stats(400.0))
        assert events == []
        grace = db.query(WatchdogEvent).filter(WatchdogEvent.result == "grace").all()
        assert len(grace) == 1 and "grâce de 10 min" in grace[0].detail
        # Après la grâce, le palier suivant est atteint.
        later = await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=12), stats=_stats(400.0))
        assert [event["action"] for event in later] == ["notify"], later


class TestLimiteHoraire:
    def _seed(self, db, machine, count, *, minutes_ago=5, simulated=False,
              result="ok", device_id=0):
        for index in range(count):
            db.add(WatchdogEvent(
                ts=W._iso(T0 - timedelta(minutes=minutes_ago + index)),
                machine_id=machine.id, machine_name=machine.name,
                device_id=device_id,
                rule="hashrate_low", measure="400 H/s", action="restart_miner",
                result=result, simulated=simulated, detail="séance précédente",
            ))
        db.commit()

    @pytest.mark.asyncio
    async def test_trois_relances_puis_watchdog_bloque(self, db, monkeypatch):
        from app import notifications

        sent = []
        monkeypatch.setattr(notifications, "enqueue",
                            lambda *a, **k: sent.append((a, k)))
        machine = _machine(db, simulation_disabled=True)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0, escalation_minutes=0)
        self._seed(db, machine, 3, device_id=device.id)

        events = await W.evaluate_machine(
            db, machine, now=T0, stats=_stats(400.0))
        assert [event["result"] for event in events] == ["blocked"], events
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert row.action == "notify" and row.result == "blocked"
        assert "3 relance(s) dans l'heure" in row.detail
        assert any("watchdog_blocked" == arg[1] for arg, _ in sent), sent

    @pytest.mark.asyncio
    async def test_les_relances_simulees_ne_comptent_pas(self, db, monkeypatch):
        machine = _machine(db, simulation_disabled=True)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0, escalation_minutes=0)
        self._seed(db, machine, 3, simulated=True)
        assert W.counted_actions(db, machine.id, 0, T0) == 0
        events = await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        assert [event["action"] for event in events] == ["restart_miner"], events

    @pytest.mark.asyncio
    async def test_relances_de_plus_d_une_heure_oubliees(self, db):
        machine = _machine(db)
        self._seed(db, machine, 3, minutes_ago=70)
        assert W.counted_actions(db, machine.id, 0, T0) == 0


# ── Mineur externe, ASIC, simulation ──────────────────────────────────
class TestMineurExterne:
    @pytest.mark.parametrize("cas", [
        {"name": "BigCoin solo", "kind": "solo", "binary": "/opt/bigcoin/bigcoind"},
        {"name": "WAM", "kind": "gpu", "binary": "/opt/wam/wamminer"},
        {"name": "nœud Korsh", "kind": "node", "binary": "/usr/local/bin/korshd"},
    ])
    @pytest.mark.asyncio
    async def test_notification_seule(self, db, monkeypatch, cas):
        machine = _machine(db, name=f"ext-{cas['kind']}", simulation_disabled=True)
        machine.external_miners = json.dumps([cas])
        db.commit()
        seen = []

        async def fake_action(db_, machine_, device_, action, detail, **kw):
            seen.append(action)
            return {"ok": True, "detail": "notification seule", "notify_only": True}

        monkeypatch.setattr(W, "perform_action", fake_action)
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        device = _device(db, machine, model="Carte du mineur externe")
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0, escalation_minutes=0)

        events = await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        assert [event["action"] for event in events] == ["notify"], events
        assert seen == ["notify"]
        assert calls == []
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert row.rule == "hashrate_low" and row.action == "notify"
        assert "mineur externe" in row.detail and row.simulated is False

    @pytest.mark.asyncio
    async def test_device_occupe_par_un_mineur_externe(self, db):
        machine = _machine(db)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id, occupied_external=True)
        config = W.normalize({})
        state = types.SimpleNamespace(stage=0, last_action_at=None)
        action, detail = W.choose_action(db, machine, device, state, config, T0)
        assert action == "notify" and "mineur externe" in detail

    @pytest.mark.asyncio
    async def test_une_seule_notification_par_fenetre(self, db, monkeypatch):
        from app import notifications

        sent = []
        monkeypatch.setattr(notifications, "enqueue",
                            lambda *a, **k: sent.append(a))
        machine = _machine(db, name="ext-bigcoin")
        machine.external_miners = json.dumps([{"name": "BigCoin solo"}])
        db.commit()
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0, escalation_minutes=5)
        await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        await W.evaluate_machine(
            db, machine, now=T0 + timedelta(minutes=1), stats=_stats(400.0))
        count = db.query(WatchdogEvent).filter(WatchdogEvent.rule == "hashrate_low").count()
        assert count == 1, count


class TestAsic:
    @pytest.mark.asyncio
    async def test_notification_seule(self, db, monkeypatch):
        machine = _machine(db, name="asic-s19", machine_kind="asic")
        seen = []

        async def fake_action(db_, machine_, device_, action, detail, **kw):
            seen.append(action)
            return {"ok": True, "detail": "notification seule", "notify_only": True}

        monkeypatch.setattr(W, "perform_action", fake_action)
        _save(db, rule="unreachable", minutes=0, escalation_minutes=0)
        events = await W.evaluate_machine(db, machine, now=T0, offline="panneau muet")
        assert [event["action"] for event in events] == ["notify"], events
        assert seen == ["notify"]
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert "ASIC" in row.detail


class TestSimulation:
    @pytest.mark.asyncio
    async def test_machine_non_armee_action_simulee(self, db, monkeypatch):
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        machine = _machine(db)  # simulation_disabled = False → simulée
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0, escalation_minutes=0)
        events = await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        assert [event["action"] for event in events] == ["restart_miner"], events
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert row.simulated is True
        assert "simulé" in row.detail
        assert calls == []

    @pytest.mark.asyncio
    async def test_machine_armee_action_reelle(self, db, monkeypatch):
        machine = _machine(db, simulation_disabled=True)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0, escalation_minutes=0)
        calls = _spy(monkeypatch, __import__("app.ssh_client", fromlist=["ssh_run_command"]))
        events = await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        assert [event["action"] for event in events] == ["restart_miner"], events
        assert len(calls) == 1
        assert calls[0][0][1] == "systemctl restart fossor-miner-gpu-pearl.service"


# ── Reboot et power cycle ─────────────────────────────────────────────
class TestRebootEtPowerCycle:
    @pytest.mark.asyncio
    async def test_reboot_refuse_sans_valider(self, db, monkeypatch):
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        machine = _machine(db)
        result = await W._reboot(machine, simulated=False)
        assert result["ok"] is False and "VALIDER" in result["detail"]
        assert calls == []

    @pytest.mark.asyncio
    async def test_reboot_valide_execute_une_commande_constante(self, db, monkeypatch):
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        machine = _machine(db, watchdog_reboot_ok=True)
        result = await W._reboot(machine, simulated=False)
        assert result["ok"] is True
        assert calls[0][0][1] == "systemctl reboot"

    @pytest.mark.asyncio
    async def test_reboot_interdit_sur_asic(self, db):
        machine = _machine(db, machine_kind="asic", watchdog_reboot_ok=True)
        result = await W._reboot(machine, simulated=False)
        assert result["ok"] is False and "ASIC" in result["detail"]

    @pytest.mark.asyncio
    async def test_power_cycle_refuse_sans_valider(self, db):
        machine = _machine(db)
        result = await W._power_cycle(db, machine, simulated=False, now=T0)
        assert result["ok"] is False and "VALIDER" in result["detail"]

    @pytest.mark.asyncio
    async def test_api_authorize_exige_le_mot(self, db, client):
        machine = _machine(db, name="rig-autorisation")
        url = f"/api/watchdog/machines/{machine.id}/authorize"
        refused = await client.post(url, json={"action": "reboot", "confirm": "oui"})
        assert refused.status_code == 400
        assert "VALIDER" in refused.json()["detail"]
        accepted = await client.post(url, json={"action": "reboot", "confirm": "VALIDER"})
        assert accepted.status_code == 200 and accepted.json()["enabled"] is True
        db.expire_all()
        assert db.get(Machine, machine.id).watchdog_reboot_ok is True
        state = await client.get("/api/watchdog")
        assert state.json()["config"]["actions"]["reboot"] is False

    @pytest.mark.asyncio
    async def test_palier_reboot_sans_autorisation_reste_notification(self, db, monkeypatch):
        machine = _machine(db, watchdog_reboot_ok=False)
        seen = []

        async def fake_action(db_, machine_, device_, action, detail, **kw):
            seen.append(action)
            return {"ok": True, "detail": "notification seule", "notify_only": True}

        monkeypatch.setattr(W, "perform_action", fake_action)
        config = W.normalize({})
        config["actions"]["reboot"] = True
        _apply = W._apply_rule if hasattr(W, "_apply_rule") else None
        state = WatchdogState(machine_id=machine.id, device_id=0, rule="unreachable",
                              since=W._iso(T0 - timedelta(minutes=30)), stage=2)
        db.add(state)
        db.commit()
        events = []
        await W._apply_rule(db, machine, None, "unreachable", active=True,
                            measure="panneau muet", minutes=0, config=config,
                            moment=T0, stdout="", runner=None, events=events)
        assert seen == ["notify"], seen
        assert events[0]["action"] == "notify"


# ── Campagne en bascule ───────────────────────────────────────────────
class TestCampagneEnBascule:
    def _campaign(self, db, machine, profile, status, mode="host"):
        campaign = Campaign(
            uuid=str(uuid.uuid4()), coin="PRL", profile_id=profile.id,
            start_at=W._iso(T0), status=status, execution_mode=mode,
        )
        db.add(campaign)
        db.commit()
        db.add(CampaignDevice(campaign_id=campaign.id, machine_id=machine.id))
        db.commit()
        return campaign

    @pytest.mark.asyncio
    async def test_aucune_action_pendant_la_bascule(self, db, monkeypatch):
        machine = _machine(db, name="rig-bascule", simulation_disabled=True)
        profile = _profile(db)
        device = _device(db, machine, token=profile.id)
        _metrics(db, machine, device, [1000.0] * 6)
        self._campaign(db, machine, profile, "approved")
        seen = []

        async def fake_action(db_, machine_, device_, action, detail, **kw):
            seen.append(action)
            return {"ok": True, "detail": "notification seule", "notify_only": True}

        monkeypatch.setattr(W, "perform_action", fake_action)
        _save(db, rule="hashrate_low", minutes=0, pct=50.0, escalation_minutes=0)
        events = await W.evaluate_machine(db, machine, now=T0, stats=_stats(400.0))
        assert [event["action"] for event in events] == ["notify"], events
        assert seen == ["notify"]
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert "en bascule" in row.detail and "aucune action" in row.detail


# ── Aucune commande SSH par concaténation de texte libre ──────────────
class TestCommandesConstruites:
    @pytest.mark.asyncio
    async def test_unite_issue_du_slug_du_profil(self, db, monkeypatch):
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        machine = _machine(db, name="rig-slug", ssh_user="miner")
        profile = _profile(db, name="Mineur; rm -rf / #")
        unit = W.profile_unit(profile)
        assert unit == "fossor-miner-mineur-rm-rf.service"
        result = await W._restart_miner(db, machine, None, unit, simulated=False)
        assert result["ok"] is True
        assert len(calls) == 1
        command = calls[0][0][1]
        assert command == f"sudo -n systemctl restart {unit}"
        assert re.fullmatch(r"sudo -n systemctl restart fossor-miner-[a-z0-9-]+\.service",
                            command)
        assert ";" not in command and "#" not in command

    @pytest.mark.asyncio
    async def test_nom_de_vm_refuse_sil_contient_du_texte_libre(self, db, monkeypatch):
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        host = _machine(db, name="hote-vm", simulation_disabled=True)
        machine = _machine(db, name="rig-vm")
        profile = _profile(db)
        campaign = Campaign(
            uuid=str(uuid.uuid4()), coin="PRL", profile_id=profile.id,
            start_at=W._iso(T0), status="running", execution_mode="vm",
        )
        db.add(campaign)
        db.commit()
        db.add(CampaignDevice(campaign_id=campaign.id, machine_id=machine.id))
        db.add(SpecVm(uuid=str(uuid.uuid4()), campaign_id=campaign.id,
                      host_machine_id=host.id, name="fossor-vm; reboot"))
        db.commit()
        result = await W._restart_vm(db, machine, simulated=False)
        assert result["ok"] is False and "nom de VM" in result["detail"]
        assert calls == []

    @pytest.mark.asyncio
    async def test_nom_de_vm_valide_execute_virsh(self, db, monkeypatch):
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        host = _machine(db, name="hote-vm2", simulation_disabled=True)
        machine = _machine(db, name="rig-vm2")
        profile = _profile(db)
        campaign = Campaign(
            uuid=str(uuid.uuid4()), coin="PRL", profile_id=profile.id,
            start_at=W._iso(T0), status="running", execution_mode="vm",
        )
        db.add(campaign)
        db.commit()
        db.add(CampaignDevice(campaign_id=campaign.id, machine_id=machine.id))
        db.add(SpecVm(uuid=str(uuid.uuid4()), campaign_id=campaign.id,
                      host_machine_id=host.id, name="fossor-spec-1"))
        db.commit()
        result = await W._restart_vm(db, machine, simulated=False)
        assert result["ok"] is True
        assert calls[0][0][1] == "virsh reboot fossor-spec-1"

    @pytest.mark.asyncio
    async def test_vmid_proxmox_converti_en_entier(self, db, monkeypatch):
        from app import ssh_client

        calls = _spy(monkeypatch, ssh_client)
        host = _machine(db, name="hote-pve", simulation_disabled=True)
        machine = _machine(db, name="rig-pve")
        profile = _profile(db)
        campaign = Campaign(
            uuid=str(uuid.uuid4()), coin="PRL", profile_id=profile.id,
            start_at=W._iso(T0), status="running", execution_mode="vm",
        )
        db.add(campaign)
        db.commit()
        db.add(CampaignDevice(campaign_id=campaign.id, machine_id=machine.id))
        db.add(SpecVm(uuid=str(uuid.uuid4()), campaign_id=campaign.id,
                      host_machine_id=host.id, name="fossor-pve-1",
                      hypervisor="pve", vmid=103))
        db.commit()
        result = await W._restart_vm(db, machine, simulated=False)
        assert result["ok"] is True
        assert calls[0][0][1] == "qm reboot 103"
