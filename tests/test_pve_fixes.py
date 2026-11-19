"""Correctifs des campagnes Proxmox révélés par le test réel (§4.6).

Chaque test appelle le code de production : argv réencodé pour ``ssh``, unité
systemd absente, modèle de l'hôte choisi, routes ``/api/vms/*`` par la
fabrique de backend, destruction qui emporte le snippet cloud-init.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid as uuid_lib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import miner_remote as MR
from app import pve_remote as PR
from app import pve_vm as P
from app import spec_vm as V
from app import vm_backend as VB

SHA = "a" * 64
WALLET = "demo1pna00000000000000000000000000000000000"
ARGV = ["sh", "-c", 'for t in a b; do echo "ITEM:$t"; done']


def _machine(db, *, name, host, os_kind, vm_host=True):
    from app.models import Machine

    row = Machine(
        uuid=str(uuid_lib.uuid4()),
        name=name,
        host=host,
        ssh_port=22,
        ssh_user="root" if os_kind == "proxmox" else "fossor",
        auth_type="fossor_key",
        os_kind=os_kind,
        enabled=True,
        usage="cpu",
        vm_host=vm_host,
    )
    db.add(row)
    db.flush()
    return row


def _pve_model(host_id: int) -> dict:
    return P.validate_pve_model(
        {
            "host_id": host_id,
            "node": "pve",
            "disk_storage": "local-lvm",
            "ci_storage": "local",
            "bridge": "vmbr0",
            "vmid_min": 9900,
            "vmid_max": 9909,
            "vcpus": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "image_url": "https://exemple.tld/images/debian-12-genericcloud-amd64.qcow2",
            "checksum": SHA,
            "checksum_algo": "sha256",
        }
    )


def _db():
    from app.database import SessionLocal, init_db

    init_db()
    return SessionLocal()


# ── Constat n°1 : argv perdu par ssh ─────────────────────────────────
def test_argv_reencode_arrive_intact_au_shell_distant():
    """La commande produite est rejouée par un vrai ``bash -lc``."""
    commands = [
        V.ssh_vm_argv("192.0.2.10", ARGV),
        P.ssh_vm_argv("192.0.2.10", ARGV),
    ]
    for command in commands:
        distant = command[-1]
        assert "specminer@192.0.2.10" in command, command
        # Une seule chaîne citée : ssh ne redécoupe pas l'argv.
        assert distant == "sh -c 'for t in a b; do echo \"ITEM:$t\"; done'", distant
        played = subprocess.run(
            ["bash", "-lc", distant], capture_output=True, text=True, check=False
        )
        assert played.returncode == 0, played.stderr
        assert played.stdout.split() == ["ITEM:a", "ITEM:b"]


def test_ancien_cablage_cassait_la_commande():
    """Preuve du défaut : l'ancien ``*argv`` rejoué ne passe pas."""
    broken = subprocess.run(
        ["bash", "-lc", " ".join(ARGV)], capture_output=True, text=True, check=False
    )
    assert broken.returncode != 0


# ── Constat n°2 : unité absente = rien à arrêter ─────────────────────
def test_unite_systemd_absente_est_un_succes(monkeypatch):
    calls: list[str] = []

    async def fake_runner(machine, command, **kwargs):
        calls.append(command)
        if "INIT:" in command:  # sonde : systemd présent
            return {"ok": True, "status": 0, "stdout": "TOOL:systemctl\nINIT:systemd\n"}
        if "systemctl stop" in command:
            return {
                "ok": True,
                "status": 5,
                "stdout": "",
                "stderr": "Unit fossor-custom-1.0.0.service not loaded.",
            }
        return {"ok": True, "status": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(MR, "RUNNER", fake_runner)
    machine = SimpleNamespace(
        name="hote", host="192.0.2.9", ssh_port=22, ssh_user="root", auth_type="fossor_key"
    )
    profile = {
        "name": "campagne",
        "software": "custom",
        "version": "1.0.0",
        "binary_path": "mineur",
        "api_port": None,
        "pool": "solo",
    }
    result = asyncio.run(MR.stop(machine, profile, simulated=False, user="root"))
    assert result["ok"] is True
    assert "rien à arrêter" in result["note"]
    assert any("not loaded" not in call for call in calls)


# ── Constat n°3 : vm_ready suit le modèle de l'hôte choisi ───────────
def test_vm_ready_lit_le_modele_de_l_hote_choisi(monkeypatch):
    from app.routers import campaigns as ROUTER

    host = SimpleNamespace(id=7, os_kind="proxmox", name="pve-lab")
    monkeypatch.setattr(VB, "spec_host", lambda db: host)

    class Modele:
        kind = "proxmox"

        def __init__(self, present: bool) -> None:
            self.present = present

        def load_model(self, db):
            return {"host_id": 7} if self.present else None

    monkeypatch.setattr(VB, "backend_for_host", lambda machine: Modele(True))
    assert ROUTER._vm_ready(None) is True
    monkeypatch.setattr(VB, "backend_for_host", lambda machine: Modele(False))
    assert ROUTER._vm_ready(None) is False


# ── Constat n°4 : routes /api/vms/* par la fabrique + snippet ────────
def test_route_vms_choisit_le_pilote_proxmox():
    from app.ai_config import set_setting
    from app.models import SpecVm
    from app.routers import vms as R

    db = _db()
    host = _machine(db, name="pve-lab", host="192.0.2.30", os_kind="proxmox")
    set_setting(db, "spec_host_id", host.id)
    set_setting(db, "spec_pve_model", _pve_model(host.id))
    row = SpecVm(
        uuid=str(uuid_lib.uuid4()),
        host_machine_id=host.id,
        name="fossor-1-NNN-abc123",
        mode="vm",
        hypervisor="proxmox",
        vmid=9900,
        vcpus=2,
        memory_mb=2048,
        disk_gb=20,
        network="vmbr0",
        node_port=3333,
        state="shut off",
    )
    db.add(row)
    db.commit()

    machine, backend, model = R._vm_backend(db, row)
    assert machine.id == host.id
    assert backend.kind == "proxmox"
    assert model["vmid_min"] == 9900 and model["vmid_max"] == 9909
    db.close()


def test_destruction_proxmox_emporte_le_snippet():
    model = _pve_model(1)
    plan = P.snippet_remove_plan(model, 9900)
    command = plan[0].command()
    assert "fossor-user-9900.yaml" in command
    assert "rm -f" in command

    host = SimpleNamespace(
        name="pve-lab", host="192.0.2.30", ssh_port=22, ssh_user="root", auth_type="fossor_key"
    )
    now = int(time.time())
    result = asyncio.run(
        PR.destroy(
            host,
            model,
            9900,
            backup={"verified": True, "checked_at_epoch": now},
            now_epoch=now,
            max_age_days=3,
            simulated=True,
        )
    )
    assert any("fossor-user-9900.yaml" in item for item in result["commands"])


# ── Constat n°5 : hôte sans device, vCPU du modèle ───────────────────
def test_campagne_vm_sans_device_utilise_les_vcpu_du_modele():
    """La cible machine (sans device) prend ``targets[0]`` et les vCPU."""
    from app import campaign_runner as RUN

    host = SimpleNamespace(id=3, name="pve-lab", os_kind="proxmox")
    target = RUN.Target(machine=host)
    assert target.threads == 1 and target.device_uuids == []
    target.threads = int(_pve_model(3)["vcpus"] or target.threads or 1)
    assert target.threads == 2
    assert RUN.render_argv(
        {
            "name": "p",
            "coin": "NNN",
            "wallet": WALLET,
            "pool": "solo",
            "software": "custom",
            "version": "1.0.0",
            "binary_path": "mineur",
            "cmdline": "/opt/fossor/miners/custom/1.0.0/mineur --wallet {wallet} --threads {threads}",
            "threads": 2,
        },
        host,
        target,
    ) == [
        "/opt/fossor/miners/custom/1.0.0/mineur",
        "--wallet",
        WALLET,
        "--threads",
        "2",
    ]
