"""Campagnes ``vm``/``hybrid`` sur un hôte Proxmox (§4.6, pve-campagnes).

Le point de décision est testé tel quel (:func:`app.vm_backend.backend_for`) :
un hôte ``os_kind=proxmox`` prend le pilote Proxmox, tout autre hôte garde
libvirt. La campagne simulée exerce le **vrai** ``PveBackend`` : en simulation
chaque fonction de :mod:`app.pve_remote` rend le plan avant tout appel, donc
aucune connexion n'est possible.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import campaign_runner as RUN
from app import campaigns as C
from app import pve_remote as PR
from app import pve_vm as P
from app import spec_vm as V
from app import vm_backend as VB

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 64
WALLET = "demo1pna00000000000000000000000000000000000"


def _db():
    from app.database import SessionLocal

    return SessionLocal()


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


def _seed_pve(db, *, mode="vm", with_model=True, with_pve_setting=True):
    from app.ai_config import set_setting
    from app.models import Campaign, CampaignDevice, Device, MinerProfile

    host = _machine(db, name="pve-lab", host="192.0.2.30", os_kind="proxmox")
    spec = MinerProfile(
        uuid=str(uuid_lib.uuid4()),
        name="Campagne PVE",
        coin="NNN",
        wallet=WALLET,
        pool="solo",
        software="custom",
        binary_url="https://exemple.tld/mineur.tar.gz",
        sha256=SHA,
        binary_path="mineur",
        version="1.0.0",
        cmdline=(
            "/opt/fossor/miners/custom/1.0.0/mineur --wallet {wallet} "
            + ("--pool {vm_node} " if mode == "hybrid" else "--pool {pool} ")
            + "--threads {threads}"
        ),
        threads=2,
        execution_mode=mode,
        enabled=True,
    )
    db.add(spec)
    db.flush()
    cpu = Device(
        uuid=str(uuid_lib.uuid4()),
        machine_id=host.id,
        kind="cpu",
        model="CPU jetable",
        threads=4,
        present=True,
        enabled_for_mining=True,
    )
    db.add(cpu)
    db.flush()
    campaign = Campaign(
        uuid=str(uuid_lib.uuid4()),
        coin="NNN",
        profile_id=spec.id,
        start_at=C.iso(T0 - timedelta(seconds=5)),
        end_at=C.iso(T0 + timedelta(minutes=30)),
        status="approved",
        origin="manual",
        execution_mode=mode,
    )
    db.add(campaign)
    db.flush()
    db.add(
        CampaignDevice(
            campaign_id=campaign.id,
            machine_id=host.id,
            device_uuid=cpu.uuid,
            kind="cpu",
            label="CPU — CPU jetable",
        )
    )
    if with_model:
        from app.routers.pve import PVE_MODEL_KEY

        set_setting(db, PVE_MODEL_KEY, _pve_model(host.id))
        if with_pve_setting:
            set_setting(db, VB.SPEC_HOST_KEY, host.id)
    db.commit()
    return campaign, host, spec


# ── 1. Décision du backend ────────────────────────────────────────────
class TestChoixDuBackend:
    def test_proxmox_prend_le_pilote_pve(self):
        assert VB.backend_kind(SimpleNamespace(os_kind="proxmox")) == "proxmox"
        assert isinstance(VB.backend_for_host(SimpleNamespace(os_kind="proxmox")), VB.PveBackend)

    def test_ubuntu_garde_libvirt(self):
        for kind in ("ubuntu", "debian", "hiveos", "", None):
            assert VB.backend_kind(SimpleNamespace(os_kind=kind)) == "libvirt"
            assert isinstance(
                VB.backend_for_host(SimpleNamespace(os_kind=kind)), VB.LibvirtBackend
            )

    def test_hote_de_spec_choisi_commande_le_modele(self):
        db = _db()
        try:
            pve_campaign, pve_host, _ = _seed_pve(db)
            lib_host = _machine(db, name="ubuntu-lab", host="192.0.2.31", os_kind="ubuntu")
            from app.ai_config import set_setting
            from app.routers.vms import VM_MODEL_KEY

            set_setting(
                db,
                VM_MODEL_KEY,
                {
                    "host_id": lib_host.id,
                    "base_image_url": "https://exemple.tld/base.qcow2",
                    "base_image_sha256": SHA,
                },
            )
            db.commit()
            # Les deux modèles existent : c'est spec_host_id qui tranche.
            context = VB.backend_for(db)
            assert context.kind == "proxmox"
            assert context.host.id == pve_host.id
            set_setting(db, VB.SPEC_HOST_KEY, lib_host.id)
            db.commit()
            context = VB.backend_for(db)
            assert context.kind == "libvirt"
            assert context.host.id == lib_host.id
        finally:
            db.close()

    def test_sans_hote_ou_sans_modele_le_refus_est_nomme(self):
        db = _db()
        try:
            with pytest.raises(VB.VmBackendError) as error:
                VB.backend_for(db)
            assert "hôte de spec" in str(error.value)
            host = _machine(db, name="pve-nu", host="192.0.2.32", os_kind="proxmox")
            from app.ai_config import set_setting

            set_setting(db, VB.SPEC_HOST_KEY, host.id)
            db.commit()
            with pytest.raises(VB.VmBackendError) as error:
                VB.backend_for(db)
            assert "modèle de VM Proxmox" in str(error.value)
        finally:
            db.close()


# ── 2. Campagne simulée : plan complet, aucune exécution ──────────────
class TestCampagneProxmoxSimulee:
    async def test_plan_complet_jusqu_a_la_destruction(self):
        db = _db()
        try:
            campaign, host, profile = _seed_pve(db)
            model = _pve_model(host.id)
            preview = P.plan_preview(model, "fossor-apercu-nnn-000000", model["vmid_min"])
            commands = [step.command() for step in preview]
            assert commands[0].startswith("install -d")
            assert any("qm create 9900" in c for c in commands)
            assert any("qm start 9900" in c for c in commands)
            assert any("qm shutdown 9900" in c for c in commands)
            assert commands[-1] == (
                "qm destroy 9900 --purge --destroy-unreferenced-disks 1"
            )
            assert not any(c.split()[0] == "pct" for c in commands)
            assert "import-from=" in " ".join(commands)
        finally:
            db.close()

    async def test_campagne_vm_simulee_sans_aucun_appel(self):
        from app.models import CampaignEvent, SpecVm

        db = _db()
        try:
            campaign, host, _ = _seed_pve(db)
            await RUN.tick(db, now=T0, simulated=True)
            db.refresh(campaign)
            assert campaign.status == "running"

            events = (
                db.query(CampaignEvent)
                .filter(CampaignEvent.campaign_id == campaign.id)
                .all()
            )
            kinds = [row.kind for row in events]
            assert "vm-created" in kinds and "vm-addressed" in kinds
            assert "vm-provisioned" in kinds and "campaign-started" in kinds

            created = next(row for row in events if row.kind == "vm-created")
            assert "Proxmox" in (created.detail or "") or "qm" in (created.detail or "")
            assert "qm create 9900" in (created.detail or "")
            assert '"hypervisor": "proxmox"' in (created.detail or "").replace(
                '"hypervisor":"proxmox"', '"hypervisor": "proxmox"'
            )

            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            assert vm.hypervisor == "proxmox"
            assert vm.vmid is None  # la simulation n'affirme aucune création
            assert vm.ip == RUN.SIMULATION_IP
            assert vm.name.startswith(f"fossor-c{campaign.id}-nnn-")

            started = next(row for row in events if row.kind == "campaign-started")
            assert "ssh" in (started.detail or "")
            assert "setsid" in (started.detail or "")
        finally:
            db.close()

    async def test_hybride_mine_sur_lhote_avec_le_noeud_de_la_vm(self):
        from app.models import SpecVm

        db = _db()
        try:
            campaign, _, _ = _seed_pve(db, mode="hybrid")
            await RUN.tick(db, now=T0, simulated=True)
            db.refresh(campaign)
            assert campaign.status == "running"
            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            assert vm.mode == "hybrid"
            assert vm.hypervisor == "proxmox"
        finally:
            db.close()


# ── 3. Garde-fous ─────────────────────────────────────────────────────
class TestGardeFous:
    def test_vmid_hors_plage_refuse(self):
        model = _pve_model(1)
        assert not P.in_range(1234, model["vmid_min"], model["vmid_max"])
        with pytest.raises(P.VmError):
            P.choose_vmid(9900, 9901, {9900, 9901})

    async def test_creation_hors_plage_refusee_avant_toute_commande(self):
        calls = []

        async def runner(host, command, stdin=None, timeout=30):
            calls.append(command)
            return {"ok": True, "status": 0, "stdout": "", "stderr": ""}

        PR.RUNNER = runner
        try:
            model = _pve_model(1)
            row = SimpleNamespace(name="fossor-c1-nnn-abcdef", vmid=1234, ip="")
            backend = VB.PveBackend()
            with pytest.raises(VB.VmBackendError) as error:
                await backend.create(SimpleNamespace(os_kind="proxmox"), model, row, True)
            assert "hors de la plage" in str(error.value)
            assert calls == []
        finally:
            PR.RUNNER = None

    async def test_destruction_refusee_sans_sauvegarde_verifiee(self):
        calls = []

        async def runner(host, command, stdin=None, timeout=30):
            calls.append(command)
            return {"ok": True, "status": 0, "stdout": "", "stderr": ""}

        PR.RUNNER = runner
        try:
            model = _pve_model(1)
            row = SimpleNamespace(name="fossor-c1-nnn-abcdef", vmid=9900, ip="")
            backend = VB.PveBackend()
            with pytest.raises(Exception) as error:
                await backend.destroy(
                    SimpleNamespace(os_kind="proxmox"), model, row,
                    backup=None, now_epoch=1_800_000_000, max_age_days=7, simulated=False,
                )
            assert "sauvegarde" in str(error.value)
            assert calls == []
        finally:
            PR.RUNNER = None

    async def test_pct_jamais_execute(self):
        async def runner(host, command, stdin=None, timeout=30):
            return {"ok": True, "status": 0, "stdout": "", "stderr": ""}

        PR.RUNNER = runner
        try:
            with pytest.raises(PR.PveRemoteError):
                await PR._run(
                    SimpleNamespace(os_kind="proxmox"),
                    P.VmStep("conteneur interdit", ("pct", "list")),
                )
        finally:
            PR.RUNNER = None

    async def test_collecte_sans_vmid_refusee(self):
        backend = VB.PveBackend()
        row = SimpleNamespace(name="fossor-c1-nnn-abcdef", vmid=None, ip="192.0.2.40")
        with pytest.raises(VB.VmBackendError) as error:
            await backend.collect(SimpleNamespace(os_kind="proxmox"), {}, row, None, False)
        assert "VMID" in str(error.value)


# ── 4. API : modèle sauvegardé puis relu, plan sans exécution ────────
class TestApiPve:
    async def test_modele_sauvegarde_puis_relu_et_hote_de_spec(self, client):
        db = _db()
        try:
            host = _machine(db, name="pve-api", host="192.0.2.33", os_kind="proxmox")
            host_id = host.id
            db.commit()
        finally:
            db.close()
        payload = {
            "pve_model": {
                "host_id": host_id,
                "node": "pve2",
                "disk_storage": "local-lvm",
                "ci_storage": "local",
                "bridge": "vmbr1",
                "vmid_min": 9910,
                "vmid_max": 9915,
                "vcpus": 4,
                "memory_mb": 4096,
                "disk_gb": 32,
                "image_url": "https://exemple.tld/images/debian-12-genericcloud-amd64.qcow2",
                "checksum": SHA,
                "checksum_algo": "sha256",
            }
        }
        response = await client.put("/api/spec/pve/settings", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pve_model"]["node"] == "pve2"
        assert body["pve_model"]["vmid_min"] == 9910
        assert body["spec_host_id"] == host_id
        assert body["spec_host_kind"] == "proxmox"

        spec = await client.get("/api/spec/settings")
        assert spec.status_code == 200
        assert spec.json()["spec_host_id"] == host_id
        assert spec.json()["spec_host_kind"] == "proxmox"

        plan = await client.post("/api/spec/pve/plan", json={})
        assert plan.status_code == 200, plan.text
        steps = plan.json()["steps"]
        assert steps[0]["command"].startswith("install -d")
        assert steps[-1]["command"].startswith("qm destroy 9910")
        assert all(step["command"].split()[0] != "pct" for step in steps)
