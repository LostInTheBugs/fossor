"""Finitions des campagnes en VM (5e essai) — clé de rebond, SSH, GLIBC, fiche VM.

Chaque test appelle le code de production : plans réellement construits,
runner réellement exécuté (bouchon), message réellement rendu.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app import pve_remote as PR
from app import pve_vm as P
from app import spec_vm as V

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_journal():
    PR.drain_retries()
    yield
    PR.drain_retries()


# ── 1. Clé de rebond : dossier de travail, retirée avec la dernière VM ──────
def test_cle_de_rebond_hors_de_root_ssh():
    """La clé privée vit dans le dossier du pilote, plus dans /root/.ssh."""
    assert P.KEY_PATH == f"{P.PVE_TMP_DIR}/fossor-vm"
    assert P.KNOWN_HOSTS == f"{P.PVE_TMP_DIR}/known_hosts.fossor-vm"
    assert "/root/.ssh" not in P.KEY_PATH
    assert "/root/.ssh" not in P.KNOWN_HOSTS


def test_installation_de_la_cle_cree_le_dossier_avant_d_ecrire():
    """Sans le dossier, ``tee`` échoue : le plan doit le créer d'abord (0700)."""
    steps = PR.install_key_plan("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n")
    assert steps[0].argv == ("install", "-d", "-m", "0700", P.PVE_TMP_DIR, P.IMAGE_DIR)
    assert steps[1].argv == ("tee", P.KEY_PATH)
    assert steps[1].stdin.endswith("\n")
    assert steps[2].argv == ("chmod", "0400", P.KEY_PATH)
    assert steps[3].argv == ("touch", P.KNOWN_HOSTS)
    assert all("/root/.ssh" not in " ".join(step.argv) for step in steps)


def test_cleanup_image_conserve_la_cle_de_rebond():
    """Cache désactivé après création : l'image part, la clé privée reste."""
    model = {"image_name": "debian-12-genericcloud-amd64.qcow2"}
    image = P.image_path(model)
    step = P.image_cleanup_plan(model)[0]
    command = " ".join(step.argv)
    assert image in command and P.PUBKEY_PATH in command
    assert P.KEY_PATH not in command
    assert f"rm -rf {P.PVE_TMP_DIR}" not in command


def test_derniere_vm_detruite_retire_la_cle_et_le_known_hosts():
    """Aucune VM Fossor restante : clé privée, known_hosts et dossier partent."""
    steps = P.last_vm_cleanup_plan([], keep_cache=False)
    commands = " ".join(" ".join(step.argv) for step in steps)
    assert P.KEY_PATH in commands and P.KNOWN_HOSTS in commands
    assert f"rm -rf {P.PVE_TMP_DIR}" in commands


def test_derniere_vm_detruite_avec_cache_garde_le_dossier_mais_pas_la_cle():
    steps = P.last_vm_cleanup_plan([], keep_cache=True)
    commands = " ".join(" ".join(step.argv) for step in steps)
    assert P.KEY_PATH in commands
    assert f"rm -rf {P.PVE_TMP_DIR}" not in commands


def test_une_vm_subsiste_la_cle_reste():
    assert P.last_vm_cleanup_plan(["fossor-c7-nnn-a1b2c3"], keep_cache=False) == []


# ── 2. SSH : cloud-init attendu, trois essais, journal des essais ───────────
def test_cloud_init_est_attendu_avant_le_lancement_du_mineur(monkeypatch):
    calls: list[str] = []

    async def runner(host, command, stdin=None, timeout=60):
        calls.append(command)
        return {"ok": True, "status": 0, "stdout": "1234\n", "stderr": ""}

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(PR, "SSH_RETRY_DELAYS", (0.0, 0.0))
    profile = {"name": "p", "software": "custom", "version": "1.0.0", "binary_path": "mineur"}
    asyncio.run(PR.start_miner(object(), profile, ["--wallet", "x"], "192.0.2.10", simulated=False))

    assert len(calls) == 2
    assert "cloud-init status --wait" in calls[0]
    assert calls[0].startswith("ssh ")
    assert "mineur" not in calls[1] and "cloud-init" not in calls[1]
    assert "setsid" in calls[1] and "--wallet x" in calls[1]


def test_ssh_coupe_deux_fois_est_rejoue_et_journalise(monkeypatch):
    calls: list[str] = []

    async def runner(host, command, stdin=None, timeout=60):
        calls.append(command)
        if len(calls) <= 2:
            return {"ok": False, "status": 255, "stdout": "", "stderr": "Connection lost"}
        return {"ok": True, "status": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(PR, "SSH_RETRY_DELAYS", (0.0, 0.0))
    step = P.vm_step("pas idempotent", "192.0.2.10", ["true"])

    result = asyncio.run(PR._run_guest(object(), step))

    assert result["stdout"] == "ok"
    assert len(calls) == 3
    journal = PR.drain_retries()
    assert [item["attempt"] for item in journal] == [1, 2]
    assert all(item["attempts"] == 3 for item in journal)
    assert all("Connection lost" in item["error"] for item in journal)


def test_ssh_encore_coupe_apres_trois_essais_echoue(monkeypatch):
    calls: list[str] = []

    async def runner(host, command, stdin=None, timeout=60):
        calls.append(command)
        return {"ok": False, "status": 255, "stdout": "", "stderr": "Connection refused"}

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(PR, "SSH_RETRY_DELAYS", (0.0, 0.0))
    step = P.vm_step("pas idempotent", "192.0.2.10", ["true"])

    with pytest.raises(PR.PveRemoteError):
        asyncio.run(PR._run_guest(object(), step))

    assert len(calls) == 3
    assert len(PR.drain_retries()) == 2


def test_refus_definitif_jamais_rejoue(monkeypatch):
    """« Permission denied » n'est pas un incident de transport : un seul essai."""
    calls: list[str] = []

    async def runner(host, command, stdin=None, timeout=60):
        calls.append(command)
        return {"ok": False, "status": 255, "stdout": "", "stderr": "Permission denied (publickey)."}

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(PR, "SSH_RETRY_DELAYS", (0.0, 0.0))
    step = P.vm_step("pas idempotent", "192.0.2.10", ["true"])

    with pytest.raises(PR.PveRemoteError):
        asyncio.run(PR._run_guest(object(), step))

    assert len(calls) == 1
    assert PR.drain_retries() == []


def test_un_pas_qm_n_est_pas_rejoue(monkeypatch):
    calls: list[str] = []

    async def runner(host, command, stdin=None, timeout=60):
        calls.append(command)
        return {"ok": False, "status": 255, "stdout": "", "stderr": "Connection lost"}

    monkeypatch.setattr(PR, "RUNNER", runner)
    step = P.VmStep("qm start", ("qm", "start", "9901"))
    with pytest.raises(PR.PveRemoteError):
        asyncio.run(PR._run_guest(object(), step))
    assert len(calls) == 1


# ── 3. GLIBC : conseil « image plus récente » seulement si c'est vrai ──────
LOADER = "/lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found (required by /opt/fossor/miners/custom/1.0.0/mineur)"


def test_vm_plus_recente_rend_la_sortie_du_chargeur():
    verdict = V.parse_binary_probe(f"{LOADER}\nGLIBC:2.39\nEXIT:127\n")
    assert verdict["ok"] is False
    assert verdict["advise_newer_image"] is False
    assert "image plus récente" not in verdict["message"]
    assert "le binaire exige GLIBC_2.38" in verdict["message"]
    assert "GLIBC_2.38' not found" in verdict["message"]
    assert verdict["glibc_provided"] == "2.39"


def test_vm_plus_ancienne_conseille_une_image_recente():
    verdict = V.parse_binary_probe(f"{LOADER}\nGLIBC:2.31\nEXIT:127\n")
    assert verdict["advise_newer_image"] is True
    assert "choisissez une image plus récente" in verdict["message"]


def test_vm_de_meme_version_ne_conseille_rien():
    verdict = V.parse_binary_probe(f"{LOADER}\nGLIBC:2.38\nEXIT:127\n")
    assert verdict["advise_newer_image"] is False
    assert "image plus récente" not in verdict["message"]
    assert "GLIBC_2.38' not found" in verdict["message"]


def test_version_inconnue_ne_conseille_rien():
    verdict = V.parse_binary_probe(f"{LOADER}\nGLIBC:\nEXIT:127\n")
    assert verdict["advise_newer_image"] is False


# ── 4. Fiche VM : routes réelles et rafraîchissement ──────────────────────
def test_fiche_vm_appelle_backup_check_et_rafraichit_la_liste():
    html = (ROOT / "static" / "settings.html").read_text()
    assert "'backup-check':verb" in html
    assert "'/api/vms/'+id+'/'+route" in html
    # La liste se charge avec l'écran, même si les réglages échouent.
    assert "async function loadSpec(){\n  loadVmList();" in html
    # Et se rafraîchit après l'action, y compris en échec.
    assert "const d=await r.json();\n  await loadVmList();" in html


def test_le_test_e2e_electricite_attend_le_contenu():
    text = (ROOT / "tests" / "e2e" / "test_z_power.py").read_text()
    assert "textContent.includes('€/kWh')" in text
    assert "wait_for_function" in text
