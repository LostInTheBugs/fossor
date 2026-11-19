"""Intégration des finitions (5e essai) : SSH coupé au démarrage, zéro clé laissée.

Réutilise le harnais de ``tests/test_pve_campaigns_integration.py`` : nœud
Proxmox factice (``qm``/``pvesh``/``pvesm`` bouchonnés) et **vraie cible SSH**
jetable. Les deux garanties ne se prouvent que contre une vraie connexion :
un SSH qui tombe doit être rejoué, et la clé privée de rebond doit disparaître
de l'hôte à la destruction de la dernière VM.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from pathlib import Path

import pytest

from tests.test_pve_campaigns_integration import (  # noqa: F401 — fixture réutilisée
    SSH_HOST,
    _db,
    _guest,
    _journal,
    _seed,
    harness,
)

pytestmark = pytest.mark.skipif(
    not SSH_HOST, reason="cible SSH jetable absente (FOSSOR_TEST_SSH_HOST)"
)


async def _stop_fake_miner() -> None:
    """Ménage de la cible : aucun mineur ni PID ne survit à un test."""
    await _guest(
        'sudo -n pkill -f "[m]ineur --wallet" 2>/dev/null; '
        "sudo -n rm -f /opt/fossor/run/*.pid; echo CLEAN_OK"
    )


class TestSshPerduAuDemarrageDuMineur:
    """SSH tombe une fois au démarrage : cloud-init attendu, essai suivant gagne."""

    async def test_ssh_coupe_une_fois_puis_mineur_lance(self, harness, monkeypatch):
        from app import binaries as B
        from app import campaign_runner as RUN
        from app import pve_remote as PR
        from app.models import CampaignEvent, MinerProfile

        db = _db()
        try:
            campaign, host, model = _seed(db, harness)
            profile = (
                db.query(MinerProfile)
                .filter(MinerProfile.id == campaign.profile_id)
                .one()
            )
            payload = (
                b"#!/bin/sh\n"
                b'case "$1" in --version|--help) echo "faux-mineur 1.0"; exit 0;; esac\n'
                b"while true; do sleep 5; done\n"
            )
            path = B.binary_dir() / f"{profile.uuid}.elf"
            path.write_bytes(payload)
            profile.binary_source = "fossor"
            profile.binary_kind = "elf"
            profile.binary_stored = str(path)
            profile.sha256 = hashlib.sha256(payload).hexdigest()
            profile.binary_size = len(payload)
            db.commit()

            inner = harness["runner"]
            cut = {"done": False}

            async def flaky(host_obj, command, stdin=None, timeout=60):
                # La première commande de la VM qui suit le provisionnement :
                # c'est le pas « fin de cloud-init » ajouté avant le mineur.
                if not cut["done"] and "cloud-init status" in command:
                    cut["done"] = True
                    return {
                        "ok": False,
                        "status": 255,
                        "stdout": "",
                        "stderr": "Connection lost",
                    }
                return await inner(host_obj, command, stdin=stdin, timeout=timeout)

            monkeypatch.setattr(PR, "RUNNER", flaky)
            monkeypatch.setattr(PR, "SSH_RETRY_DELAYS", (0.5, 0.5))

            result = await RUN.tick(db, now=RUN.C.now_utc(), simulated=False)
            db.refresh(campaign)
            print("\n### tick avec SSH coupé :", json.dumps(result, default=str))
            _journal(db, campaign.id)

            events = (
                db.query(CampaignEvent)
                .filter(CampaignEvent.campaign_id == campaign.id)
                .order_by(CampaignEvent.id.asc())
                .all()
            )
            kinds = [row.kind for row in events]
            retries = [row for row in events if row.kind == "vm-ssh-retry"]
            started = [row for row in events if row.kind == "campaign-started"]
            print("essais rejoués :", [(r.message, r.result) for r in retries])
            print("campaign-started :", [(r.message, r.result) for r in started])

            assert cut["done"] is True
            assert retries, kinds
            assert "essai 1/3" in (retries[0].message or "")
            assert "Connection lost" in (retries[0].message or "")
            assert started and started[0].result == "ok", kinds
            assert campaign.status == "running", campaign.note

            alive = await _guest(
                'ps -eo args= | grep -F "/opt/fossor/miners" | grep -v grep | head -3'
            )
            print("processus du mineur dans la cible :", alive.strip() or "(aucun)")
            assert "mineur" in alive
        finally:
            db.close()
            await _stop_fake_miner()


class TestAucuneClePriveeSurLhote:
    """Après destruction de la dernière VM, la clé de rebond a quitté l'hôte."""

    async def test_cle_retiree_a_la_destruction(self, harness, monkeypatch):
        from app import campaign_runner as RUN
        from app.ai_config import set_setting
        from app.models import SpecVm
        from app.routers import vms as VR

        db = _db()
        try:
            campaign, host, model = _seed(db, harness)
            await RUN.tick(db, now=RUN.C.now_utc(), simulated=False)
            db.refresh(campaign)
            _journal(db, campaign.id)

            key = Path(harness["P"].KEY_PATH)
            known = Path(harness["P"].KNOWN_HOSTS)
            assert key.exists(), "clé de rebond absente pendant la campagne"
            assert known.exists()
            print(f"\n### clé de rebond : {key} ({oct(key.stat().st_mode & 0o777)})")
            assert (key.stat().st_mode & 0o777) == 0o400
            assert key.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY")

            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            parent = shlex.quote(str(Path(vm.backup_path).parent))
            target = shlex.quote(vm.backup_path)
            made = await _guest(
                f"sudo -n mkdir -p {parent} && "
                f"head -c 4096 /dev/urandom | sudo -n tee {target} >/dev/null && echo BACKUP_OK"
            )
            assert "BACKUP_OK" in made, made

            set_setting(db, "simulation_mode", False)
            db.commit()
            verdict = await VR.check_backup(vm.id, db=db, user="test")
            print("verdict sauvegarde :", json.dumps(verdict))
            assert verdict.get("verified") is True, verdict

            destroyed = await VR.destroy_vm(vm.id, {"confirm": "DETRUIRE"}, db=db, user="test")
            print("destruction :", json.dumps(destroyed, default=str))
            assert destroyed.get("destroyed") is True
            assert destroyed.get("last_vm") is True

            print("contenu du dossier de travail après destruction :",
                  sorted(p.name for p in key.parent.iterdir()) if key.parent.exists() else "(absent)")
            assert not key.exists(), "la clé privée de rebond est restée sur l'hôte"
            assert not known.exists(), "le known_hosts dédié est resté sur l'hôte"

            vm_live = await _guest("ls /opt/fossor/run 2>/dev/null; echo FIN")
            assert "mineur" not in vm_live
        finally:
            db.close()
            await _stop_fake_miner()
