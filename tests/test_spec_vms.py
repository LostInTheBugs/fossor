"""Modes ``vm`` et ``hybrid`` (§4.6, étape 3c) — modèle, plans, garde-fous, simulation.

Aucune connexion : les plans sont figés (assertions sur le texte exact) et les
quelques chemins réels utilisent un ``RUNNER`` factice qui journalise.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import timedelta

import pytest

from app import campaign_runner as RUN
from app import campaigns as C
from app import miners as M
from app import spec_vm as V
from app import vm_remote as VR

T0 = C.parse_iso("2026-11-18T12:00:00+00:00")
WALLET = "prl1demo" + "q" * 30
SHA = "b" * 64
NAME = "fossor-c7-qwc-a1b2c3"


def model(**over) -> dict:
    data = {
        "host_id": 3,
        "base_image_url": (
            "https://cloud.debian.org/images/cloud/trixie/latest/"
            "debian-12-genericcloud-amd64.qcow2"
        ),
        "base_image_sha256": SHA,
        "vcpus": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
    }
    data.update(over)
    return V.validate_vm_model(data)


def profile(**over) -> dict:
    data = {
        "name": "NNN dans la VM",
        "coin": "NNN",
        "wallet": WALLET,
        "pool": "solo",
        "software": "custom",
        "binary_url": "https://exemple.tld/mineur.tar.gz",
        "sha256": SHA,
        "binary_path": "mineur",
        "version": "1.0.0",
        "cmdline": "/opt/fossor/miners/custom/1.0.0/mineur --wallet {wallet} --threads {threads}",
        "threads": 2,
        "execution_mode": "vm",
    }
    data.update(over)
    return M.validate_profile(data)


# ── 1. Modèle de VM ──────────────────────────────────────────────────
class TestModele:
    def test_sha256_obligatoire(self):
        for value in ("", None, "abc", "z" * 64):
            with pytest.raises(V.VmError) as exc:
                V.validate_vm_model(
                    {
                        "host_id": 1,
                        "base_image_url": "https://exemple.tld/debian.qcow2",
                        "base_image_sha256": value,
                    }
                )
            assert "SHA256" in str(exc.value)

    def test_url_obligatoire_et_nom_de_fichier(self):
        with pytest.raises(V.VmError):
            V.validate_vm_model({"host_id": 1, "base_image_sha256": SHA})
        with pytest.raises(V.VmError) as exc:
            V.validate_vm_model(
                {
                    "host_id": 1,
                    "base_image_url": "https://exemple.tld/image.txt",
                    "base_image_sha256": SHA,
                }
            )
        assert ".qcow2" in str(exc.value)

    def test_bornes_et_defauts(self):
        full = model()
        assert (full["vcpus"], full["memory_mb"], full["disk_gb"]) == (2, 2048, 20)
        assert full["network"] == "default"
        assert full["node_port"] == V.DEFAULT_NODE_PORT
        assert full["base_image_name"] == "debian-12-genericcloud-amd64.qcow2"
        for field, value in (("vcpus", 0), ("vcpus", 33), ("memory_mb", 128),
                             ("disk_gb", 1), ("node_port", 80)):
            with pytest.raises(V.VmError):
                model(**{field: value})

    def test_hote_obligatoire(self):
        with pytest.raises(V.VmError) as exc:
            V.validate_vm_model(
                {"base_image_url": "https://e.tld/a.qcow2", "base_image_sha256": SHA}
            )
        assert "vm_host" in str(exc.value)

    def test_gabarit_refuse_les_metacaracteres(self):
        with pytest.raises(V.VmError):
            model(wallet_create_cmd="createwallet; rm -rf /")


# ── 2. Commandes figées ──────────────────────────────────────────────
class TestCommandes:
    def test_plan_de_creation(self):
        m = model()
        plan = V.create_plan(m, NAME, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExemplaireDeClePubliqueFossor fossor@test")
        commands = [step.command() for step in plan]
        overlay = (
            f"sudo -n qemu-img create -f qcow2 -b {V.IMAGE_ROOT}/images/"
            f"debian-12-genericcloud-amd64.qcow2 -F qcow2 {V.IMAGE_ROOT}/vms/{NAME}/disk.qcow2 20G"
        )
        assert overlay in commands
        assert (
            f"sudo -n cloud-localds {V.IMAGE_ROOT}/vms/{NAME}/seed.iso "
            f"{V.IMAGE_ROOT}/vms/{NAME}/user-data {V.IMAGE_ROOT}/vms/{NAME}/meta-data"
        ) in commands
        install = [c for c in commands if "--name" in c][0]
        assert install.startswith("sudo -n virt-install --connect qemu:///system")
        assert "--import" in install and "--noautoconsole" in install
        assert f"path={V.IMAGE_ROOT}/vms/{NAME}/disk.qcow2,format=qcow2,bus=virtio" in install
        assert "network=default,model=virtio" in install
        assert "--metadata description=fossor_vm=1,title=Fossor" in install
        assert "--memory 2048 --vcpus 2" in install

    def test_image_verifiee_par_sha256_avant_creation(self):
        plan = V.create_plan(model(), NAME, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExemplaireDeClePubliqueFossor fossor@test")
        image = plan[1].command()
        assert "sha256sum -c -" in image
        assert model()["base_image_sha256"] in image
        assert "--max-time 600" in image

    def test_user_data_contient_specminer_et_la_cle(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExemplaire fossor@instance"
        user_data = V.cloud_init_user_data(NAME, key)
        assert "name: specminer" in user_data
        assert key in user_data
        assert "NOPASSWD:ALL" in user_data
        assert "lock_passwd: true" in user_data
        assert "ssh_pwauth: false" in user_data
        # Aucun mot de passe, aucune seed : la clé publique seulement.
        assert "password:" not in user_data
        assert "instance-id: " + NAME in V.cloud_init_meta_data(NAME)

    def test_extinction_conserve_la_vm(self):
        commands = [s.command() for s in V.shutdown_plan(NAME)]
        assert commands[0] == f"sudo -n virsh --connect qemu:///system shutdown {NAME}"
        assert not any("undefine" in c or "rm -rf" in c for c in commands)

    def test_destruction_explicite(self):
        commands = [s.command() for s in V.destroy_plan(NAME)]
        assert commands[0] == f"sudo -n virsh --connect qemu:///system destroy {NAME}"
        assert commands[1] == f"sudo -n virsh --connect qemu:///system undefine {NAME} --nvram"
        assert commands[2] == f"sudo -n rm -rf {V.IMAGE_ROOT}/vms/{NAME}"

    def test_liste_ne_touche_rien(self):
        commands = [s.command() for s in V.list_plan()]
        assert "list --all --name" in commands[0]
        assert "destroy" not in commands[0] and "undefine" not in commands[0]

    def test_nom_de_vm_prefixe(self):
        assert V.vm_name(7, "qwc", "a1b2c3") == NAME
        with pytest.raises(V.VmError):
            V.vm_name(0, "qwc", "a1b2c3")
        with pytest.raises(V.VmError):
            V.vm_name(7, "qwc", "../x")


# ── 3. Propriété : refus des VM étrangères ───────────────────────────
class TestPropriete:
    def test_cas_reel_du_parc_de_fred(self):
        """Les specbox existantes n'ont ni préfixe ni métadonnée : refus."""
        for name, description in (
            ("specbox-wam", ""),
            ("specbox-wam", "fossor_vm=1"),
            ("fossor-ancienne", ""),
            ("Ubuntu-22.04", "fossor_vm=1"),
        ):
            with pytest.raises(V.VmError):
                V.ensure_owned(name, description)
        assert V.is_owned(NAME, "fossor_vm=1") is True
        assert V.is_owned(NAME, "") is False
        assert V.is_owned("specbox-wam", "fossor_vm=1") is False

    def test_liste_marque_les_vm_etrangeres(self):
        text = (
            "VM:specbox-wam\n"
            "  specbox PCRyzen\n"
            f"VM:{NAME}\n"
            "  fossor_vm=1\n"
        )
        items = V.parse_vm_list(text)
        assert [(i.name, i.owned) for i in items] == [("specbox-wam", False), (NAME, True)]


# ── 4. Destruction : sauvegarde vérifiée obligatoire (§10.11) ────────
class TestSauvegarde:
    def test_stat_absent_vide_ou_illisible(self):
        assert V.parse_backup_stat("MISSING")["present"] is False
        assert V.parse_backup_stat("")["present"] is False
        assert V.parse_backup_stat("n'importe quoi")["present"] is False
        assert V.parse_backup_stat("2048 1700000000") == {
            "present": True,
            "size": 2048,
            "mtime": 1700000000,
        }

    def test_verdict(self):
        now = 1_700_000_000
        ok = V.evaluate_backup({"present": True, "size": 2048, "mtime": now - 3600}, now)
        assert ok["verified"] is True
        assert "récente" in ok["reason"]
        missing = V.evaluate_backup({"present": False, "size": 0, "mtime": 0}, now)
        assert missing["verified"] is False and "absent" in missing["reason"]
        empty = V.evaluate_backup({"present": True, "size": 0, "mtime": now}, now)
        assert empty["verified"] is False and "vide" in empty["reason"]
        stale = V.evaluate_backup(
            {"present": True, "size": 10, "mtime": now - 30 * 86400}, now, max_age_days=7
        )
        assert stale["verified"] is False and "ancienne" in stale["reason"]

    def test_destruction_refusee_sans_sauvegarde(self):
        now = 1_700_000_000
        for backup in (None, {}, {"verified": False, "reason": "fichier absent"}):
            with pytest.raises(V.VmError) as exc:
                V.ensure_destroyable(NAME, V.OWNER_MARKER, backup, now)
            assert "Destruction refusée" in str(exc.value)

    def test_destruction_acceptee_apres_controle(self):
        now = 1_700_000_000
        backup = V.evaluate_backup({"present": True, "size": 4096, "mtime": now}, now)
        assert V.ensure_destroyable(NAME, V.OWNER_MARKER, backup, now)["verified"] is True

    def test_controle_trop_ancien_refuse(self):
        now = 1_700_000_000
        backup = {"verified": True, "checked_at_epoch": now - 40 * 86400}
        with pytest.raises(V.VmError) as exc:
            V.ensure_destroyable(NAME, V.OWNER_MARKER, backup, now, max_age_days=7)
        assert "trop ancien" in str(exc.value)

    def test_commande_de_controle_ne_lit_pas_le_fichier(self):
        command = V.backup_check_plan("192.0.2.10", "/home/specminer/data/wallet.dat")[0].command()
        assert 'stat -c "%s %Y"' in command
        for forbidden in ("cat ", "sha256sum", "xxd", "base64"):
            assert forbidden not in command


# ── 5. `{vm_node}` ───────────────────────────────────────────────────
class TestVmNode:
    def test_adresse_valide(self):
        assert V.validate_vm_node("192.168.122.10:19333") == "192.168.122.10:19333"
        assert V.node_address("192.168.122.10", 19333) == "192.168.122.10:19333"

    def test_adresses_refusees(self):
        for value in (
            "",
            "192.168.122.10",
            "192.168.122.10:80",
            "192.168.122.10:70000",
            "abc:19333",
            "192.168.122.10:19333; rm -rf /",
            "192.168.122.10:19333\n",
            "::1:19333",
        ):
            with pytest.raises((V.VmError, M.ProfileError)):
                V.validate_vm_node(value)

    def test_gabarit_hybride_rendu(self):
        p = profile(
            execution_mode="hybrid",
            cmdline=(
                "/opt/fossor/miners/custom/1.0.0/mineur --wallet {wallet} "
                "--pool {vm_node} --gpus {gpus}"
            ),
            pool="solo",
        )
        values = M.build_values(p, worker="rig", gpus=[0], vm_node="192.168.122.10:19333")
        argv = M.render_argv(p, values)
        assert "--pool" in argv and "--gpus" in argv
        assert argv[argv.index("--pool") + 1] == "192.168.122.10:19333"
        assert argv[argv.index("--gpus") + 1] == "0"

    def test_vm_node_absent_refuse(self):
        p = profile(
            execution_mode="hybrid",
            cmdline="/opt/fossor/miners/custom/1.0.0/mineur --pool {vm_node}",
        )
        with pytest.raises(M.ProfileError):
            M.build_values(p, worker="rig")

    def test_injection_par_valeur_substituee_refusee(self):
        p = profile(
            execution_mode="hybrid",
            cmdline="/opt/fossor/miners/custom/1.0.0/mineur --pool {vm_node}",
        )
        with pytest.raises(M.ProfileError):
            M.build_values(p, worker="rig", vm_node="192.168.122.10:19333;reboot")


# ── 6. Simulation : zéro exécution ──────────────────────────────────
class RecordingRunner:
    def __init__(self, stdout=""):
        self.calls: list[str] = []
        self.stdout = stdout

    async def __call__(self, machine, command, stdin=None, timeout=120):
        self.calls.append(command)
        return {"status": 0, "stdout": self.stdout, "stderr": "", "ok": True}


class ExplodingRunner:
    async def __call__(self, *args, **kwargs):  # pragma: no cover - doit être inatteignable
        raise AssertionError("aucune commande ne doit partir en simulation")


class TestSimulation:
    async def test_creation_simulee_zero_execution(self, monkeypatch):
        monkeypatch.setattr(VR, "RUNNER", ExplodingRunner())
        result = await VR.create(
            object(), model(), NAME,
            ssh_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExemplaireDeClePubliqueFossor fossor@test",
            private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfaux\n-----END OPENSSH PRIVATE KEY-----",
            simulated=True,
        )
        assert result["simulated"] is True
        assert any("virt-install" in c for c in result["commands"])
        assert any("qemu-img create" in c for c in result["commands"])
        assert any("cloud-localds" in c for c in result["commands"])

    async def test_provisionnement_et_mineur_simules(self, monkeypatch):
        monkeypatch.setattr(VR, "RUNNER", ExplodingRunner())
        got = await VR.provision(
            object(), model(node_url="https://e.tld/node.tar.gz", node_sha256=SHA),
            profile(), "192.0.2.10", coin="NNN", simulated=True, expected_address=WALLET,
        )
        assert any("sha256sum -c -" in c for c in got["commands"])
        start = await VR.start_miner(object(), profile(), ["/opt/fossor/x"], "192.0.2.10",
                                    simulated=True)
        assert "setsid nohup" in start["commands"][0]
        stop = await VR.stop_miner(object(), profile(), "192.0.2.10", simulated=True)
        assert "kill -TERM" in stop["commands"][0]
        down = await VR.shutdown(object(), NAME, simulated=True)
        assert any("shutdown" in c for c in down["commands"])

    async def test_destruction_simulee_zero_execution(self, monkeypatch):
        monkeypatch.setattr(VR, "RUNNER", ExplodingRunner())
        backup = V.evaluate_backup({"present": True, "size": 10, "mtime": 1_700_000_000},
                                   1_700_000_000)
        result = await VR.destroy(
            object(), NAME, V.OWNER_MARKER, backup=backup, now_epoch=1_700_000_000,
            max_age_days=7, simulated=True,
        )
        assert result["simulated"] is True
        assert any("undefine" in c for c in result["commands"])

    async def test_destruction_refusee_avant_toute_commande(self, monkeypatch):
        runner = RecordingRunner()
        monkeypatch.setattr(VR, "RUNNER", runner)
        with pytest.raises(V.VmError):
            await VR.destroy(object(), NAME, V.OWNER_MARKER, backup=None,
                             now_epoch=1_700_000_000, max_age_days=7, simulated=False)
        assert runner.calls == []

    async def test_destruction_refusee_sur_vm_etrangere(self, monkeypatch):
        runner = RecordingRunner()
        monkeypatch.setattr(VR, "RUNNER", runner)
        backup = {"verified": True, "checked_at_epoch": 1_700_000_000}
        with pytest.raises(V.VmError):
            await VR.destroy(object(), "specbox-wam", "", backup=backup,
                             now_epoch=1_700_000_000, max_age_days=7, simulated=False)
        assert runner.calls == []

    async def test_destruction_reelle_apres_controle(self, monkeypatch):
        runner = RecordingRunner()
        monkeypatch.setattr(VR, "RUNNER", runner)
        backup = V.evaluate_backup({"present": True, "size": 10, "mtime": 1_700_000_000},
                                   1_700_000_000)
        result = await VR.destroy(object(), NAME, V.OWNER_MARKER, backup=backup,
                                  now_epoch=1_700_000_000, max_age_days=7, simulated=False)
        assert result["destroyed"] is True
        assert len(runner.calls) == 3
        assert "undefine" in runner.calls[1]


# ── 7. Parsing -------------------------------------------------------
class TestParsing:
    def test_ip_arp_puis_lease(self):
        arp = (
            " Name       MAC address          Protocol     Address\n"
            "-------------------------------------------------------------------------------\n"
            " vnet0      52:54:00:aa:bb:cc    ipv4         192.168.122.45/24\n"
        )
        assert V.parse_domifaddr(arp) == "192.168.122.45"
        assert V.parse_domifaddr("rien") is None
        assert V.parse_domifaddr(" vnet0 52:54:00:aa:bb:cc ipv4 169.254.1.1/16") is None
        assert V.parse_domstate("running\n") == "running"

    def test_proxmox_detecte_sans_pilote(self):
        assert V.parse_pveversion("pve-manager/8.2.4/abfc1b4a5e21bd18")["driver"] == "proxmox"
        assert V.parse_pveversion("bash: pveversion: command not found")["driver"] == "none"
        assert V.parse_libvirt_version("Virsh command line tool of libvirt 10.0.0")["driver"] == "libvirt"

    def test_dominfo(self):
        text = (
            "Id:             12\n"
            "Name:           fossor-c7-qwc-a1b2c3\n"
            "CPU(s):         2\n"
            "CPU time:       12.5s\n"
            "Max memory:     2097152 KiB\n"
        )
        info = VR.parse_dominfo(text)
        assert info == {"vcpus": 2, "cpu_time_s": 12.5, "memory_kb": 2097152}


# ── 8. Campagne en mode vm / hybrid (planificateur) ──────────────────
class SpecBackend:
    """Backend factice : journalise, ne touche à rien."""

    def __init__(self, fail_create=False):
        self.calls: list[tuple] = []
        self.fail_create = fail_create

    async def start(self, machine, profile, argv, simulated):
        self.calls.append(("start", profile["name"], tuple(argv)))
        return {"simulated": simulated, "commands": ["start " + profile["name"]]}

    async def stop(self, machine, profile, simulated):
        self.calls.append(("stop", profile["name"]))
        return {"simulated": simulated, "commands": ["stop " + profile["name"]]}

    async def hashrate(self, machine, profile):
        return 42.0

    async def vm_create(self, host, model, name, simulated):
        self.calls.append(("vm_create", name, simulated))
        if self.fail_create:
            raise RuntimeError("libvirt absent")
        return {"simulated": simulated, "commands": [f"virt-install --name {name}"]}

    async def vm_address(self, host, name, simulated):
        self.calls.append(("vm_address", name))
        return {"simulated": simulated, "ip": "192.168.122.77"}

    async def vm_pin(self, host, ip, simulated):
        self.calls.append(("vm_pin", ip))
        return {"simulated": simulated, "host_key": f"{ip} ssh-ed25519 AAAA"}

    async def vm_provision(self, host, model, profile, ip, coin, expected_address, simulated):
        self.calls.append(("vm_provision", ip, coin, expected_address))
        return {"simulated": simulated, "address": expected_address,
                "commands": ["ssh specminer@ip sha256sum -c -"]}

    async def vm_start_miner(self, host, profile, argv, ip, simulated):
        self.calls.append(("vm_start_miner", ip, tuple(argv)))
        return {"simulated": simulated, "commands": ["ssh specminer@ip setsid nohup mineur"]}

    async def vm_stop_miner(self, host, profile, ip, simulated):
        self.calls.append(("vm_stop_miner", ip))
        return {"simulated": simulated, "commands": ["ssh specminer@ip kill -TERM"]}

    async def vm_shutdown(self, host, name, simulated):
        self.calls.append(("vm_shutdown", name, simulated))
        return {"simulated": simulated, "commands": [f"virsh shutdown {name}"]}


def _seed_spec(db, *, mode="vm", with_model=True, status="approved"):
    from app.ai_config import set_setting
    from app.models import Campaign, CampaignDevice, Device, Machine, MinerProfile

    from app import campaign_runner as R

    host = Machine(
        uuid=str(uuid_lib.uuid4()),
        name="hote-vm",
        host="192.0.2.20",
        ssh_port=22,
        ssh_user="fossor",
        auth_type="fossor_key",
        os_kind="ubuntu",
        enabled=True,
        usage="cpu",
        vm_host=True,
    )
    db.add(host)
    db.flush()
    default = MinerProfile(
        uuid=str(uuid_lib.uuid4()),
        name="Token par défaut",
        coin="PRL",
        wallet=WALLET,
        pool="stratum+tcp://pool.exemple.tld:3333",
        software="custom",
        binary_url="https://e.tld/defaut.tar.gz",
        sha256=SHA,
        binary_path="defaut",
        version="1.0.0",
        cmdline="/opt/fossor/miners/custom/1.0.0/defaut --wallet {wallet} --threads {threads}",
        execution_mode="host",
        enabled=True,
        threads=2,
    )
    db.add(default)
    db.flush()
    spec = MinerProfile(
        uuid=str(uuid_lib.uuid4()),
        name="Campagne spec",
        coin="NNN",
        wallet=WALLET,
        pool="solo",
        software="custom",
        binary_url="https://e.tld/mineur.tar.gz",
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
    host.default_miner_profile_id = default.id
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
        end_at=C.iso(T0 + timedelta(seconds=120)),
        status=status,
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
        set_setting(db, "spec_vm_model", model(host_id=host.id))
        set_setting(db, "spec_default_mode", mode)
    db.commit()
    return campaign, host, default, spec


def _db():
    from app.database import SessionLocal

    return SessionLocal()


class TestCampagneVm:
    async def test_demarrage_cree_la_vm_et_journalise_les_commandes(self):
        from app.models import CampaignEvent, SpecVm

        db = _db()
        try:
            campaign, host, _, _ = _seed_spec(db)
            backend = SpecBackend()
            result = await RUN.tick(db, now=T0, backend=backend, simulated=True)
            db.refresh(campaign)
            assert result[0]["action"] == "started"
            assert campaign.status == "running"
            kinds = [row.kind for row in db.query(CampaignEvent)
                     .filter(CampaignEvent.campaign_id == campaign.id).all()]
            assert kinds[:2] == ["default-stopped", "vm-created"]
            assert "vm-addressed" in kinds
            assert "vm-provisioned" in kinds
            assert "campaign-started" in kinds
            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            assert vm.name.startswith("fossor-c%d-nnn-" % campaign.id)
            assert vm.host_machine_id == host.id
            assert vm.mode == "vm"
            assert vm.ip == RUN.SIMULATION_IP
            assert vm.wallet_address == WALLET
            assert vm.backup_path == f"{V.DATA_DIR}/wallet.dat"
            started = [c for c in backend.calls if c[0] == "vm_start_miner"]
            assert started and started[0][1] == RUN.SIMULATION_IP
            # Le mineur de la VM reçoit le pool du profil (solo) et le wallet.
            argv = started[0][2]
            assert "solo" in argv and WALLET in argv
        finally:
            db.close()

    async def test_fin_de_campagne_eteint_et_conserve_la_vm(self):
        from app.models import CampaignEvent, SpecVm

        db = _db()
        try:
            campaign, _, _, _ = _seed_spec(db)
            backend = SpecBackend()
            await RUN.tick(db, now=T0, backend=backend, simulated=True)
            later = T0 + timedelta(seconds=200)
            await RUN.tick(db, now=later, backend=backend, simulated=True)
            kinds = [row.kind for row in db.query(CampaignEvent)
                     .filter(CampaignEvent.campaign_id == campaign.id).all()]
            assert "campaign-stopped" in kinds
            assert "vm-stopped" in kinds
            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            assert vm.destroyed_at is None
            assert ("vm_shutdown", vm.name, True) in backend.calls
            assert not any(call[0] == "vm_create" and call[2] is False for call in backend.calls)
            event = (
                db.query(CampaignEvent)
                .filter(CampaignEvent.campaign_id == campaign.id,
                        CampaignEvent.kind == "vm-stopped")
                .first()
            )
            assert "conservée" in event.message
            assert '"kept": true' in (event.detail or "").replace('"kept":true', '"kept": true')
        finally:
            db.close()

    async def test_sans_modele_de_vm_la_campagne_echoue(self):
        db = _db()
        try:
            campaign, _, _, _ = _seed_spec(db, with_model=False)
            await RUN.tick(db, now=T0, backend=SpecBackend(), simulated=True)
            db.refresh(campaign)
            assert campaign.status == "failed"
            assert "modèle de VM" in (campaign.note or "")
        finally:
            db.close()

    async def test_echec_de_creation_marque_la_vm(self):
        from app.models import SpecVm

        db = _db()
        try:
            campaign, _, _, _ = _seed_spec(db)
            await RUN.tick(db, now=T0, backend=SpecBackend(fail_create=True), simulated=True)
            db.refresh(campaign)
            assert campaign.status == "failed"
            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            assert vm.state == "failed"
            assert "libvirt absent" in (vm.note or "")
        finally:
            db.close()

    async def test_hybride_mine_sur_lhote_avec_le_noeud_de_la_vm(self):
        from app.models import SpecVm

        db = _db()
        try:
            campaign, host, _, _ = _seed_spec(db, mode="hybrid")
            backend = SpecBackend()
            await RUN.tick(db, now=T0, backend=backend, simulated=True)
            db.refresh(campaign)
            assert campaign.status == "running"
            assert ("vm_start_miner", RUN.SIMULATION_IP) not in [
                (c[0], c[1]) for c in backend.calls if c[0] == "vm_start_miner"
            ]
            started = [c for c in backend.calls if c[0] == "start"]
            assert started, "le mineur doit démarrer sur l'hôte"
            argv = started[0][2]
            assert f"{RUN.SIMULATION_IP}:{V.DEFAULT_NODE_PORT}" in argv
            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            assert vm.mode == "hybrid"
        finally:
            db.close()
