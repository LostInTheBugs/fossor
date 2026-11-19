"""Pilotage HiveOS (§3e) — analyse, masquage, plans, refus, simulation.

Formats vérifiés sur les fixtures réelles du dépôt
(``collect_stats_hiveos.txt``, ``detect_hw_hiveos_rig.txt``) et sur des
exemples **publics** : ``miner start`` / ``miner stop`` pilotent le mineur de la
flight sheet (<https://hiveon.com/knowledge-base/guides/linux/>), le watchdog
HiveOS relance le mineur puis redémarre le rig (<https://happymining.fr/blogs/infos/monter-son-kit-de-minage>)
et un ``miner stop`` volontaire ne l'arrête pas — il faut ``wd stop`` ou le mode
maintenance (<https://hiveon.com/forum/t/hashrate-watchdog-issues/11470>).

Aucun test ne contacte un rig réel : les fixtures sont locales et la simulation
installe un ``RUNNER`` qui lève si on le touche.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import hiveos as H
from app import hiveos_remote as HR
from app.hw_parser import parse_detect_hw

FIXTURES = Path(__file__).parent / "fixtures"
PROBE_TEXT = (FIXTURES / "hiveos_probe_rig.txt").read_text()


def probe() -> dict:
    return H.parse_probe(PROBE_TEXT)


def confirm_text() -> str:
    """Section ``HIVE_STAT`` de la collecte HiveOS réelle (fixture du dépôt)."""
    text = (FIXTURES / "collect_stats_hiveos.txt").read_text()
    return text.split("### HIVE_STAT", 1)[1].split("### HIVE_GPU", 1)[0].strip()


# ── Analyse des fichiers HiveOS ────────────────────────────────────────
class TestAnalyse:
    def test_wallet_et_rig(self):
        data = probe()
        assert data["detected"] is True
        assert data["miner"] == "hivefake"
        assert data["miner_bin"] == "/hive/bin/miner"
        assert data["wallet"]["coin"] == "PEARL"
        assert data["wallet"]["miner"] == "hivefake"
        assert data["rig"]["watchdog"] is True
        assert data["rig"]["maintenance"] is False
        assert "RIG_PASSWD" in data["rig"]["keys"]

    def test_gpu_du_rig_et_en_minage(self):
        data = probe()
        assert data["gpu_total"] == 5
        assert data["mining_gpus"] == 5
        assert data["hashrate_hs"] == pytest.approx(328947000000.0)
        assert data["hashrate_source"] == "last_stat.json"
        assert data["coin"] == "PEARL"

    def test_rig_passwd_masque(self):
        data = probe()
        blob = json.dumps(H.mask_probe(data), ensure_ascii=False)
        assert "MotDePasseHiveosFactice" not in blob
        assert data["rig"]["values"]["RIG_PASSWD"] == H.MASKED
        # la clé reste lisible : on prouve sa présence sans sa valeur
        assert "RIG_PASSWD" in data["rig"]["values"]

    def test_passwd_last_stat_reel_masque(self):
        parsed = H.parse_last_stat(confirm_text())
        assert parsed["hashrate_hs"] == pytest.approx(328947000000.0)
        assert parsed["miner"] == "srbminer"
        assert parsed["coin"] == "PEARL"
        assert "S3CR3T-HIVE-PASS" not in json.dumps(parsed, ensure_ascii=False)

    def test_wallet_masque_dans_la_version_stockable(self):
        blob = json.dumps(H.mask_probe(probe()), ensure_ascii=False)
        assert "0xFakeWallet" not in blob
        assert H.mask_probe(probe())["wallet"]["wallet"] == H.MASKED

    def test_detect_hw_hiveos_rig(self):
        detected = parse_detect_hw((FIXTURES / "detect_hw_hiveos_rig.txt").read_text())
        kind = detected.get("os_kind") if isinstance(detected, dict) else getattr(detected, "os_kind", "")
        assert kind == "hiveos"
        machine = SimpleNamespace(os_kind="other", os_kind_detected=kind)
        assert H.is_hiveos(machine) is True

    def test_processus_du_mineur_hiveos_seulement(self):
        data = probe()
        active = H.active_processes(data)
        assert [row["pid"] for row in active] == [4242]
        # un mineur hors HiveOS reste externe : jamais touché (§3d, §3e.5)
        assert all("wam-miner" not in row["args"] for row in active)
        assert H.miner_running(data) is True

    def test_marqueur_colle_au_json_sans_saut_de_ligne(self):
        """``last_stat.json`` sans saut de ligne final : la section GPU reste lue."""
        glued = PROBE_TEXT.replace("}\n### HIVE_GPU", "}### HIVE_GPU")
        assert "}### HIVE_GPU" in glued
        data = H.parse_probe(glued)
        assert data["gpu_total"] == 5
        assert data["hashrate_hs"] == pytest.approx(328947000000.0)

    def test_statut_sans_mineur(self):
        assert H.parse_status("miner is not running")["running"] is False
        assert H.parse_status("Miner is stopped")["running"] is False
        assert H.parse_status("")["running"] is None


# ── Plans d'arrêt et de reprise ────────────────────────────────────────
class TestPlans:
    def test_arret_et_reprise_par_les_commandes_hiveos(self):
        stop = [step.command() for step in H.plan_stop(probe(), ack=True)]
        assert stop[1] == "sudo -n /hive/bin/miner stop"
        start = [step.command() for step in H.plan_start(probe(), ack=True)]
        assert start[0] == "sudo -n /hive/bin/miner start"
        assert any("last_stat.json" in cmd for cmd in start)

    def test_watchdog_actif_refuse_sans_option(self):
        with pytest.raises(H.HiveosError) as exc:
            H.plan_stop(probe(), ack=False)
        assert "watchdog HiveOS actif" in str(exc.value)
        verdict = H.watchdog_verdict(probe(), ack=False)
        assert verdict["level"] == "block"
        assert "wd stop" in verdict["parade"]

    def test_watchdog_actif_avec_option(self):
        stop = [step.command() for step in H.plan_stop(probe(), ack=True)]
        assert stop[0] == "sudo -n /hive/bin/wd stop"
        assert stop[1] == "sudo -n /hive/bin/miner stop"
        start = [step.command() for step in H.plan_start(probe(), ack=True)]
        assert start[-1] == "sudo -n /hive/bin/wd start"

    def test_watchdog_inactif_aucune_parade(self):
        text = PROBE_TEXT.replace("WD_ENABLED=1", "WD_ENABLED=0")
        data = H.parse_probe(text)
        assert data["rig"]["watchdog"] is False
        assert H.watchdog_verdict(data)["level"] == "off"
        assert [step.command() for step in H.plan_stop(data)] == [
            "sudo -n /hive/bin/miner stop",
            "ps -eo pid,args",
        ]

    def test_maintenance_hiveos_leve_le_refus(self):
        text = PROBE_TEXT.replace("MAINTENANCE=0", "MAINTENANCE=1")
        data = H.parse_probe(text)
        verdict = H.watchdog_verdict(data, ack=False)
        assert verdict["level"] == "ok"
        assert "maintenance" in verdict["reason"]

    def test_aucune_modification_de_rig_conf(self):
        for step in H.plan_stop(probe(), ack=True) + H.plan_start(probe(), ack=True):
            joined = step.command()
            assert "rig.conf" not in joined
            assert "wallet.conf" not in joined
            assert ">" not in joined
            assert step.argv[0] in ("sudo", "ps", "cat")


# ── Campagne partielle et mineur externe ───────────────────────────────
class TestRefus:
    def test_campagne_partielle_refusee(self):
        reason = H.partial_refusal([0, 1], 5)
        assert reason.startswith("HiveOS : campagne partielle impossible")
        assert "tous les GPU du mineur HiveOS seraient arrêtés" in reason

    def test_campagne_complete_acceptee(self):
        assert H.partial_refusal([0, 1, 2, 3, 4], 5) is None
        assert H.partial_refusal([0, 1, 2, 3, 4, 4], 5) is None
        assert H.partial_refusal([], 0) is None

    def test_pilotage_montre_le_refus(self):
        devices = [
            SimpleNamespace(kind="gpu", present=True, enabled_for_mining=True, integrated=False, index=i)
            for i in range(5)
        ]
        view = H.pilotage(probe(), devices, ack=False)
        assert view["present"] is True
        assert view["devices"] == [0, 1, 2, 3, 4]
        assert view["pilotable"] is False
        assert "watchdog HiveOS actif" in view["refusal"]
        assert view["commands"] == {"stop": [], "start": []}

    def test_pilotage_avec_option_liste_les_commandes(self):
        devices = [
            SimpleNamespace(kind="gpu", present=True, enabled_for_mining=True, integrated=False, index=i)
            for i in range(5)
        ]
        view = H.pilotage(probe(), devices, ack=True)
        assert view["pilotable"] is True
        assert view["commands"]["stop"][0] == "sudo -n /hive/bin/wd stop"
        assert view["commands"]["start"][0] == "sudo -n /hive/bin/miner start"

    def test_gpu_integres_et_desactives_exclus(self):
        devices = [
            SimpleNamespace(kind="gpu", present=True, enabled_for_mining=True, integrated=False, index=0),
            SimpleNamespace(kind="gpu", present=True, enabled_for_mining=False, integrated=False, index=1),
            SimpleNamespace(kind="gpu", present=True, enabled_for_mining=True, integrated=True, index=2),
            SimpleNamespace(kind="cpu", present=True, enabled_for_mining=True, integrated=False, index=None),
        ]
        assert H.gpu_indices(devices) == [0]

    def test_mineur_externe_jamais_dans_le_plan(self):
        text = PROBE_TEXT.replace("MINER_STATUS\nminer is running", "MINER_STATUS\nminer is stopped").replace(
            "WD_ENABLED=1", "WD_ENABLED=0"
        )
        data = H.parse_probe(text)
        commands = [step.command() for step in H.plan_stop(data)] + [
            step.command() for step in H.plan_start(data)
        ]
        assert not any("wam" in cmd or "srbminer" in cmd for cmd in commands)
        # le script wam hors HiveOS reste dans `ps` mais n'est jamais visé
        assert [row["pid"] for row in H.active_processes(data)] == [4242]
        assert all("wam-miner" not in row["args"] for row in H.active_processes(data))


# ── Simulation : zéro exécution ────────────────────────────────────────
class TestSimulation:
    async def test_arret_simule_zero_appel(self, monkeypatch):
        calls: list[object] = []

        async def boom(*args, **kwargs):
            calls.append(args)
            raise AssertionError("appel SSH en mode simulation")

        monkeypatch.setattr(HR, "RUNNER", boom)
        result = await HR.stop(
            SimpleNamespace(), probe(), ack_watchdog=True, simulated=True
        )
        assert calls == []
        assert result["simulated"] is True
        assert result["commands"][0] == "sudo -n /hive/bin/wd stop"
        assert "sudo -n /hive/bin/miner stop" in result["commands"]

    async def test_reprise_simulee_zero_appel(self, monkeypatch):
        calls: list[object] = []

        async def boom(*args, **kwargs):
            calls.append(args)
            raise AssertionError("appel SSH en mode simulation")

        monkeypatch.setattr(HR, "RUNNER", boom)
        result = await HR.resume(SimpleNamespace(), probe(), ack_watchdog=True, simulated=True)
        assert calls == []
        assert result["simulated"] is True
        assert "sudo -n /hive/bin/miner start" in result["commands"]

    async def test_sonde_simulee_zero_appel(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise AssertionError("appel SSH en mode simulation")

        monkeypatch.setattr(HR, "RUNNER", boom)
        result = await HR.probe(SimpleNamespace(), simulated=True)
        assert result["simulated"] is True
        assert result["detected"] is False

    def test_sonde_lecture_seule(self):
        from app.ssh_client import WHITELISTED_COMMANDS

        command = WHITELISTED_COMMANDS["hiveos_probe"]
        for forbidden in ("miner start", "miner stop", "wd stop", "wd start", "rm ", "> ", "tee "):
            assert forbidden not in command
        for expected in ("/hive-config/wallet.conf", "/hive-config/rig.conf", "/run/hive/cur_miner",
                         "/run/hive/miners", "/hive/miners/custom", "/run/hive/last_stat.json",
                         "miner status", "wd status"):
            assert expected in command
