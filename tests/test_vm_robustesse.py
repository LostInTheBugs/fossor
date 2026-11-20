"""Robustesse des campagnes en VM — correctifs du 4e essai.

Chaque test appelle le code de production : détection du dépôt par octets
magiques, provisionnement d'un ELF brut, sonde ``--version``/GLIBC, contrôle de
vivacité, ``{worker}``/``{threads}``, contrôle et destruction après
provisionnement échoué, lecture des deux formats de fichiers de sommes.
"""

from __future__ import annotations

import asyncio
import json
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import binaries as B
from app import miners as M
from app import pve_vm as P
from app import spec_vm as V
from app.routers import vms as VR

ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 96
SHA = "b" * 64


def _profile(**over):
    data = {
        "name": "Profil VM",
        "coin": "NNN",
        "wallet": "demo1pna00000000000000000000000000000000000",
        "pool": "solo",
        "software": "custom",
        "binary_url": "https://example.invalid/miner.tar.gz",
        "sha256": SHA,
        "binary_path": "miner",
        "version": "1.0.0",
        "cmdline": "/opt/fossor/miners/custom/1.0.0/miner --wallet {wallet} --worker {worker} --threads {threads}",
        "api_port": 0,
        "api_kind": "",
        "binary_source": "fossor",
        "wallet_mode": "file",
        "execution_mode": "vm",
        "threads": 0,
    }
    data.update(over)
    return data


# ── 1. dépôt de binaire ───────────────────────────────────────────────
def test_depot_detecte_le_type_par_les_octets():
    assert B.detect_kind(ELF) == "elf"
    assert B.detect_kind(b"\x1f\x8b\x08\x00demo") == "tar.gz"
    assert B.detect_kind(b"\xfd7zXZ\x00demo") == "tar.xz"
    assert B.detect_kind(b"PK\x03\x04demo") == "zip"
    with pytest.raises(B.BinaryError) as exc:
        B.detect_kind(b"#!/bin/sh\necho hello\n")
    assert "format non reconnu" in str(exc.value)


def test_elf_brut_stocke_tel_quel_et_verifie():
    uid = str(uuid_lib.uuid4())
    saved = B.save_binary(uid, ELF)
    assert saved["kind"] == "elf"
    assert Path(saved["path"]).read_bytes() == ELF
    assert saved["path"].endswith(".elf")
    profile = {"uuid": uid, "sha256": saved["sha256"], "binary_kind": "elf",
               "binary_stored": saved["path"]}
    path, digest = B.verified_path(profile)
    assert path.read_bytes() == ELF
    assert digest == saved["sha256"]


def test_provision_elf_sans_extraction():
    profile = M.validate_profile(_profile())
    profile.update(binary_kind="elf", binary_stored="/tmp/x.elf")
    model = {"node_port": 19333, "wallet_backup_path": "/home/specminer/data/wallet.dat"}
    steps = V.provision_plan(model, profile, "192.0.2.5", coin="NNN", binary_pushed=True)
    titles = [step.title for step in steps]
    assert "binaire ELF installé tel quel (aucune extraction)" in titles
    assert "extraction du mineur" not in titles
    elf_step = next(s for s in steps if s.title.startswith("binaire ELF installé"))
    assert "miner.elf" in elf_step.command()


# ── 3. compatibilité du binaire ───────────────────────────────────────
def test_glibc_insuffisante_message_exact():
    text = (
        "GLIBC:2.36\nEXIT:127\n"
        "/opt/fossor/miners/custom/1.0.0/miner: /lib/x86_64-linux-gnu/libc.so.6: "
        "version `GLIBC_2.38' not found (required by /opt/fossor/miners/custom/1.0.0/miner)\n"
    )
    verdict = V.parse_binary_probe(text)
    assert verdict["ok"] is False
    assert verdict["message"] == (
        "le binaire exige GLIBC_2.38, la VM fournit 2.36 : choisissez une image plus récente"
    )


def test_binaire_present_et_compatible():
    assert V.parse_binary_probe("GLIBC:2.39\nEXIT:0\nSRBMiner-MULTI 2.9.5\n")["ok"] is True


# ── 4. vivacité ───────────────────────────────────────────────────────
def test_vivacite_lit_le_processus_du_binaire():
    profile = M.validate_profile(_profile())
    profile.update(binary_kind="elf")
    path = M.binary_path(profile)
    assert V.parse_alive(f" 4242 {path} --wallet demo\n", profile)["alive"] is True
    assert V.parse_alive(" 1 /sbin/init\n", profile)["alive"] is False


# ── 5. worker et threads ──────────────────────────────────────────────
def test_worker_du_profil_prime_sur_le_nom_de_machine():
    profile = M.validate_profile(_profile(worker="rig-atelier"))
    values = M.build_values(profile, worker="machine-x", threads=2)
    assert values["worker"] == "rig-atelier"
    values = M.build_values(M.validate_profile(_profile()), worker="machine-x", threads=2)
    assert values["worker"] == "machine-x"


def test_threads_du_profil_prime_sur_les_vcpu():
    profile = M.validate_profile(_profile(threads=6))
    assert M.build_values(profile, threads=2)["threads"] == "6"
    profile = M.validate_profile(_profile())
    assert M.build_values(profile, threads=8)["threads"] == "8"


# ── 7. images Ubuntu et fichiers de sommes ────────────────────────────
UBUNTU = "a" * 64
DEBIAN = "c" * 128


def test_lit_un_sha256sums_ubuntu():
    text = f"{'0' * 64}  autre.img\n{UBUNTU} *noble-server-cloudimg-amd64.img\n"
    assert V.parse_checksum_file(text, "noble-server-cloudimg-amd64.img") == UBUNTU
    assert P.parse_checksum_file(text, "noble-server-cloudimg-amd64.img") == UBUNTU


def test_lit_un_sha512sums_debian():
    text = f"{DEBIAN}  debian-12-genericcloud-amd64.qcow2\n"
    assert V.parse_checksum_file(text, "debian-12-genericcloud-amd64.qcow2") == DEBIAN


def test_modele_proxmox_ubuntu_avec_fichier_de_sommes():
    model = P.validate_pve_model({
        "host_id": 1,
        "node": "pve",
        "disk_storage": "local-lvm",
        "ci_storage": "local",
        "bridge": "vmbr0",
        "image_url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "checksum_url": "https://cloud-images.ubuntu.com/noble/current/SHA256SUMS",
        "checksum": "",
        "checksum_algo": "sha256",
        "vmid_min": 9900,
        "vmid_max": 9909,
    })
    assert model["image_name"] == "noble-server-cloudimg-amd64.img"
    assert model["checksum_url"].endswith("SHA256SUMS")
    plan = P.image_plan(model)
    assert "SHA256SUMS" in plan[1].command()


def test_modele_libvirt_ubuntu_avec_fichier_de_sommes():
    model = V.validate_vm_model({
        "host_id": 1,
        "base_image_url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "checksum_url": "https://cloud-images.ubuntu.com/noble/current/SHA256SUMS",
        "base_image_sha256": "",
    })
    assert model["base_image_name"] == "noble-server-cloudimg-amd64.img"
    assert "SHA256SUMS" in V.image_plan(model)[1].command()


# ── 2. backup-check + destroy sur provisionnement échoué ──────────────
class _FakeBackend:
    kind = "libvirt"

    def __init__(self):
        self.destroyed = None

    async def check_backup(self, machine, model, row, path, *, now_epoch,
                           max_age_days, simulated):
        return {"verified": False, "reason": "fichier de sauvegarde absent",
                "present": False, "size": 0, "mtime": 0}

    async def destroy(self, machine, model, row, *, backup, now_epoch,
                      max_age_days, simulated):
        self.destroyed = {"name": row.name, "backup": backup}
        return {"destroyed": True}


def _row(db, *, state="failed", wallet=""):
    from app.models import Campaign, Machine, MinerProfile, SpecVm

    machine = Machine(
        uuid=str(uuid_lib.uuid4()), name="hote-test", host="192.0.2.1",
        ssh_port=22, ssh_user="fossor", auth_type="fossor_key",
        os_kind="debian", enabled=True, usage="cpu", vm_host=True,
    )
    db.add(machine)
    db.flush()
    profile = MinerProfile(
        uuid=str(uuid_lib.uuid4()), name="profil-test", coin="NNN",
        wallet="demo1pna00000000000000000000000000000000000", pool="solo",
        software="custom", binary_url="https://example.invalid/x", sha256=SHA,
        binary_path="miner", version="1.0.0",
        cmdline="/opt/fossor/miners/custom/1.0.0/miner --wallet {wallet}", api_port=0, enabled=True,
        wallet_mode="file", execution_mode="vm", threads=0,
    )
    db.add(profile)
    db.flush()
    campaign = Campaign(
        uuid=str(uuid_lib.uuid4()), coin="NNN", profile_id=profile.id, status="failed",
        start_at=datetime.now(timezone.utc).isoformat(), execution_mode="vm",
        origin="manual",
    )
    db.add(campaign)
    db.flush()
    row = SpecVm(
        uuid=str(uuid_lib.uuid4()), campaign_id=campaign.id,
        host_machine_id=machine.id, name="fossor-c9-nnn-abcdef", mode="vm",
        hypervisor="libvirt", vcpus=2, memory_mb=2048, disk_gb=20,
        node_port=19333, state=state, wallet_address=wallet,
        backup_path="/home/specminer/data/wallet.dat",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, machine


def test_backup_check_puis_destroy_apres_provisionnement_echoue(monkeypatch):
    from app.database import SessionLocal

    db = SessionLocal()
    fake = _FakeBackend()
    try:
        row, machine = _row(db)
        monkeypatch.setattr(VR, "_vm_backend", lambda _db, _row: (machine, fake, {}))
        monkeypatch.setattr("app.pilotage.simulated_for", lambda _db, _machine: False)
        monkeypatch.setattr(VR, "backup_max_age_days", lambda _db: 7)

        verdict = asyncio.run(VR.check_backup(row.id, db=db, user="test"))
        assert verdict["verified"] is True
        assert verdict["reason"] == "aucun wallet dans la VM"
        assert verdict["no_wallet"] is True

        db.refresh(row)
        assert json.loads(row.backup_state)["verified"] is True
        result = asyncio.run(VR.destroy_vm(
            row.id, {"confirm": "DETRUIRE"}, db=db, user="test"))
        assert result["destroyed"] is True
        assert fake.destroyed["name"] == row.name
        assert fake.destroyed["backup"]["reason"] == "aucun wallet dans la VM"
        db.refresh(row)
        assert row.state == "destroyed"
    finally:
        db.close()


def test_backup_check_vm_avec_wallet_ne_masque_pas_un_absent(monkeypatch):
    from app.database import SessionLocal

    db = SessionLocal()
    fake = _FakeBackend()
    try:
        row, machine = _row(db, state="running", wallet="demo1pna00000000000000000000000000000000000")
        monkeypatch.setattr(VR, "_vm_backend", lambda _db, _row: (machine, fake, {}))
        monkeypatch.setattr("app.pilotage.simulated_for", lambda _db, _machine: False)
        monkeypatch.setattr(VR, "backup_max_age_days", lambda _db: 7)
        verdict = asyncio.run(VR.check_backup(row.id, db=db, user="test"))
        assert verdict["verified"] is False
        assert verdict["reason"] == "fichier de sauvegarde absent"
    finally:
        db.close()


# ── 6. ligne de campagne retrouvable après rechargement ───────────────
def test_ligne_campagne_par_id_et_arret_au_clic():
    html = Path(__file__).resolve().parents[1] / "static" / "campaigns.html"
    text = html.read_text()
    assert 'data-campaign-id="' in text
    assert 'closest("tr[data-campaign-id]")' in text
    assert 'data-act="stop"' in text
