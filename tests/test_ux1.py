"""UX-1 (§4.19) : état de santé, rôles des devices, réglages de mise à jour,
actions ``rollback`` / ``forget_previous`` du compagnon.

Aucune machine réelle n'est contactée : tout est calcul pur, base de test ou
faux runner.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import health, update_settings


@pytest.fixture
def db():
    """Session sur la base de test du cas courant (DATABASE_URL posé par conftest)."""
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def machine(**extra):
    base = {
        "uuid": "u-1",
        "name": "rig-1",
        "collected": True,
        "online": True,
        "devices": [],
        "total_hashrate_hs": 1000.0,
        "cpu": {},
    }
    base.update(extra)
    return base


class TestHealthRules:
    def test_green_when_everything_is_fine(self):
        result = health.evaluate(machine(), {"__machine__": 1000.0})
        assert result["status"] == "green"
        assert result["reason"] == ""

    def test_never_collected_is_orange(self):
        assert health.evaluate(machine(collected=False), {})["reason"] == "jamais collectée"

    def test_offline_is_red_with_the_error(self):
        result = health.evaluate(machine(online=False, error="timeout SSH"), {})
        assert result["status"] == "red"
        assert result["reason"] == "hors ligne"
        assert result["detail"] == "timeout SSH"

    def test_miner_api_unreachable_is_orange(self):
        result = health.evaluate(machine(api_error="API injoignable"), {})
        assert result["status"] == "orange"
        assert result["reason"] == "mineur : API injoignable"

    def test_enabled_device_without_hashrate_is_orange(self):
        devices = [{"kind": "gpu", "index": 1, "enabled_for_mining": True, "hashrate_hs": None}]
        result = health.evaluate(machine(devices=devices), {})
        assert result["status"] == "orange"
        assert result["reason"] == "GPU 1 sans hashrate"

    def test_disabled_device_is_ignored(self):
        devices = [{"kind": "gpu", "index": 1, "enabled_for_mining": False, "hashrate_hs": None}]
        assert health.evaluate(machine(devices=devices), {})["status"] == "green"

    def test_integrated_unselected_device_is_ignored(self):
        devices = [{"kind": "gpu", "index": 0, "enabled_for_mining": False,
                    "integrated": True, "hashrate_hs": None}]
        assert health.evaluate(machine(devices=devices), {})["status"] == "green"

    def test_hashrate_below_seventy_percent_is_orange(self):
        result = health.evaluate(machine(total_hashrate_hs=600.0), {"__machine__": 1000.0})
        assert result["status"] == "orange"
        assert result["reason"] == "hashrate à 60 % de la moyenne 1 h"

    def test_hashrate_exactly_at_the_threshold_stays_green(self):
        assert health.evaluate(machine(total_hashrate_hs=700.0),
                               {"__machine__": 1000.0})["status"] == "green"

    def test_gpu_temperature_threshold(self):
        devices = [{"kind": "gpu", "index": 0, "enabled_for_mining": True,
                    "hashrate_hs": 10.0, "temp_c": 80.5}]
        result = health.evaluate(machine(devices=devices), {})
        assert result["status"] == "orange"
        assert result["reason"] == "GPU 0 à 80 °C"

    def test_gpu_temperature_at_the_limit_is_green(self):
        devices = [{"kind": "gpu", "index": 0, "enabled_for_mining": True,
                    "hashrate_hs": 10.0, "temp_c": 80.0}]
        assert health.evaluate(machine(devices=devices), {})["status"] == "green"

    def test_cpu_temperature_threshold(self):
        devices = [{"kind": "cpu", "enabled_for_mining": True, "hashrate_hs": 10.0,
                    "temp_c": 91.0}]
        assert health.evaluate(machine(devices=devices), {})["reason"] == "CPU à 91 °C"
        assert health.evaluate(machine(cpu={"temp_c": 95.0}), {})["reason"] == "CPU à 95 °C"

    def test_psu_warning_and_critical(self):
        assert health.evaluate(machine(), {}, psu_pct=80.0)["status"] == "green"
        orange = health.evaluate(machine(), {}, psu_pct=85.0)
        assert orange["status"] == "orange" and "alimentation" in orange["reason"]
        red = health.evaluate(machine(), {}, psu_pct=95.0)
        assert red["status"] == "red" and "alimentation" in red["reason"]

    def test_thresholds_are_adjustable(self):
        limits = dict(health.DEFAULTS)
        limits["gpu_temp_max_c"] = 70.0
        devices = [{"kind": "gpu", "index": 0, "enabled_for_mining": True,
                    "hashrate_hs": 10.0, "temp_c": 75.0}]
        assert health.evaluate(machine(devices=devices), {}, limits)["status"] == "orange"
        limits["hashrate_min_pct"] = 50.0
        assert health.evaluate(machine(total_hashrate_hs=600.0),
                               {"__machine__": 1000.0}, limits)["status"] == "green"


class TestHealthThresholds:
    def test_defaults_and_normalisation(self):
        assert health.normalize({}) == health.DEFAULTS
        fixed = health.normalize({"hashrate_min_pct": 999, "gpu_temp_max_c": "abc"})
        assert fixed["hashrate_min_pct"] == 100.0
        assert fixed["gpu_temp_max_c"] == health.DEFAULTS["gpu_temp_max_c"]

    def test_round_trip_through_settings(self, db):
        saved = health.save_thresholds(db, {"hashrate_min_pct": 65, "gpu_temp_max_c": 78})
        assert saved["hashrate_min_pct"] == 65.0
        assert health.thresholds(db)["gpu_temp_max_c"] == 78.0

    def test_out_of_bounds_and_incoherent_psu_are_refused(self, db):
        with pytest.raises(ValueError):
            health.save_thresholds(db, {"hashrate_min_pct": 0})
        with pytest.raises(ValueError):
            health.save_thresholds(db, {"psu_warn_pct": 95, "psu_crit_pct": 90})


class TestDeviceRoles:
    def test_campaign_refuses_default_only_device_with_422(self, monkeypatch):
        from app.routers import campaigns

        machine_row = SimpleNamespace(id=1, name="rig-1")
        device = SimpleNamespace(
            uuid="dev-1", machine_id=1, occupied_external=False, model="RTX 3070",
            kind="gpu", index=0, role="default_only",
        )

        class FakeQuery:
            def __init__(self, value):
                self.value = value

            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return self.value

        class FakeDB:
            def query(self, model):
                from app.models import Device, Machine

                if model is Machine:
                    return FakeQuery(machine_row)
                if model is Device:
                    return FakeQuery(device)
                return FakeQuery(None)

        with pytest.raises(HTTPException) as exc:
            campaigns._clean_devices(FakeDB(), [{"machine_id": 1, "device_uuid": "dev-1"}])
        assert exc.value.status_code == 422
        assert "token par défaut" in exc.value.detail

    def test_default_and_spec_device_is_accepted(self):
        from app.routers import campaigns

        machine_row = SimpleNamespace(id=1, name="rig-1")
        device = SimpleNamespace(
            uuid="dev-2", machine_id=1, occupied_external=False, model="RTX 3070",
            kind="gpu", index=0, role="default_and_spec",
        )

        class FakeQuery:
            def __init__(self, value):
                self.value = value

            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return self.value

        class FakeDB:
            def query(self, model):
                from app.models import Device, Machine

                return FakeQuery(machine_row if model is Machine else device)

        rows = campaigns._clean_devices(FakeDB(), [{"machine_id": 1, "device_uuid": "dev-2"}])
        assert rows[0]["device_uuid"] == "dev-2"


class TestUpdateSettings:
    def test_defaults(self, db):
        values = update_settings.read(db)
        assert values == {"check_interval_s": 3600, "auto_apply": False, "ignorer_auto": ""}

    def test_interval_is_validated(self, db):
        with pytest.raises(ValueError):
            update_settings.save(db, {"check_interval_s": 42})
        saved = update_settings.save(db, {"check_interval_s": 21600, "auto_apply": True})
        assert saved["check_interval_s"] == 21600
        assert update_settings.read(db)["auto_apply"] is True

    def test_ignorer_auto_after_a_rollback(self, db):
        update_settings.save(db, {"auto_apply": True})
        values = update_settings.ignore_auto(db, "2026.11.005")
        assert values["ignorer_auto"] == "2026.11.005"
        assert update_settings.read(db)["ignorer_auto"] == "2026.11.005"
        # une version invalide n'entre pas dans le réglage
        with pytest.raises(ValueError):
            update_settings.save(db, {"ignorer_auto": "n-importe-quoi"})
        assert update_settings.ignore_auto(db, "")["ignorer_auto"] == ""

    def test_auto_apply_is_off_by_default(self, db):
        assert update_settings.read(db)["auto_apply"] is False


class FakeRunner:
    def __init__(self, present=True):
        self.present = present
        self.removed = []
        self.ups = []

    def image_present(self, reference):
        return self.present

    def remove_image(self, reference):
        self.removed.append(reference)
        return True, "untagged"

    def compose_up(self, restore=None):
        self.ups.append(restore)
        return True, "ok"


def updater_under_test(tmp_path, runner):
    from updater.updater import Updater

    updater = Updater.__new__(Updater)
    updater.config = SimpleNamespace(
        image="ghcr.io/lostinthebugs/fossor",
        env_path=tmp_path / ".env",
        backup_dir=tmp_path / "backups",
        data_dir=tmp_path,
        tag_key="FOSSOR_IMAGE_TAG",
    )
    updater.runner = runner
    updater.status = {"state": "idle"}
    updater.history = []
    updater._write_status = lambda: None
    updater._history = lambda event, **extra: updater.history.append({"event": event, **extra})
    updater._transition = lambda state, step, message, **kw: updater.status.update(
        {"state": state, "step": step, "message": message, **kw}
    )
    updater._remember_nonce = lambda nonce: None
    updater._reject = lambda request, reason: updater.status.update(
        {"state": "failed", "step": "refus", "message": reason}
    ) or "failed"
    return updater


class TestUpdaterActions:
    def test_forget_previous_removes_the_kept_image(self, tmp_path):
        runner = FakeRunner()
        updater = updater_under_test(tmp_path, runner)
        updater.status["precedente"] = {"version": "2026.11.003", "image": True}
        result = updater._forget_previous({"target": "2026.11.003", "nonce": "n" * 8})
        assert result == "done"
        assert runner.removed == ["ghcr.io/lostinthebugs/fossor:2026.11.003"]
        assert updater.status["precedente"] is None
        assert updater.history[-1]["event"] == "forgotten"

    def test_forget_previous_refuses_without_kept_version(self, tmp_path):
        updater = updater_under_test(tmp_path, FakeRunner())
        assert updater._forget_previous({"target": "2026.11.003", "nonce": "n" * 8}) == "failed"

    def test_manual_rollback_restores_env_image_and_backup(self, tmp_path):
        runner = FakeRunner()
        updater = updater_under_test(tmp_path, runner)
        (tmp_path / ".env").write_text("FOSSOR_IMAGE=ghcr.io/lostinthebugs/fossor\nFOSSOR_IMAGE_TAG=2026.11.005\n")
        backup = tmp_path / "backups" / "pre-2026.11.005-20261118T100000Z"
        backup.mkdir(parents=True)
        (backup / "fossor.db").write_bytes(b"db")
        updater.status["precedente"] = {"version": "2026.11.004", "image": True}
        updater.wait_healthy = lambda target: (True, "2026.11.004")

        result = updater._manual_rollback({"target": "2026.11.004", "nonce": "n" * 8})
        assert result == "rolled_back"
        assert "FOSSOR_IMAGE_TAG=2026.11.004" in (tmp_path / ".env").read_text()
        assert runner.ups == [str(backup)]
        assert updater.status["state"] == "rolled_back"
        assert updater.status["precedente"]["version"] == "2026.11.005"

    def test_manual_rollback_refuses_without_backup(self, tmp_path):
        updater = updater_under_test(tmp_path, FakeRunner())
        (tmp_path / ".env").write_text("FOSSOR_IMAGE_TAG=2026.11.005\n")
        updater.status["precedente"] = {"version": "2026.11.004", "image": True}
        assert updater._manual_rollback({"target": "2026.11.004", "nonce": "n" * 8}) == "failed"
