"""Intégration Proxmox de bout en bout : faux nœud + vraie cible SSH (§4.6).

Le nœud Proxmox est **factice** (``qm``, ``pvesh``, ``pvesm``, ``pveversion``
et ``ssh-keyscan`` bouchonnés dans un ``PATH`` temporaire) ; la VM est une
**vraie cible SSH jetable** (conteneur ``fossor-sshd:test``) : les commandes
``provision``/mineur/collecte/sauvegarde y passent par une vraie connexion
SSH, avec la clé Fossor et l'hôte épinglé.

La campagne tourne **simulation OFF** : création (VMID choisi dans la plage),
adresse, provisionnement (mineur téléchargé et SHA256 vérifié), démarrage
d'un faux mineur, ``ps`` à chaque étape, arrêt, sauvegarde vérifiée, puis
extinction et destruction.

    docker network create fossor-t-pve
    docker run -d --rm --name fossor-ssh-pve --network fossor-t-pve \\
      --network-alias fossor-ssh-pve fossor-sshd:test
    docker run --rm --network fossor-t-pve --name fossor-pve-test \\
      -v "$PWD:/src:ro" -w /src -e PYTHONPATH=/src \\
      -e FOSSOR_TEST_SSH_HOST=fossor-ssh-pve -e FOSSOR_TEST_SSH_PORT=2222 \\
      -e FOSSOR_TEST_SSH_USER=tester -e FOSSOR_TEST_SSH_PASSWORD=testerpass \\
      -e FOSSOR_TEST_HTTP_HOST=fossor-pve-test \\
      fossor-app:test python -m pytest tests/test_pve_campaigns_integration.py -s -q
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import io
import json
import os
import shlex
import socket
import tarfile
import threading
import uuid as uuid_lib
from datetime import timedelta
from pathlib import Path

import pytest

SSH_HOST = os.environ.get("FOSSOR_TEST_SSH_HOST", "")
SSH_PORT = int(os.environ.get("FOSSOR_TEST_SSH_PORT", "2222"))
SSH_USER = os.environ.get("FOSSOR_TEST_SSH_USER", "tester")
SSH_PASSWORD = os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass")
HTTP_HOST = os.environ.get("FOSSOR_TEST_HTTP_HOST", socket.gethostname())

pytestmark = pytest.mark.skipif(
    not SSH_HOST, reason="cible SSH jetable absente (FOSSOR_TEST_SSH_HOST)"
)

STUBS = {
    "qm": '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["FOSSOR_STUB_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
args = sys.argv[1:]
cmd = args[0] if args else ""

def save():
    state_path.write_text(json.dumps(state, indent=1))

def pairs(items):
    out = {}
    i = 0
    while i < len(items):
        key = items[i]
        if key.startswith("--"):
            value = items[i + 1] if i + 1 < len(items) and not items[i + 1].startswith("--") else "1"
            out[key[2:]] = value
            i += 2
        else:
            i += 1
    return out

if cmd == "create":
    vmid = args[1]
    opts = pairs(args[2:])
    state[vmid] = {
        "name": opts.get("name", "?"), "status": "stopped",
        "tags": opts.get("tags", "fossor"),
        "description": opts.get("description", "fossor_vm=1"),
        "mac": "AA:BB:CC:DD:EE:99", "scsi0": "local-lvm:vm-%s-disk-0" % vmid,
    }
    save()
elif cmd == "set":
    vmid = args[1]
    opts = pairs(args[2:])
    entry = state.setdefault(vmid, {"status": "stopped", "tags": "fossor",
                                    "description": "fossor_vm=1", "name": "?"})
    for key in ("tags", "description", "name", "scsi0"):
        if key in opts:
            entry[key] = opts[key]
    save()
elif cmd in ("start", "stop", "shutdown"):
    vmid = args[1]
    state.setdefault(vmid, {})["status"] = "running" if cmd == "start" else "stopped"
    save()
elif cmd == "status":
    vmid = args[1]
    print("status: " + state.get(vmid, {}).get("status", "stopped"))
elif cmd == "config":
    vmid = args[1]
    entry = state.get(vmid, {})
    print("name: " + entry.get("name", "?"))
    print("tags: " + entry.get("tags", ""))
    print("description: " + entry.get("description", ""))
    print("scsi0: " + entry.get("scsi0", "local-lvm:vm-%s-disk-0" % vmid))
    print("net0: virtio=%s,bridge=vmbr0" % entry.get("mac", "AA:BB:CC:DD:EE:99"))
elif cmd == "guest":
    print(json.dumps([{"name": "eth0", "ip-addresses": [
        {"ip-address": os.environ["FOSSOR_STUB_VM_IP"], "ip-address-type": "ipv4", "prefix": 16}
    ]}]))
elif cmd == "destroy":
    state.pop(args[1], None)
    save()
elif cmd == "list":
    print("      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID")
    for vmid, entry in state.items():
        print("      %s %s %s 2048 20 0" % (vmid, entry.get("name", "?"), entry.get("status", "stopped")))
else:
    sys.stderr.write("qm: commande inconnue " + cmd + "\\n")
    sys.exit(2)
''',
    "pvesh": '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
path = args[1] if args and args[0] == "get" else ""
storages = [
    {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup,snippets", "path": os.environ["FOSSOR_STUB_VZ"]},
    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"},
]
if path == "/storage":
    print(json.dumps(storages))
elif path.startswith("/storage/"):
    name = path.rsplit("/", 1)[-1]
    found = next((s for s in storages if s["storage"] == name), {})
    if found.get("path"):
        Path(found["path"]).mkdir(parents=True, exist_ok=True)
    print(json.dumps(found, separators=(",", ":")))
elif path == "/cluster/resources":
    print("[]")
else:
    print("{}")
''',
    "pvesm": '''#!/bin/bash
echo "Name             Type     Status           Total            Used       Available        %"
echo "local             dir     active        1000000             1          999999    0.00%"
echo "local-lvm     lvmthin     active        2000000             1         1999999    0.00%"
''',
    "pveversion": '''#!/bin/bash
echo "pve-manager/8.2.4/abfc1b4a5e21bd18 (running kernel: 6.8.4-2-pve)"
''',
    "ssh-keyscan": '''#!/usr/bin/env python3
import asyncio, os, sys
import asyncssh

args = [a for a in sys.argv[1:] if not a.startswith("-") and not a.isdigit()]
host = args[-1]
port = int(os.environ.get("FOSSOR_TEST_SSH_PORT", "2222"))

async def main():
    key = await asyncssh.get_server_host_key(host, port)
    blob = key.export_public_key().decode().split()
    print(f"{host} {blob[0]} {blob[1]}")

asyncio.run(main())
''',
    "ssh": '''#!/usr/bin/env python3
import asyncio, os, shlex, sys
import asyncssh

args = sys.argv[1:]
key = None
known = None
host = None
rest = []
i = 0
while i < len(args):
    item = args[i]
    if item in ("-i", "-p"):
        if item == "-i":
            key = args[i + 1]
        i += 2
    elif item == "-o":
        opt = args[i + 1]
        if opt.startswith("UserKnownHostsFile="):
            known = opt.split("=", 1)[1]
        i += 2
    elif item in ("-6",):
        i += 1
    elif item == "-l":
        i += 2
    elif item.startswith("-"):
        i += 1
    else:
        host = item
        rest = args[i + 1:]
        break

if not host:
    sys.stderr.write("ssh: hote absent\\n")
    sys.exit(2)
user, _, target = host.partition("@")
port = int(os.environ.get("FOSSOR_TEST_SSH_PORT", "2222"))

def pinned():
    if not known or not os.path.exists(known):
        return None
    lines = [line.split() for line in open(known).read().splitlines() if line.strip()]
    return lines[-1][2] if lines and len(lines[-1]) >= 3 else None

async def main():
    expected = pinned()
    if not expected:
        sys.stderr.write("ssh: aucune cle d'hote epinglee\\n")
        return 255
    async with asyncssh.connect(
        target, port=port, username=user, client_keys=[key] if key else None,
        known_hosts=None, connect_timeout=10, login_timeout=20,
    ) as conn:
        server = conn.get_server_host_key()
        if server.export_public_key().decode().split()[1] != expected:
            sys.stderr.write("ssh: cle d'hote differente de la cle epinglee\\n")
            return 255
        result = await conn.run(shlex.join(rest), check=False)
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        return result.exit_status

sys.exit(asyncio.run(main()))
''',
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


async def _guest(script: str) -> str:
    """Commande exécutée sur la cible jetable (compte de test, sans la clé)."""
    import asyncssh

    async with asyncssh.connect(
        SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASSWORD,
        known_hosts=None,
    ) as conn:
        result = await conn.run(script, check=False)
        return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import asyncssh

    from app import fossor_keys
    from app import pve_remote as PR
    from app import pve_vm as P
    from app import spec_vm as V

    # ── Contenu servi : image cloud et archive du mineur ──────────────
    files = tmp_path / "served"
    files.mkdir()
    image = files / "debian-12-genericcloud-amd64.qcow2"
    image.write_bytes(b"contenu-qcow2-bouchon" * 32)
    image_sha = hashlib.sha256(image.read_bytes()).hexdigest()

    archive = files / "mineur.tar.gz"
    payload = b"#!/bin/sh\nwhile true; do sleep 5; done\n"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("mineur")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    miner_sha = hashlib.sha256(archive.read_bytes()).hexdigest()

    port = _free_port()
    handler = lambda *a, **kw: _Handler(*a, directory=str(files), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://{HTTP_HOST}:{port}"

    # ── Clé Fossor : la même pour la fabrication et le SSH de la VM ───
    key = asyncssh.generate_private_key("ssh-ed25519")
    private_text = key.export_private_key().decode()
    public_text = key.export_public_key().decode().strip()
    monkeypatch.setattr(fossor_keys, "private_key_text", lambda: private_text)
    monkeypatch.setattr(fossor_keys, "public_key_text", lambda: public_text)

    # Cible jetable : clé Fossor autorisée (image dérivée avec sudo NOPASSWD).
    setup = asyncio.run(
        _guest(
            "install -d -m 0700 ~/.ssh && "
            f"printf '%s\\n' {shlex.quote(public_text)} >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys && "
            "sudo -n pkill -f \"[m]ineur --wallet\" 2>/dev/null; "
            "sudo -n rm -f /opt/fossor/run/*.pid; echo SETUP_OK"
        )
    )
    assert "SETUP_OK" in setup, setup

    # ── Nœud Proxmox factice ──────────────────────────────────────────
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in STUBS.items():
        path = stubs / name
        path.write_text(body)
        path.chmod(0o755)
    state = tmp_path / "qm-state.json"
    state.write_text("{}")

    env = dict(os.environ)
    env["PATH"] = f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    env["FOSSOR_STUB_STATE"] = str(state)
    env["FOSSOR_STUB_VM_IP"] = socket.gethostbyname(SSH_HOST)
    env["FOSSOR_STUB_VZ"] = str(tmp_path / "vz")
    env["FOSSOR_TEST_SSH_PORT"] = str(SSH_PORT)

    async def runner(host, command, stdin=None, timeout=60):
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None),
                timeout=max(5, int(timeout)),
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "status": 124, "stdout": "", "stderr": "timeout"}
        return {
            "ok": True,
            "status": proc.returncode,
            "stdout": out.decode("utf-8", "replace"),
            "stderr": err.decode("utf-8", "replace"),
        }

    from app import pve_remote as PR2

    monkeypatch.setattr(PR2, "RUNNER", runner)
    monkeypatch.setattr(P, "KEY_PATH", str(tmp_path / "id_fossor_vm"))
    monkeypatch.setattr(P, "KNOWN_HOSTS", str(tmp_path / "known_hosts.fossor_vm"))
    monkeypatch.setattr(P, "PVE_TMP_DIR", str(tmp_path / "pve"))
    monkeypatch.setattr(P, "IMAGE_DIR", str(tmp_path / "pve" / "images"))
    monkeypatch.setattr(P, "PUBKEY_PATH", str(tmp_path / "pve" / "fossor.pub"))
    monkeypatch.setattr(P, "VM_USER", SSH_USER)
    monkeypatch.setattr(V, "VM_USER", SSH_USER)
    monkeypatch.setattr(V, "INSTALL_DIR", f"/home/{SSH_USER}/opt")
    monkeypatch.setattr(V, "DATA_DIR", f"/home/{SSH_USER}/data")
    monkeypatch.setattr(V, "LOG_DIR", f"/home/{SSH_USER}/logs")
    monkeypatch.setattr(V, "RUN_DIR", f"/home/{SSH_USER}/run")

    yield {
        "state": state,
        "base": base,
        "image": image,
        "image_sha": image_sha,
        "archive": archive,
        "miner_sha": miner_sha,
        "runner": runner,
        "P": P,
        "V": V,
        "PR": PR,
    }
    server.shutdown()


def _seed(db, harness, *, campaign_id=None):
    from app import campaigns as C
    from app.ai_config import set_setting
    from app import vm_backend as VB
    from app.models import Campaign, CampaignDevice, Device, Machine, MinerProfile
    from app.routers.pve import PVE_MODEL_KEY

    host = Machine(
        uuid=str(uuid_lib.uuid4()),
        name="pve-jetable",
        host=SSH_HOST,
        ssh_port=SSH_PORT,
        ssh_user="root",
        auth_type="fossor_key",
        os_kind="proxmox",
        enabled=True,
        usage="cpu",
        vm_host=True,
    )
    db.add(host)
    db.flush()
    profile = MinerProfile(
        uuid=str(uuid_lib.uuid4()),
        name="Campagne PVE intégration",
        coin="NNN",
        wallet="demo1pna00000000000000000000000000000000000",
        pool="solo",
        software="custom",
        binary_url=f"{harness['base']}/mineur.tar.gz",
        sha256=harness["miner_sha"],
        binary_path="mineur",
        version="1.0.0",
        cmdline="/opt/fossor/miners/custom/1.0.0/mineur --wallet {wallet} --threads {threads}",
        threads=2,
        execution_mode="vm",
        enabled=True,
    )
    db.add(profile)
    db.flush()
    device = Device(
        uuid=str(uuid_lib.uuid4()),
        machine_id=host.id,
        kind="cpu",
        model="CPU jetable",
        threads=4,
        present=True,
        enabled_for_mining=True,
    )
    db.add(device)
    db.flush()
    now = C.now_utc()
    campaign = Campaign(
        uuid=str(uuid_lib.uuid4()),
        coin="NNN",
        profile_id=profile.id,
        start_at=C.iso(now - timedelta(seconds=5)),
        end_at=C.iso(now + timedelta(minutes=10)),
        status="approved",
        origin="manual",
        execution_mode="vm",
    )
    db.add(campaign)
    db.flush()
    db.add(
        CampaignDevice(
            campaign_id=campaign.id,
            machine_id=host.id,
            device_uuid=device.uuid,
            kind="cpu",
            label="CPU — CPU jetable",
        )
    )
    model = harness["P"].validate_pve_model(
        {
            "host_id": host.id,
            "node": "pve",
            "disk_storage": "local-lvm",
            "ci_storage": "local",
            "bridge": "vmbr0",
            "vmid_min": 9900,
            "vmid_max": 9909,
            "vcpus": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "image_url": f"{harness['base']}/debian-12-genericcloud-amd64.qcow2",
            "checksum": harness["image_sha"],
            "checksum_algo": "sha256",
            "wallet_backup_path": f"/home/{SSH_USER}/data/wallet.dat",
        }
    )
    set_setting(db, PVE_MODEL_KEY, model)
    set_setting(db, VB.SPEC_HOST_KEY, host.id)
    db.commit()
    return campaign, host, model


def _journal(db, campaign_id):
    from app.models import CampaignEvent

    rows = (
        db.query(CampaignEvent)
        .filter(CampaignEvent.campaign_id == campaign_id)
        .order_by(CampaignEvent.id.asc())
        .all()
    )
    print("\n### JOURNAL DE CAMPAGNE")
    for row in rows:
        print(f"{row.kind:<22} {row.result:<5} simulated={row.simulated} {row.message}")
        if row.detail:
            print(f"    detail: {row.detail[:600]}")
    return rows


class TestCampagneProxmoxReelle:
    async def test_creation_ip_mineur_sauvegarde_extinction_destruction(self, harness):
        from app import campaign_runner as RUN
        from app import vm_backend as VB
        from app.models import Campaign, SpecVm

        db = _db()
        try:
            campaign, host, model = _seed(db, harness)
            backend = VB.PveBackend()

            print("\n### ETAPE 1 — création (simulation OFF)")
            await RUN.tick(db, now=RUN.C.now_utc(), simulated=False)
            db.refresh(campaign)
            print(f"campagne #{campaign.id} statut={campaign.status}")
            assert campaign.status == "running", campaign.note

            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            print(f"VM {vm.name} vmid={vm.vmid} ip={vm.ip} hypervisor={vm.hypervisor}")
            assert vm.hypervisor == "proxmox"
            assert 9900 <= int(vm.vmid) <= 9909
            assert vm.ip == socket.gethostbyname(SSH_HOST)
            assert vm.state == "running"

            state = json.loads(harness["state"].read_text())
            print("### qm state:", json.dumps(state, indent=1))
            assert str(vm.vmid) in state
            assert state[str(vm.vmid)]["status"] == "running"
            assert "fossor" in state[str(vm.vmid)]["tags"]
            assert "fossor_vm=1" in state[str(vm.vmid)]["description"]

            print("\n### ETAPE 2 — ps dans la VM après démarrage")
            collected = await backend.collect(host, model, vm, None, False)
            print("qm status:", collected["state"])
            for row in collected["processes"]:
                print(f"  {row['pid']} {row['args']}")
            assert any("/opt/" in row["args"] for row in collected["processes"]), collected

            # Le wallet vit dans la VM : seule sa taille et sa date sont lues.
            print("\n### ETAPE 3 — sauvegarde du wallet (stat seulement)")
            await _guest(f"install -d -m 0755 /home/{SSH_USER}/data && printf wallet > /home/{SSH_USER}/data/wallet.dat")
            verdict = await backend.check_backup(
                host, model, vm, f"/home/{SSH_USER}/data/wallet.dat",
                now_epoch=int(RUN.C.now_utc().timestamp()), max_age_days=7, simulated=False,
            )
            print("verdict:", json.dumps(verdict))
            assert verdict["verified"] is True

            # Destruction refusée sans sauvegarde vérifiée.
            with pytest.raises(Exception) as refused:
                await backend.destroy(
                    host, model, vm, backup=None,
                    now_epoch=int(RUN.C.now_utc().timestamp()), max_age_days=7,
                    simulated=False,
                )
            print("destruction sans verdict:", refused.value)
            assert json.loads(harness["state"].read_text()).get(str(vm.vmid))

            print("\n### ETAPE 4 — fin de campagne (arrêt, VM éteinte et conservée)")
            campaign.end_at = RUN.C.iso(RUN.C.now_utc() - timedelta(seconds=1))
            db.commit()
            await RUN.tick(db, now=RUN.C.now_utc(), simulated=False)
            db.refresh(campaign)
            vm = db.query(SpecVm).filter(SpecVm.campaign_id == campaign.id).one()
            print("statut campagne:", campaign.status, "| VM:", vm.state)

            print("\n### ETAPE 5 — ps après arrêt du mineur")
            collected = await backend.collect(host, model, vm, None, False)
            print("qm status:", collected["state"])
            for row in collected["processes"]:
                print(f"  {row['pid']} {row['args']}")
            assert not any("/opt/" in row["args"] for row in collected["processes"])

            print("\n### ETAPE 6 — destruction après sauvegarde vérifiée")
            destroyed = await backend.destroy(
                host, model, vm, backup=verdict,
                now_epoch=int(RUN.C.now_utc().timestamp()), max_age_days=7, simulated=False,
            )
            print("resultat:", json.dumps({k: v for k, v in destroyed.items() if k != "commands"}))
            assert destroyed["destroyed"] is True
            assert str(vm.vmid) not in json.loads(harness["state"].read_text())

            _journal(db, campaign.id)
        finally:
            db.close()


def _db():
    from app.database import SessionLocal

    return SessionLocal()
