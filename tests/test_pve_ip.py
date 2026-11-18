"""Correctif d'IP des VM Proxmox (§4.6) : agent invité, IPv6 lien-local, image poussée.

Les charges de test reprennent la **forme réelle** des sorties relevées sur un
nœud Proxmox 9.2.2 lors de l'essai du pilote (``qm guest cmd``, ``ip neigh``,
``ip -6 neigh``, ``ip -4 -o addr``, échec ``curl`` de résolution) ; les adresses
et les MAC sont **anonymisées** (plages de documentation RFC 5737).
"""

from __future__ import annotations

import base64

import pytest

from app import pve_remote as PR
from app import pve_vm as P

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFossorTestKeyOnly000000000000000000 fossor@test"
VMID = 9900
MAC = "bc:24:11:00:00:01"
LINK_LOCAL = "fe80::be24:11ff:fe00:0001"

# ── Relevés réels, anonymisés ────────────────────────────────────────
AGENT_JSON = (
    '[{"name":"lo","hardware-address":"00:00:00:00:00:00","ip-addresses":['
    '{"ip-address-type":"ipv4","ip-address":"127.0.0.1","prefix":8},'
    '{"ip-address-type":"ipv6","ip-address":"::1","prefix":128}]},'
    '{"name":"ens18","hardware-address":"bc:24:11:00:00:01","ip-addresses":['
    '{"ip-address-type":"ipv6","ip-address":"fe80::be24:11ff:fe00:0001","prefix":64},'
    '{"ip-address-type":"ipv4","ip-address":"192.0.2.31","prefix":24}]}]'
)
AGENT_SILENT = "QEMU guest agent is not running"
NEIGH_V4_SANS_MAC = (
    "192.0.2.1 dev vmbr0 lladdr aa:bb:cc:00:00:01 REACHABLE\n"
    "192.0.2.2 dev vmbr0 lladdr aa:bb:cc:00:00:02 STALE\n"
)
NEIGH_V4_AVEC_MAC = NEIGH_V4_SANS_MAC + f"192.0.2.31 dev vmbr0 lladdr {MAC} REACHABLE\n"
NEIGH_V6 = (
    f"{LINK_LOCAL} dev vmbr0 lladdr {MAC} router STALE\n"
    "fe80::1 dev vmbr0 lladdr aa:bb:cc:00:00:02 router REACHABLE\n"
)
ADDR_OUT = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
    "2: ens18    inet 192.0.2.31/24 brd 192.0.2.255 scope global dynamic ens18\\"
    "       valid_lft 86390sec preferred_lft 86390sec\n"
)
CURL_DNS = "curl: (6) Could not resolve host: cloud.debian.org"
CONFIG = (
    "boot: order=scsi0\n"
    "scsi0: data2-lvm:vm-9900-disk-0,size=8G\n"
    "net0: virtio=BC:24:11:00:00:01,bridge=vmbr0\n"
    "tags: fossor\n"
    "description: fossor_vm=1 fossor-test-pve\n"
)


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


# ── Chemin 1 : agent invité ──────────────────────────────────────────
def test_agent_invite_donne_ipv4():
    assert P.parse_guest_ip(AGENT_JSON) == "192.0.2.31"


def test_agent_muet_rend_vide():
    assert P.parse_guest_ip(AGENT_SILENT) == ""
    assert P.parse_guest_ip("") == ""


def test_cloud_init_installe_et_active_l_agent():
    data = P.userdata_yaml("fossor-test-pve", KEY)
    assert data.startswith("#cloud-config")
    assert "packages:\n  - qemu-guest-agent" in data
    assert "runcmd:\n  - systemctl enable --now qemu-guest-agent" in data
    assert f"- name: {P.VM_USER}" in data
    assert f"- {KEY}" in data


def test_plan_de_creation_active_l_agent_et_le_snippet():
    plan = P.create_plan(model(), "fossor-test-pve", VMID, KEY)
    joined = "\n".join(step.command() for step in plan)
    assert "--agent enabled=1" in joined
    assert "--cicustom user=local:snippets/fossor-user-9900.yaml" in joined
    snippets = [s for s in plan if s.stdin and "qemu-guest-agent" in s.stdin]
    assert len(snippets) == 1
    assert "pvesh get" in snippets[0].command()
    assert "snippets" in snippets[0].command()


# ── Chemin 2 : IPv6 lien-local ───────────────────────────────────────
def test_voisins_ipv6_rendent_l_adresse_lien_local():
    assert P.parse_ipv6_neigh(NEIGH_V6, MAC) == LINK_LOCAL
    assert P.parse_ipv6_neigh(NEIGH_V6, MAC.upper()) == LINK_LOCAL
    assert P.parse_ipv6_neigh(NEIGH_V6, "bc:24:11:00:00:09") == ""


def test_arp_ipv4_vide_ne_rend_rien():
    assert P.parse_ip_neigh(NEIGH_V4_SANS_MAC, MAC) == ""
    assert P.parse_ip_neigh(NEIGH_V4_AVEC_MAC, MAC) == "192.0.2.31"


def test_ip_addr_rend_l_ipv4_globale():
    assert P.parse_addr_ipv4(ADDR_OUT) == "192.0.2.31"
    assert P.parse_addr_ipv4("") == ""


def test_ssh_lien_local_ajoute_6_et_le_scope():
    argv = P.ssh_vm_argv(LINK_LOCAL, ("ip", "-4", "-o", "addr", "show"), "vmbr0")
    assert argv[0] == "ssh"
    assert "-6" in argv
    assert f"{P.VM_USER}@{LINK_LOCAL}%vmbr0" in argv
    with pytest.raises(P.VmError, match="bridge"):
        P.ssh_vm_argv(LINK_LOCAL, ("ip", "addr"), "")


async def test_wait_address_replie_sur_le_lien_local(monkeypatch):
    seen = []

    async def runner(host, command, stdin=None, timeout=180):
        seen.append(command)
        if command.startswith("qm config"):
            return {"status": 0, "stdout": CONFIG, "stderr": "", "ok": True}
        if command.startswith("qm guest cmd"):
            return {"status": 1, "stdout": AGENT_SILENT, "stderr": "", "ok": True}
        if command == "ip neigh show":
            return {"status": 0, "stdout": NEIGH_V4_SANS_MAC, "stderr": "", "ok": True}
        if command == "ip -6 neigh show":
            return {"status": 0, "stdout": NEIGH_V6, "stderr": "", "ok": True}
        if "ssh-keyscan" in command:
            return {"status": 0, "stdout": "HOSTKEY:ok", "stderr": "", "ok": True}
        if command.startswith("ssh"):
            return {"status": 0, "stdout": ADDR_OUT, "stderr": "", "ok": True}
        return {"status": 0, "stdout": "", "stderr": "", "ok": True}

    monkeypatch.setattr(PR, "RUNNER", runner)
    out = await PR.wait_address(None, VMID, simulated=False, attempts=1, pause=0)
    assert out["ip"] == "192.0.2.31"
    assert out["link_local"] == LINK_LOCAL
    assert out["source"] == "SSH sur adresse lien-local"
    assert any("ssh-keyscan" in command for command in seen)
    ssh = [command for command in seen if command.startswith("ssh ")]
    assert ssh and LINK_LOCAL + "%vmbr0" in ssh[0]


async def test_wait_address_utilise_l_agent_avant_tout_repli(monkeypatch):
    seen = []

    async def runner(host, command, stdin=None, timeout=180):
        seen.append(command)
        if command.startswith("qm config"):
            return {"status": 0, "stdout": CONFIG, "stderr": "", "ok": True}
        if command.startswith("qm guest cmd"):
            return {"status": 0, "stdout": AGENT_JSON, "stderr": "", "ok": True}
        return {"status": 0, "stdout": "", "stderr": "", "ok": True}

    monkeypatch.setattr(PR, "RUNNER", runner)
    out = await PR.wait_address(None, VMID, simulated=False, attempts=1, pause=0)
    assert out["source"] == "agent invité"
    assert not any("ip neigh" in command for command in seen)


async def test_wait_address_sans_aucune_trace_leve(monkeypatch):
    async def runner(host, command, stdin=None, timeout=180):
        stdout = CONFIG if command.startswith("qm config") else ""
        return {"status": 0, "stdout": stdout, "stderr": "", "ok": True}

    monkeypatch.setattr(PR, "RUNNER", runner)
    with pytest.raises(PR.PveRemoteError, match="voisins IPv6"):
        await PR.wait_address(None, VMID, simulated=False, attempts=1, pause=0)


# ── Chemin 3 : image poussée par Fossor quand l'hôte ne résout pas ───
def test_echec_dns_reconnu():
    assert P.image_error_is_dns(f"code 4 — {CURL_DNS}") is True
    assert P.image_error_is_dns("Temporary failure in name resolution") is True
    assert P.image_error_is_dns("code 5 — image: FAILED") is False
    assert P.image_url_host("https://cloud.debian.org/x/y.qcow2") == "cloud.debian.org"


async def test_creation_pousse_l_image_quand_l_hote_ne_resout_pas(monkeypatch, tmp_path):
    m = model()
    local = tmp_path / "debian-12-genericcloud-amd64.qcow2"
    local.write_bytes(b"x" * 5000)
    pushed = []

    async def fake_download(model_arg):
        return str(local)

    async def runner(host, command, stdin=None, timeout=180):
        if "curl -fsSL" in command:
            return {"status": 4, "stdout": "", "stderr": CURL_DNS, "ok": True}
        if "base64 -d" in command:
            assert stdin
            pushed.append(base64.b64decode(stdin))
            return {"status": 0, "stdout": "PUSH:chunk", "stderr": "", "ok": True}
        if 'sum" -c -' in command:
            return {"status": 0, "stdout": f"{m['checksum']}  {P.image_path(m)}: OK",
                    "stderr": "", "ok": True}
        if command.startswith("qm status"):
            return {"status": 0, "stdout": "status: running", "stderr": "", "ok": True}
        return {"status": 0, "stdout": "", "stderr": "", "ok": True}

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(PR, "download_image", fake_download)
    out = await PR.create(None, m, "fossor-test-pve", VMID, ssh_public_key=KEY,
                          simulated=False)
    assert out["state"] == "running"
    assert out["image_source"] == "envoyée par Fossor"
    assert "ne résout pas cloud.debian.org" in out["note"]
    assert b"".join(pushed) == local.read_bytes()


async def test_creation_sans_echec_dns_ne_pousse_pas(monkeypatch):
    sent = []

    async def runner(host, command, stdin=None, timeout=180):
        sent.append(command)
        if command.startswith("qm status"):
            return {"status": 0, "stdout": "status: running", "stderr": "", "ok": True}
        return {"status": 0, "stdout": "IMAGE:present", "stderr": "", "ok": True}

    async def boom(model_arg):
        raise AssertionError("aucune image ne doit être téléchargée côté Fossor")

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(PR, "download_image", boom)
    out = await PR.create(None, model(), "fossor-test-pve", VMID, ssh_public_key=KEY,
                          simulated=False)
    assert out["image_source"] == "téléchargée par l'hôte"
    assert out["note"] == ""
    assert not any("base64 -d" in command for command in sent)


async def test_copie_refusee_si_la_somme_differe(monkeypatch, tmp_path):
    m = model()
    local = tmp_path / "img.qcow2"
    local.write_bytes(b"y" * 10)

    async def runner(host, command, stdin=None, timeout=180):
        if 'sum" -c -' in command:
            return {"status": 1, "stdout": "FAILED", "stderr": "", "ok": True}
        return {"status": 0, "stdout": "", "stderr": "", "ok": True}

    monkeypatch.setattr(PR, "RUNNER", runner)
    with pytest.raises(PR.PveRemoteError, match="non confirmée"):
        await PR.push_image(None, m, str(local))


def test_copie_decoupee_en_fragments():
    assert PR.PUSH_CHUNK >= 1024 * 1024
