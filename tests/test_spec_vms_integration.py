"""Intégration « à bouchon » du pilote libvirt (§4.6, étape 3c).

La machine de test n'a **ni /dev/kvm ni libvirt** (et le brief interdit sudo) :
aucune VM réelle n'a donc pu être créée. Ce test exécute les **vraies** commandes
produites par :mod:`app.spec_vm`, avec des bouchons ``qemu-img``, ``cloud-localds``,
``virt-install``, ``virsh``, ``ssh-keyscan`` et ``ssh`` placés en tête de ``PATH``
et un ``sudo`` transparent. Chaque bouchon journalise ses arguments : le journal
prouve l'ordre des commandes, et les effets vérifiables (image téléchargée et
hachée, surcouche créée puis supprimée) sont réels.

Le bouchon ``virsh`` déclare deux VM : ``fossor-…`` (métadonnée ``fossor_vm=1``)
et ``specbox-wam`` (VM étrangère, comme celles du parc de Fred).
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import os
import socket
import threading
from pathlib import Path

import pytest

from app import spec_vm as V
from app import vm_remote as VR

NAME = "fossor-c1-nnn-abcdef"
FOREIGN = "specbox-wam"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBouchonDeClePubliqueFossor fossor@test"
PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----\nbouchon\n-----END OPENSSH PRIVATE KEY-----\n"

STUBS = {
    "sudo": '#!/bin/bash\n[ "$1" = "-n" ] && shift\nexec "$@"\n',
    "qemu-img": """#!/bin/bash
echo "qemu-img $*" >> "$FOSSOR_STUB_LOG"
args=("$@")
printf 'qcow2' > "${args[-2]}"
exit 0
""",
    "cloud-localds": """#!/bin/bash
echo "cloud-localds $*" >> "$FOSSOR_STUB_LOG"
printf 'iso9660' > "$1"
exit 0
""",
    "virt-install": """#!/bin/bash
echo "virt-install $*" >> "$FOSSOR_STUB_LOG"
exit 0
""",
    # Adresses 192.168.122.0/24 : plage NAT libvirt par défaut.
    "virsh": """#!/bin/bash
echo "virsh $*" >> "$FOSSOR_STUB_LOG"
case "$*" in
  *"list --all --name"*) printf 'fossor-c1-nnn-abcdef\\nspecbox-wam\\n' ;;
  *"desc specbox-wam"*) echo "specbox de PCRyzen" ;;
  *"desc fossor-"*) echo "fossor_vm=1" ;;
  *domifaddr*) echo " Name  MAC address  Protocol  Address"; \
               echo " vnet0 52:54:00:aa:bb:cc ipv4 192.168.122.45/24" ;;
  *domstate*) echo "running" ;;
  *dominfo*) echo "CPU(s):        2"; echo "CPU time:      12.5s"; \
             echo "Max memory:    2097152 KiB" ;;
  *version*) echo "Virsh command line tool of libvirt 10.0.0" ;;
esac
exit 0
""",
    "pveversion": "#!/bin/bash\nexit 127\n",
    "ssh-keyscan": """#!/bin/bash
echo "ssh-keyscan $*" >> "$FOSSOR_STUB_LOG"
echo "192.168.122.45 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBouchonDeCleDHote"
exit 0
""",
    "ssh": """#!/bin/bash
echo "ssh $*" >> "$FOSSOR_STUB_LOG"
case "$*" in
  *"ps -eo"*) echo " 4242 /opt/fossor/miners/custom/1.0.0/mineur --wallet demo --pool solo" ;;
  *"curl -s"*) echo '{"hashrate": 1234.5, "algorithm": "cpu"}' ;;
  *"stat -c"*) echo "4096 1700000000" ;;
  *"setsid"*) echo "4242" ;;
  *"sha256sum -c -"*) echo ": OK" ;;
  *"MISSING"*) echo "MISSING" ;;
esac
exit 0
""",
}


def _write_stubs(directory: Path, log: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in STUBS.items():
        path = directory / name
        path.write_text(body)
        path.chmod(0o755)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """PATH bouchonné + racines réécrites + serveur HTTP local pour l'image."""
    files = tmp_path / "served"
    files.mkdir()
    image = files / "base.qcow2"
    image.write_bytes(b"contenu-qcow2-bouchon" * 10)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()

    port = _free_port()
    handler = lambda *a, **kw: _Handler(*a, directory=str(files), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    stubs = tmp_path / "bin"
    log = tmp_path / "stub.log"
    log.write_text("")
    _write_stubs(stubs, log)

    # La sonde KVM est simulée : le conteneur de test n'a pas /dev/kvm. La
    # détection elle-même est couverte par tests/test_energy_3d.py.
    async def fake_probe(host, *, simulated):
        return {"simulated": simulated, "kvm": True, "kvm_device": True,
                "kvm_domain": 1, "pveversion": False, "message": "", "stdout": ""}

    monkeypatch.setattr(VR, "probe_kvm", fake_probe)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.setenv("FOSSOR_STUB_LOG", str(log))
    monkeypatch.setattr(V, "IMAGE_ROOT", str(tmp_path / "libvirt"))
    monkeypatch.setattr(V, "KEY_PATH", str(tmp_path / "id_fossor_vm"))
    monkeypatch.setattr(V, "KNOWN_HOSTS", str(tmp_path / "known_hosts.fossor_vm"))

    async def runner(host, command, stdin=None, timeout=120):
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
        return {
            "status": proc.returncode or 0,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "ok": True,
        }

    monkeypatch.setattr(VR, "RUNNER", runner)

    yield {
        "model": V.validate_vm_model(
            {
                "host_id": 1,
                "keep_image_cache": True,
                "base_image_url": f"http://127.0.0.1:{port}/base.qcow2",
                "base_image_sha256": digest,
                "vcpus": 2,
                "memory_mb": 1024,
                "disk_gb": 5,
                "node_port": 19333,
                "wallet_backup_path": "/home/specminer/data/wallet.dat",
            }
        ),
        "digest": digest,
        "log": log,
        "tmp": tmp_path,
        "stubs": stubs,
        "port": port,
    }
    server.shutdown()


def _log(path: Path) -> str:
    return path.read_text()


class TestPiloteBouchonne:
    async def test_creation_verifie_sha_et_lance_virt_install(self, stubbed):
        model = stubbed["model"]
        result = await VR.create(
            object(), model, NAME, ssh_public_key=KEY, private_key=PRIVATE, simulated=False
        )
        assert result["state"] == "running"
        text = _log(stubbed["log"])
        image = Path(V.IMAGE_ROOT) / "images" / "base.qcow2"
        assert image.exists()
        assert hashlib.sha256(image.read_bytes()).hexdigest() == stubbed["digest"]
        overlay = Path(V.IMAGE_ROOT) / "vms" / NAME / "disk.qcow2"
        assert overlay.exists()
        seed = Path(V.IMAGE_ROOT) / "vms" / NAME / "seed.iso"
        assert seed.exists()
        assert "qemu-img create -f qcow2 -b" in text
        assert "cloud-localds" in text
        assert "virt-install --connect qemu:///system --name " + NAME in text
        assert "--metadata description=fossor_vm=1,title=Fossor" in text
        user_data = (Path(V.IMAGE_ROOT) / "vms" / NAME / "user-data").read_text()
        assert "name: specminer" in user_data and KEY in user_data

    async def test_sha256_different_refuse_la_creation(self, stubbed):
        model = dict(stubbed["model"])
        model["base_image_sha256"] = "0" * 64
        with pytest.raises(VR.VmRemoteError) as exc:
            await VR.create(
                object(), model, NAME, ssh_public_key=KEY, private_key=PRIVATE, simulated=False
            )
        assert "image de base" in str(exc.value)
        assert not (Path(V.IMAGE_ROOT) / "vms" / NAME / "disk.qcow2").exists()

    async def test_adresse_cle_dhote_et_provisionnement(self, stubbed):
        model = stubbed["model"]
        await VR.create(object(), model, NAME, ssh_public_key=KEY, private_key=PRIVATE,
                        simulated=False)
        address = await VR.wait_address(object(), NAME, simulated=False, pause=0)
        assert address["ip"] == "192.168.122.45"
        pinned = await VR.pin_host_key(object(), address["ip"], simulated=False)
        assert pinned["host_key"].startswith("192.168.122.45 ssh-ed25519")
        known = Path(V.KNOWN_HOSTS).read_text()
        assert "192.168.122.45 ssh-ed25519" in known

        profile = {
            "name": "Campagne spec",
            "coin": "NNN",
            "wallet": "nnn1demo" + "q" * 30,
            "pool": "solo",
            "software": "custom",
            "binary_url": f"http://127.0.0.1:{stubbed['port']}/base.qcow2",
            "sha256": stubbed["digest"],
            "binary_path": "mineur",
            "version": "1.0.0",
            "cmdline": "/opt/fossor/miners/custom/1.0.0/mineur --wallet {wallet} --pool {pool}",
            "api_port": 21398,
            "threads": 0,
        }
        provisioned = await VR.provision(
            object(), model, profile, address["ip"], coin="NNN", simulated=False,
            expected_address=profile["wallet"],
        )
        assert provisioned["address"] == profile["wallet"]
        text = stubbed["log"].read_text()
        assert "SHA256 du mineur vérifié dans la VM" not in text  # titres, pas commandes
        assert text.count("sha256sum -c -") >= 1
        assert "specminer@192.168.122.45" in text
        assert "StrictHostKeyChecking=yes" in text
        assert f"UserKnownHostsFile={V.KNOWN_HOSTS}" in text

        # Le mineur démarre dans la VM, avec le wallet du profil.
        started = await VR.start_miner(
            object(), profile, ["/opt/fossor/miners/custom/1.0.0/mineur", "--pool", "solo"],
            address["ip"], simulated=False,
        )
        assert started["pid"] == "4242"
        assert "setsid sh -c" in stubbed["log"].read_text()

        # Collecte : état de la VM, `ps` dans la VM, hashrate de l'API.
        collected = await VR.collect(object(), NAME, address["ip"], profile, simulated=False)
        assert collected["state"] == "running"
        assert collected["hashrate_hs"] == 1234.5
        assert collected["processes"][0]["pid"] == "4242"
        assert collected["vcpus"] == 2 and collected["memory_kb"] == 2097152

        # Le nœud de la VM est l'adresse donnée à {vm_node} pour le mode hybride.
        assert V.node_address(address["ip"], model["node_port"]) == "192.168.122.45:19333"

    async def test_liste_et_refus_des_vm_etrangeres(self, stubbed):
        listing = await VR.list_vms(object(), simulated=False)
        names = {item["name"]: item["owned"] for item in listing["vms"]}
        assert names == {NAME: True, FOREIGN: False}
        with pytest.raises(V.VmError) as exc:
            V.ensure_destroyable(FOREIGN, "specbox de PCRyzen", {"verified": True}, 1700000100)
        assert "préfixe" in str(exc.value)

    async def test_destruction_refusee_puis_acceptee(self, stubbed):
        model = stubbed["model"]
        await VR.create(object(), model, NAME, ssh_public_key=KEY, private_key=PRIVATE,
                        simulated=False)
        overlay = Path(V.IMAGE_ROOT) / "vms" / NAME / "disk.qcow2"
        assert overlay.exists()

        # 1. Sans contrôle de sauvegarde : refus, et **zéro** commande envoyée.
        before = len(stubbed["log"].read_text().splitlines())
        with pytest.raises(V.VmError) as exc:
            await VR.destroy(object(), NAME, V.OWNER_MARKER, backup=None,
                             now_epoch=1700000100, max_age_days=7, simulated=False)
        assert "Destruction refusée" in str(exc.value)
        assert len(stubbed["log"].read_text().splitlines()) == before
        assert overlay.exists()

        # 2. Contrôle réel (stat bouchonné) puis destruction : la surcouche disparaît.
        verdict = await VR.check_backup(
            object(), "192.168.122.45", "/home/specminer/data/wallet.dat",
            now_epoch=1700000100, max_age_days=7, simulated=False,
        )
        assert verdict["verified"] is True
        assert verdict["size"] == 4096
        result = await VR.destroy(object(), NAME, V.OWNER_MARKER, backup=verdict,
                                  now_epoch=1700000100, max_age_days=7, simulated=False)
        assert result["destroyed"] is True
        text = stubbed["log"].read_text()
        assert f"virsh --connect qemu:///system destroy {NAME}" in text
        assert f"virsh --connect qemu:///system undefine {NAME} --nvram" in text
        assert not overlay.exists()
        assert not (Path(V.IMAGE_ROOT) / "vms" / NAME).exists()
        # La VM étrangère n'a jamais été touchée.
        assert FOREIGN not in text

    async def test_extinction_conserve_la_vm(self, stubbed):
        await VR.create(object(), stubbed["model"], NAME, ssh_public_key=KEY,
                        private_key=PRIVATE, simulated=False)
        result = await VR.shutdown(object(), NAME, simulated=False)
        assert result["state"] == "shut off"
        overlay = Path(V.IMAGE_ROOT) / "vms" / NAME / "disk.qcow2"
        assert overlay.exists(), "la VM doit être conservée (le wallet y est)"
        assert "undefine" not in stubbed["log"].read_text()
