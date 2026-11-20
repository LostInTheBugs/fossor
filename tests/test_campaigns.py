"""Campagnes de minage (§4.7) — machine à états, contrôles, planificateur.

Les tests du planificateur utilisent une **horloge simulée** et un backend
factice : aucune connexion, aucun SSH, aucun mineur. L'intégration réelle est
dans ``tests/test_campaigns_integration.py``.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

import pytest

from app import campaigns as C
from app import campaign_runner as RUN


T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


# ── Machine à états ───────────────────────────────────────────────────
class TestStateMachine:
    def test_toutes_les_transitions_autorisees(self):
        expected = {
            "proposed": {"approved", "cancelled"},
            "approved": {"running", "cancelled"},
            "running": {"stopping", "failed", "cancelled", "miner_stopped"},
            "stopping": {"done", "failed", "cancelled"},
            "done": set(),
            "failed": set(),
            "cancelled": set(),
            "miner_stopped": set(),
        }
        for source, targets in expected.items():
            assert set(C.next_statuses(source)) == targets, source

    def test_transitions_du_cycle_nominal(self):
        for source, target in [
            ("proposed", "approved"),
            ("approved", "running"),
            ("running", "stopping"),
            ("stopping", "done"),
            ("running", "failed"),
            ("approved", "cancelled"),
        ]:
            assert C.can_transition(source, target) is True
            C.ensure_transition(source, target)

    def test_transitions_interdites_refusees(self):
        forbidden = [
            ("proposed", "running"),
            ("proposed", "done"),
            ("approved", "stopping"),
            ("running", "approved"),
            ("running", "done"),
            ("stopping", "running"),
            ("done", "running"),
            ("done", "cancelled"),
            ("failed", "approved"),
            ("cancelled", "proposed"),
        ]
        for source, target in forbidden:
            assert C.can_transition(source, target) is False, (source, target)
            with pytest.raises(C.CampaignError):
                C.ensure_transition(source, target)

    def test_statut_inconnu_refuse(self):
        with pytest.raises(C.CampaignError):
            C.check_status("en_cours")

    def test_apply_transition_ecrit_le_statut(self):
        class Row:
            status = "proposed"

        row = Row()
        C.apply_transition(row, "approved")
        assert row.status == "approved"
        with pytest.raises(C.CampaignError):
            C.apply_transition(row, "done")


# ── Contrôles avant démarrage ────────────────────────────────────────
class TestPreflight:
    def test_ok_quand_tout_va_bien(self):
        verdict = C.preflight(machine_online=True, psu_worst_pct=42.0)
        assert verdict["ok"] is True
        assert verdict["blocking"] == []

    def test_device_deja_engage(self):
        verdict = C.preflight(busy_devices=["rig / GPU 0 (campagne en cours)"])
        assert verdict["ok"] is False
        assert verdict["blocking"][0]["code"] == "device_busy"
        assert "GPU 0" in verdict["blocking"][0]["message"]

    def test_machine_injoignable(self):
        verdict = C.preflight(machine_online=False)
        assert verdict["ok"] is False
        assert verdict["blocking"][0]["code"] == "machine_unreachable"

    def test_profil_invalide(self):
        verdict = C.preflight(profile_error="SHA256 obligatoire")
        assert verdict["ok"] is False
        assert verdict["blocking"][0]["code"] == "profile_invalid"

    def test_charge_psu_sans_confirmation_refusee(self):
        verdict = C.preflight(psu_worst_pct=91.0)
        assert verdict["ok"] is False
        assert verdict["blocking"][0]["code"] == "psu_overload"
        assert verdict["psu"]["limit_pct"] == 80.0

    def test_charge_psu_confirmee_acceptee(self):
        verdict = C.preflight(psu_worst_pct=91.0, confirm_overload=True)
        assert verdict["ok"] is True
        assert verdict["warnings"][0]["code"] == "psu_overload_confirmed"


# ── Gain et coût ─────────────────────────────────────────────────────
class TestGainCost:
    def test_gain_est_la_difference_des_soldes(self):
        assert C.campaign_gain(100.0, 137.5) == 37.5
        assert C.campaign_gain(None, 10.0) is None
        assert C.campaign_gain(5.0, None) is None

    def test_cout_puissance_a_la_prise(self):
        # 250 W à la prise pendant 2 h à 0,20 €/kWh = 0,1 kWh = 0,02 €
        assert C.campaign_cost_eur(250.0, 2.0, 0.20) == 0.1
        # 300 W × 1 h = 0,3 kWh × 0,20 = 0,06 €
        assert C.campaign_cost_eur(300.0, 1.0, 0.20) == 0.06

    def test_cout_sans_puissance_ou_tarif(self):
        assert C.campaign_cost_eur(None, 2.0, 0.20) is None
        assert C.campaign_cost_eur(250.0, 2.0, None) is None

    def test_heures_entre_deux_instants(self):
        assert C.hours_between(T0, T0 + timedelta(minutes=90)) == 1.5
        assert C.hours_between(T0, T0 - timedelta(minutes=5)) == 0.0

    def test_fenetre_validee(self):
        start, end = C.validate_window("2026-11-18T12:00:00Z", "2026-11-18T14:00:00Z")
        assert start.startswith("2026-11-18T12:00:00")
        assert end.startswith("2026-11-18T14:00:00")
        assert C.validate_window("2026-11-18T12:00:00Z", None) == (
            "2026-11-18T12:00:00+00:00",
            None,
        )
        with pytest.raises(C.CampaignError):
            C.validate_window("2026-11-18T12:00:00Z", "2026-11-18T11:00:00Z")


# ── Planificateur ────────────────────────────────────────────────────
WALLET = "prl1demo" + "q" * 30
SHA = "a" * 64


class FakeBackend:
    """Backend factice : enregistre les appels, ne touche à rien."""

    def __init__(self, default_hashrate=1500.0, fail_default_start=False, fail_all_start=False):
        self.calls: list[tuple] = []
        self.default_hashrate = default_hashrate
        self.fail_default_start = fail_default_start
        self.fail_all_start = fail_all_start

    async def start(self, machine, profile, argv, simulated):
        self.calls.append(("start", profile["name"], simulated, tuple(argv)))
        if self.fail_all_start:
            raise RuntimeError("binaire cassé")
        if self.fail_default_start and profile["name"] == "Token par défaut":
            raise RuntimeError("binaire par défaut cassé")
        return {"simulated": simulated, "commands": ["start " + profile["name"]]}

    async def stop(self, machine, profile, simulated):
        self.calls.append(("stop", profile["name"], simulated))
        return {"simulated": simulated, "commands": ["stop " + profile["name"]]}

    async def hashrate(self, machine, profile):
        return self.default_hashrate

    def actions(self) -> list[tuple]:
        return [(kind, name) for kind, name, *_ in self.calls]


def _profile(db, name, worker, api_port=0):
    from app.models import MinerProfile

    existing = db.query(MinerProfile).filter(MinerProfile.name == name).first()
    if existing is not None:
        return existing
    row = MinerProfile(
        uuid=str(uuid_lib.uuid4()),
        name=name,
        coin="FAKE",
        wallet=WALLET,
        pool="stratum+tcp://pool.exemple.tld:3333",
        software="custom",
        binary_url="http://127.0.0.1:8099/faux-mineur.tar.gz",
        sha256=SHA,
        binary_path="faux-mineur",
        version="1.0.0",
        cmdline=(
            f"/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {{wallet}} --pool {{pool}} "
            f"--worker {worker} --gpus {{gpus}} --api-port {api_port}"
        ),
        api_port=api_port,
        execution_mode="host",
        enabled=True,
    )
    db.add(row)
    db.flush()
    return row


def _seed(db, *, occupied_external=False, status="approved", start_offset=0, end_offset=120):
    from app.models import Campaign, CampaignDevice, Device, Machine

    machine = Machine(
        uuid=str(uuid_lib.uuid4()),
        name="cible",
        host="192.0.2.10",
        ssh_port=2222,
        ssh_user="fossor",
        auth_type="fossor_key",
        os_kind="ubuntu",
        enabled=True,
        usage="both",
    )
    db.add(machine)
    db.flush()
    default_profile = _profile(db, "Token par défaut", "defaut", api_port=21399)
    campaign_profile = _profile(db, "Campagne spec", "campagne", api_port=21398)
    machine.default_miner_profile_id = default_profile.id
    gpu = Device(
        uuid=str(uuid_lib.uuid4()),
        machine_id=machine.id,
        kind="gpu",
        index=0,
        model="GPU jetable",
        present=True,
        enabled_for_mining=True,
        occupied_external=occupied_external,
    )
    db.add(gpu)
    db.flush()
    campaign = Campaign(
        uuid=str(uuid_lib.uuid4()),
        coin="FAKE",
        profile_id=campaign_profile.id,
        start_at=C.iso(T0 + timedelta(seconds=start_offset)),
        end_at=C.iso(T0 + timedelta(seconds=end_offset)) if end_offset is not None else None,
        status=status,
        origin="manual",
        started_at=C.iso(T0) if status in ("running", "stopping") else None,
    )
    db.add(campaign)
    db.flush()
    db.add(
        CampaignDevice(
            campaign_id=campaign.id,
            machine_id=machine.id,
            device_uuid=gpu.uuid,
            kind="gpu",
            label="GPU 0 — GPU jetable",
        )
    )
    db.commit()
    return campaign, default_profile, campaign_profile


def _db():
    from app.database import SessionLocal

    return SessionLocal()


class TestScheduler:
    async def test_demarrage_au_start_at(self):
        db = _db()
        try:
            campaign, _, _ = _seed(db)
            backend = FakeBackend()
            result = await RUN.tick(db, now=T0, backend=backend, simulated=False)
            db.refresh(campaign)
            assert result[0]["action"] == "started"
            assert campaign.status == "running"
            assert backend.actions() == [
                ("stop", "Token par défaut"),
                ("start", "Campagne spec"),
            ]
        finally:
            db.close()

    async def test_journal_du_demarrage(self):
        from app.models import CampaignEvent

        db = _db()
        try:
            campaign, _, _ = _seed(db)
            await RUN.tick(db, now=T0, backend=FakeBackend(), simulated=False)
            kinds = [
                row.kind
                for row in db.query(CampaignEvent)
                .filter(CampaignEvent.campaign_id == campaign.id)
                .order_by(CampaignEvent.id.asc())
                .all()
            ]
            assert kinds == ["default-stopped", "campaign-started", "running"]
        finally:
            db.close()

    async def test_pas_de_double_action(self):
        db = _db()
        try:
            campaign, _, _ = _seed(db)
            backend = FakeBackend()
            await RUN.tick(db, now=T0, backend=backend, simulated=False)
            await RUN.tick(db, now=T0, backend=backend, simulated=False)
            db.refresh(campaign)
            assert campaign.status == "running"
            assert backend.actions() == [
                ("stop", "Token par défaut"),
                ("start", "Campagne spec"),
            ]
        finally:
            db.close()

    async def test_fin_retour_verifie_done(self):
        db = _db()
        try:
            campaign, default_profile, campaign_profile = _seed(db, status="running", end_offset=120)
            backend = FakeBackend()
            result = await RUN.tick(db, now=T0 + timedelta(seconds=120), backend=backend, simulated=False)
            db.refresh(campaign)
            assert result[-1]["action"] == "done"
            assert campaign.status == "done"
            assert campaign.verified is True
            assert backend.actions() == [
                ("stop", "Campagne spec"),
                ("start", "Token par défaut"),
            ]
        finally:
            db.close()

    async def test_reprise_apres_redemarrage(self):
        """L'étape déjà franchie (token arrêté) n'est pas rejouée."""
        db = _db()
        try:
            campaign, _, _ = _seed(db)
            RUN.log_event(db, campaign, "default-stopped", "déjà arrêté", simulated=False)
            db.commit()
            backend = FakeBackend()
            await RUN.tick(db, now=T0, backend=backend, simulated=False)
            db.refresh(campaign)
            assert campaign.status == "running"
            assert backend.actions() == [("start", "Campagne spec")]
        finally:
            db.close()

    async def test_arret_manuel(self):
        db = _db()
        try:
            campaign, _, _ = _seed(db, status="running", end_offset=None)
            campaign.stop_requested = True
            db.commit()
            await RUN.tick(db, now=T0 + timedelta(seconds=30), backend=FakeBackend(), simulated=False)
            db.refresh(campaign)
            assert campaign.status == "done"
        finally:
            db.close()

    async def test_retour_echoue_failed_apres_deux_essais(self):
        from app.models import NotificationLog

        db = _db()
        try:
            from app import notifications
            from app.ai_config import set_setting

            campaign, _, _ = _seed(db, status="running", end_offset=60)
            # Préprod : une action réelle exige que la machine soit armée.
            from app.models import Machine

            db.query(Machine).update({Machine.simulation_disabled: True})
            db.commit()
            # Sans canal configuré, `enqueue` n'écrit rien : la matrice est le
            # seul chemin par lequel une notification existe (§4.14).
            config = notifications.get_config(db)
            config["matrix"]["default_miner_restore_failed"] = ["discord"]
            config["matrix"]["campaign_failed"] = ["discord"]
            set_setting(db, notifications.CONFIG_KEY, config)
            db.commit()
            backend = FakeBackend(fail_default_start=True, default_hashrate=0.0)
            now = T0 + timedelta(seconds=60)
            for _ in range(8):
                await RUN.tick(db, now=now, backend=backend, simulated=False, timeout_s=60)
                now += timedelta(seconds=61)
            db.refresh(campaign)
            assert campaign.status == "failed"
            assert campaign.attempts == 3  # 1 tentative + 2 essais, puis échec
            starts = [call for call in backend.calls if call[0] == "start"]
            assert len(starts) == 3, starts
            rows = (
                db.query(NotificationLog)
                .filter(NotificationLog.event == "default_miner_restore_failed")
                .all()
            )
            assert rows, "notification critique attendue"
            assert rows[0].level == "critique"
        finally:
            db.close()

    async def test_device_occupe_par_une_autre_campagne(self):
        db = _db()
        try:
            from app.models import Campaign, CampaignDevice

            first, _, _ = _seed(db)
            second, _, _ = _seed(db)
            # La première tourne déjà sur le même device (même uuid réutilisé).
            device_uuid = (
                db.query(CampaignDevice)
                .filter(CampaignDevice.campaign_id == first.id)
                .first()
                .device_uuid
            )
            db.query(CampaignDevice).filter(CampaignDevice.campaign_id == second.id).update(
                {CampaignDevice.device_uuid: device_uuid}
            )
            first.status = "running"
            db.commit()
            backend = FakeBackend()
            result = await RUN.tick(db, now=T0, backend=backend, simulated=False)
            db.refresh(second)
            refused = [item for item in result if item.get("action") == "refused"]
            assert refused, result
            assert second.status == "failed"
            assert "campagne en cours" in " ".join(refused[0]["reasons"])
        finally:
            db.close()

    async def test_mineur_externe_refuse(self):
        db = _db()
        try:
            campaign, _, _ = _seed(db, occupied_external=True)
            result = await RUN.tick(db, now=T0, backend=FakeBackend(), simulated=False)
            db.refresh(campaign)
            assert result[0]["action"] == "refused"
            assert campaign.status == "failed"
            assert "mineur externe" in " ".join(result[0]["reasons"])
        finally:
            db.close()

    async def test_simulation_journalise_les_commandes(self):
        from app.models import CampaignEvent

        db = _db()
        try:
            campaign, _, _ = _seed(db)
            backend = FakeBackend()
            await RUN.tick(db, now=T0, backend=backend, simulated=True)
            events = (
                db.query(CampaignEvent)
                .filter(CampaignEvent.campaign_id == campaign.id)
                .order_by(CampaignEvent.id.asc())
                .all()
            )
            assert events[0].simulated is True
            assert "start Campagne spec" in (events[1].detail or "")
            assert all(call[2] is True for call in backend.calls)
        finally:
            db.close()

    async def test_cout_et_gain_a_la_fin(self):
        from app.models import Metric

        db = _db()
        try:
            from app.models import CampaignDevice, Machine

            campaign, default_profile, _ = _seed(db, status="running", end_offset=120)
            campaign.start_balance = 10.0
            machine = db.query(Machine).first()
            device_uuid = (
                db.query(CampaignDevice).filter(CampaignDevice.campaign_id == campaign.id).first().device_uuid
            )
            db.add(
                Metric(
                    machine_uuid=machine.uuid,
                    device_uuid=device_uuid,
                    ts=C.iso(T0 + timedelta(seconds=30)),
                    power_w=200.0,
                )
            )
            db.commit()
            await RUN.tick(db, now=T0 + timedelta(seconds=120), backend=FakeBackend(), simulated=False)
            db.refresh(campaign)
            assert campaign.status == "done"
            assert campaign.power_w == 200.0
            assert campaign.cost_eur is not None and campaign.cost_eur > 0
            assert campaign.gain is None  # aucun solde de pool lisible → pas inventé
        finally:
            db.close()


# ── API : CRUD, transitions, audit ───────────────────────────────────
class TestApi:
    async def test_creation_approbation_demarrage_arret(self, client):
        from app.database import SessionLocal
        from app.models import AuditLog, Campaign

        from app.models import CampaignDevice, Device, Machine

        db = SessionLocal()
        try:
            campaign, default_profile, campaign_profile = _seed(db, status="proposed")
            machine_id = db.query(Machine).first().id
            device_uuid = db.query(Device).filter_by(machine_id=machine_id).first().uuid
            seeded_id = campaign.id
            profile_id = campaign_profile.id
        finally:
            db.close()

        listed = await client.get("/api/campaigns")
        assert listed.status_code == 200
        assert listed.json()["simulation"] is True

        # Le CRUD passe par l'API : suppression d'une proposition, puis création.
        removed = await client.delete(f"/api/campaigns/{seeded_id}")
        assert removed.status_code == 200, removed.text
        created = await client.post(
            "/api/campaigns",
            json={
                "profile_id": profile_id,
                "coin": "FAKE",
                "start_at": C.iso(C.now_utc()),
                "end_at": C.iso(C.now_utc() + timedelta(hours=1)),
                "devices": [{"machine_id": machine_id, "device_uuid": device_uuid}],
            },
        )
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]
        assert created.json()["status"] == "proposed"

        approved = await client.post(f"/api/campaigns/{campaign_id}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"

        twice = await client.post(f"/api/campaigns/{campaign_id}/approve")
        assert twice.status_code == 409
        assert "Transition refusée" in twice.json()["detail"]

        started = await client.post(f"/api/campaigns/{campaign_id}/start")
        assert started.status_code == 200, started.text
        assert started.json()["campaign"]["status"] == "running"
        events = started.json()["campaign"]
        detail = await client.get(f"/api/campaigns/{campaign_id}")
        kinds = [row["kind"] for row in detail.json()["events"]]
        assert kinds[0] == "created"
        assert "default-stopped" in kinds and "campaign-started" in kinds
        simulated = [row for row in detail.json()["events"] if row["kind"] == "campaign-started"]
        assert simulated[0]["simulated"] is True
        assert "faux-mineur" in (simulated[0]["detail"] or "")

        stopped = await client.post(f"/api/campaigns/{campaign_id}/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["campaign"]["status"] == "done"
        assert stopped.json()["campaign"]["verified"] is True

        again = await client.post(f"/api/campaigns/{campaign_id}/cancel")
        assert again.status_code == 409

        db = SessionLocal()
        try:
            actions = [row.action for row in db.query(AuditLog).all()]
        finally:
            db.close()
        for action in ("campaign-create", "campaign-approve", "campaign-start-now", "campaign-stop-now"):
            assert action in actions, actions

    async def test_devices_occupes_par_un_mineur_externe(self, client):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            campaign, _, _ = _seed(db, occupied_external=True, status="proposed")
            machine = db.query(__import__("app.models", fromlist=["Machine"]).Machine).first()
            payload = {
                "profile_id": campaign.profile_id,
                "coin": "FAKE",
                "start_at": C.iso(C.now_utc()),
                "devices": [
                    {
                        "machine_id": machine.id,
                        "device_uuid": db.query(__import__("app.models", fromlist=["Device"]).Device)
                        .filter_by(machine_id=machine.id)
                        .first()
                        .uuid,
                    }
                ],
            }
        finally:
            db.close()
        refused = await client.post("/api/campaigns", json=payload)
        assert refused.status_code == 409
        assert "mineur externe" in refused.json()["detail"]

    async def test_creation_invalide(self, client):
        bad = await client.post(
            "/api/campaigns", json={"profile_id": 999, "coin": "X", "start_at": "pas une date", "devices": []}
        )
        assert bad.status_code == 400
