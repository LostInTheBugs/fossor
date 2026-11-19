"""Pilote Proxmox (§4.6) : commandes figées, garde-fous, simulation sans exécution.

Les charges de test reprennent la **forme réelle** des sorties Proxmox
(``pvesm status``, ``pvesh get /storage``, ``qm list``, ``qm config``, agent
invité, ``ip neigh``) relevées sur un nœud 9.2.2 ; les valeurs sont neutres.
"""

from __future__ import annotations

import pytest

from app import pve_remote as PR
from app import pve_vm as P

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFossorTestKeyOnly000000000000000000 fossor@test"
VMID = 9900
SAVE = {"verified": True, "reason": "ok", "checked_at_epoch": 1_000_000}


def model(**over):
    raw = {
        "host_id": 3,
        "node": "pve",
        "disk_storage": "data2-lvm",
        "ci_storage": "local",
        "bridge": "vmbr0",
        "vmid_min": 9900,
        "vmid_max": 9999,
        "vcpus": 1,
        "memory_mb": 1024,
        "disk_gb": 8,
        "image_url": "https://cloud.debian.org/images/cloud/bookworm/latest/"
                     "debian-12-genericcloud-amd64.qcow2",
        "checksum": "a" * 128,
        "checksum_algo": "sha512",
    }
    raw.update(over)
    return P.validate_pve_model(raw)


# ── Modèle ───────────────────────────────────────────────────────────
def test_modele_defauts_et_plage():
    m = P.validate_pve_model(
        {
            "host_id": 3,
            "node": "pve",
            "image_url": "https://exemple.local/img/debian-12-genericcloud-amd64.qcow2",
            "checksum": "b" * 64,
        }
    )
    assert (m["vmid_min"], m["vmid_max"]) == (9900, 9999)
    assert m["bridge"] == "vmbr0"
    assert m["disk_storage"] == "local-lvm"
    assert m["ci_storage"] == "local"
    assert m["checksum_algo"] == "sha256"
    assert m["image_name"] == "debian-12-genericcloud-amd64.qcow2"


def test_modele_refuse_somme_absente_ou_tronquee():
    with pytest.raises(P.VmError, match="SHA512"):
        model(checksum="a" * 64)  # 64 hex pour un sha512
    with pytest.raises(P.VmError, match="SHA256"):
        model(checksum="a" * 128, checksum_algo="sha256")
    with pytest.raises(P.VmError, match="somme"):
        model(checksum="")


def test_modele_refuse_plage_inversee_et_nom():
    with pytest.raises(P.VmError, match="plage"):
        model(vmid_min=9999, vmid_max=9900)
    with pytest.raises(P.VmError, match="nom"):
        model(image_url="https://exemple.local/img/image.iso")


# ── Choix du VMID ────────────────────────────────────────────────────
def test_vmid_premier_libre_de_la_plage():
    assert P.choose_vmid(9900, 9999, {9900, 9901, 9903}) == 9902


def test_vmid_ignore_hors_plage_et_dict():
    assert P.choose_vmid(9900, 9999, {100: {"name": "x"}, 9900: {}}) == 9901


def test_vmid_plage_pleine_refusee():
    with pytest.raises(P.VmError, match="pleine"):
        P.choose_vmid(9900, 9901, {9900, 9901})


# ── Analyse des sorties ──────────────────────────────────────────────
PVESM = """Name             Type     Status     Total (KiB)      Used (KiB) Available (KiB)        %
data2-lvm     lvmthin     active      1919827968       345761017      1574066950   18.01%
local             dir     active        98497780        33387896        60060336   33.90%
local-lvm     lvmthin     active       365760512        61228309       304532202   16.74%
"""

STORAGE_JSON = (
    '[{"content":"backup,import,snippets,iso,vztmpl,images","path":"/var/lib/vz",'
    '"storage":"local","type":"dir"},'
    '{"content":"images,rootdir","storage":"data2-lvm","type":"lvmthin"},'
    '{"content":"images,rootdir","storage":"local-lvm","type":"lvmthin"}]'
)

QM_LIST = """      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID
       100 vm-ha                running    4096              32.00 1422733
       101 vm-windows01         stopped    8192             120.00 0
"""

CONFIG = """boot: order=scsi0
cores: 1
description: fossor_vm=1 fossor-test-pve
ide2: local:9900/vm-9900-cloudinit.qcow2,media=cdrom
memory: 1024
name: fossor-test-pve
net0: virtio=BC:24:11:0A:0B:0C,bridge=vmbr0
scsi0: data2-lvm:vm-9900-disk-0,size=8G
tags: fossor
"""

AGENT = """[
   {
      "name" : "lo",
      "ip-addresses" : [ { "ip-address" : "127.0.0.1", "ip-address-type" : "ipv4", "prefix" : 8 } ]
   },
   {
      "hardware-address" : "bc:24:11:0a:0b:0c",
      "name" : "eth0",
      "ip-addresses" : [
         { "ip-address" : "fe80::be24:11ff:fe0a:b0c", "ip-address-type" : "ipv6", "prefix" : 64 },
         { "ip-address" : "10.0.0.42", "ip-address-type" : "ipv4", "prefix" : 24 }
      ]
   }
]"""


def test_pvesm_status():
    rows = P.parse_pvesm_status(PVESM)
    assert [r["name"] for r in rows] == ["data2-lvm", "local", "local-lvm"]
    assert rows[0]["avail_gb"] == pytest.approx(1501.15, abs=0.02)
    assert rows[2]["status"] == "active"


def test_pvesh_storages_et_eligibilite():
    storages = P.parse_pve_storages(STORAGE_JSON)
    assert [s["name"] for s in storages] == ["local", "data2-lvm", "local-lvm"]
    assert storages[0]["images"] is True
    eligible = P.eligible_storages(P.parse_pvesm_status(PVESM), storages, 8)
    assert [s["name"] for s in eligible] == ["data2-lvm", "local", "local-lvm"]
    assert P.eligible_storages(P.parse_pvesm_status(PVESM), storages, 99999) == []


def test_pvesh_json_illisible_ne_leve_pas():
    assert P.parse_pve_storages("permission denied") == []
    assert P.parse_cluster_resources("pas du json") == set()


def test_qm_list_et_ressources_cluster():
    listed = P.parse_qm_list(QM_LIST)
    assert listed[100] == {"name": "vm-ha", "status": "running"}
    taken = P.parse_cluster_resources(
        '[{"type":"qemu","vmid":100,"name":"vm-ha"},{"type":"node","node":"pve"},'
        '{"type":"qemu","vmid":9900}]'
    )
    assert taken == {100, 9900}


def test_qm_config_tags_description_mac():
    cfg = P.parse_qm_config(CONFIG)
    assert cfg["name"] == "fossor-test-pve"
    assert cfg["tags"] == ["fossor"]
    assert cfg["description"].startswith("fossor_vm=1")
    assert cfg["mac"] == "bc:24:11:0a:0b:0c"


def test_agent_invite_et_arp():
    assert P.parse_guest_ip(AGENT) == "10.0.0.42"
    assert P.parse_guest_ip("QEMU guest agent is not running") == ""
    neigh = (
        "10.0.0.1 dev vmbr0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
        "10.0.0.42 dev vmbr0 lladdr BC:24:11:0A:0B:0C STALE\n"
    )
    assert P.parse_ip_neigh(neigh, "bc:24:11:0a:0b:0c") == "10.0.0.42"
    assert P.parse_ip_neigh(neigh, "00:11:22:33:44:55") == ""


# ── Commandes figées ─────────────────────────────────────────────────
def test_create_plan_commandes():
    steps = P.create_plan(model(), "fossor-test-pve", VMID, KEY)
    argv = {s.title: s.argv for s in steps}
    create = argv["création de la VM (qm create)"]
    assert create[:3] == ("qm", "create", "9900")
    assert create[create.index("--name") + 1] == "fossor-test-pve"
    assert create[create.index("--tags") + 1] == "fossor"
    assert P.OWNER_MARKER in create[create.index("--description") + 1]
    assert create[create.index("--net0") + 1] == "virtio,bridge=vmbr0"
    assert create[create.index("--agent") + 1] == "enabled=1"
    assert create[create.index("--onboot") + 1] == "0"

    disk = argv["import du disque cloud dans le stockage choisi"]
    assert disk[:3] == ("qm", "set", "9900")
    assert disk[disk.index("--scsi0") + 1] == (
        "data2-lvm:0,import-from=/var/tmp/fossor-pve/images/"
        "debian-12-genericcloud-amd64.qcow2"
    )
    assert ("qm", "resize", "9900", "scsi0", "8G") == argv["redimensionnement du disque"]

    ci = argv["cloud-init (utilisateur specminer, clé Fossor, DHCP)"]
    assert ci[ci.index("--ciuser") + 1] == "specminer"
    assert ci[ci.index("--ipconfig0") + 1] == "ip=dhcp"
    assert ci[ci.index("--ide2") + 1] == "local:cloudinit"
    assert ("qm", "start", "9900") == argv["démarrage de la VM"]


def test_image_plan_verifie_sha512():
    steps = P.image_plan(model())
    download = steps[1].argv
    assert download[0] == "sh" and download[1] == "-c"
    assert '${a}sum' in download[2]  # l'algorithme est un argument, jamais concaténé nu
    assert download[-5].endswith("debian-12-genericcloud-amd64.qcow2")
    assert download[-3] == "sha512"
    assert download[-4] == "a" * 128


def test_destroy_plan_et_arret():
    steps = P.destroy_plan(VMID)
    assert steps[0].argv == ("qm", "stop", "9900")
    assert steps[1].argv == (
        "qm", "destroy", "9900", "--purge", "--destroy-unreferenced-disks", "1"
    )
    assert P.shutdown_plan(VMID)[0].argv == ("qm", "shutdown", "9900", "--timeout", "120")


def test_aucun_plan_ne_contient_pct():
    plans = [
        P.inspect_plan(), P.detect_plan(), P.create_plan(model(), "fossor-x", VMID, KEY),
        P.destroy_plan(VMID), P.shutdown_plan(VMID), P.ip_plan(VMID), P.config_plan(VMID),
        P.cleanup_plan(),
    ]
    assert not [s for plan in plans for s in plan if s.argv and s.argv[0] == "pct"]


def test_detect_exige_version_et_kvm():
    assert P.parse_detect("pve-manager/9.2.2/b9984c6d90a4bd80\nKVM=1\nQM=1")["vm_host"] is True
    assert P.parse_detect("pve-manager/9.2.2/b998\nKVM=0\nQM=1")["vm_host"] is False
    assert P.parse_detect("KVM=1")["hypervisor"] == ""


# ── Propriété et destruction ─────────────────────────────────────────
def test_refus_hors_plage():
    with pytest.raises(P.VmError, match="hors de la plage"):
        P.ensure_owned(100, 9900, 9999, ["fossor"], P.OWNER_MARKER)


def test_refus_sans_tag():
    with pytest.raises(P.VmError, match="tag"):
        P.ensure_owned(9900, 9900, 9999, [], P.OWNER_MARKER)


def test_refus_sans_metadonnee():
    with pytest.raises(P.VmError, match="métadonnée"):
        P.ensure_owned(9900, 9900, 9999, ["fossor"], "vm de Fred")


def test_refus_destruction_sans_sauvegarde():
    with pytest.raises(P.VmError, match="sauvegarde"):
        P.ensure_destroyable(9900, 9900, 9999, ["fossor"], P.OWNER_MARKER, None, 1_000_000)
    vieux = {"verified": True, "reason": "ok", "checked_at_epoch": 1}
    with pytest.raises(P.VmError, match="trop ancien"):
        P.ensure_destroyable(9900, 9900, 9999, ["fossor"], P.OWNER_MARKER, vieux, 10**9, 7)


def test_destruction_acceptee_avec_sauvegarde():
    assert P.ensure_destroyable(9900, 9900, 9999, ["fossor"], P.OWNER_MARKER, SAVE, 1_000_000)


# ── Simulation = zéro exécution ──────────────────────────────────────
async def _boom(*args, **kwargs):
    raise AssertionError("aucune commande ne doit être exécutée en simulation")


@pytest.mark.parametrize("action", ["create", "destroy", "shutdown", "stop", "inspect",
                                    "detect", "list", "cleanup"])
async def test_simulation_zero_execution(monkeypatch, action):
    monkeypatch.setattr(PR, "RUNNER", _boom)
    m = model()
    if action == "create":
        out = await PR.create(None, m, "fossor-test-pve", VMID, ssh_public_key=KEY,
                              private_key="clé", simulated=True)
    elif action == "destroy":
        out = await PR.destroy(None, m, VMID, backup=SAVE, now_epoch=1_000_000,
                               max_age_days=7, simulated=True)
    elif action == "shutdown":
        out = await PR.shutdown(None, VMID, simulated=True)
    elif action == "stop":
        out = await PR.stop(None, VMID, simulated=True)
    elif action == "inspect":
        out = await PR.inspect(None, simulated=True, model=m)
    elif action == "detect":
        out = await PR.detect(None, simulated=True)
    elif action == "list":
        out = await PR.list_guests(None, simulated=True)
    else:
        out = await PR.cleanup(None, simulated=True)
    assert out["simulated"] is True
    assert out["commands"]
    assert "aucune commande" in out["note"]


async def test_destruction_sans_sauvegarde_ne_contacte_pas_l_hote(monkeypatch):
    monkeypatch.setattr(PR, "RUNNER", _boom)
    with pytest.raises(P.VmError, match="sauvegarde"):
        await PR.destroy(None, model(), VMID, backup=None, now_epoch=1_000_000,
                         max_age_days=7, simulated=False)


async def test_creation_refuse_vmid_hors_plage(monkeypatch):
    monkeypatch.setattr(PR, "RUNNER", _boom)
    with pytest.raises(PR.PveRemoteError, match="plage"):
        await PR.create(None, model(), "fossor-test-pve", 100, ssh_public_key=KEY,
                        simulated=False)


async def test_destruction_refuse_une_vm_sans_tag(monkeypatch):
    async def runner(host, command, stdin=None, timeout=180):
        return {"status": 0, "stdout": CONFIG.replace("tags: fossor", "tags: autre"),
                "stderr": "", "ok": True}

    monkeypatch.setattr(PR, "RUNNER", runner)
    with pytest.raises(P.VmError, match="tag"):
        await PR.destroy(None, model(), VMID, backup=SAVE, now_epoch=1_000_000,
                         max_age_days=7, simulated=False)


async def test_destruction_executee_apres_controles(monkeypatch):
    seen = []

    async def runner(host, command, stdin=None, timeout=180):
        seen.append(command)
        return {"status": 0, "stdout": CONFIG if command.startswith("qm config") else "",
                "stderr": "", "ok": True}

    monkeypatch.setattr(PR, "RUNNER", runner)
    out = await PR.destroy(None, model(), VMID, backup=SAVE, now_epoch=1_000_000,
                           max_age_days=7, simulated=False)
    assert out["destroyed"] is True
    assert seen[0].startswith("qm config 9900")
    assert "qm destroy 9900 --purge --destroy-unreferenced-disks 1" in seen
    # Le snippet cloud-init part avec la VM, puis le cache du pilote (option
    # « garder l'image en cache » désactivée par défaut).
    assert any("fossor-user-9900.yaml" in command for command in seen)
    assert seen[-1].startswith("rm -rf ")
