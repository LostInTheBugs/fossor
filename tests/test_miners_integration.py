"""Intégration réelle de l'étape 3a — faux mineur servi en local, cible jetable.

Rien n'est contacté en dehors du réseau Docker ``fossor-test`` : la cible est le
conteneur Ubuntu sshd construit par ``tests/docker/sshd-ubuntu.Dockerfile``
(sans systemd → chemin de repli ``setsid nohup``) et le binaire est un faux
mineur Python servi par un conteneur HTTP local.

Variables attendues (sinon le module est ignoré) :
  FOSSOR_TEST_MINER_HOST   nom du conteneur cible
  FOSSOR_TEST_MINER_URL    URL de l'archive du faux mineur
  FOSSOR_TEST_MINER_SHA    SHA256 de cette archive
  FOSSOR_TEST_SSH_USER / FOSSOR_TEST_SSH_PASSWORD  compte temporaire de provision
"""

from __future__ import annotations

import json
import os

import pytest

HOST = os.environ.get("FOSSOR_TEST_MINER_HOST", "")
PORT = int(os.environ.get("FOSSOR_TEST_MINER_PORT", "2222"))
URL = os.environ.get("FOSSOR_TEST_MINER_URL", "")
SHA = os.environ.get("FOSSOR_TEST_MINER_SHA", "")
TEMP_USER = os.environ.get("FOSSOR_TEST_SSH_USER", "tester")
TEMP_PASSWORD = os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass")
API_PORT = 21399
WALLET = "prl1demo" + "q" * 30

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (HOST and URL and SHA), reason="cible/faux mineur absents"),
]

BODY = {
    "name": "Faux mineur",
    "coin": "FAKE",
    "wallet": WALLET,
    "pool": "stratum+tcp://pool.exemple.tld:3333",
    "software": "custom",
    "binary_url": URL,
    "sha256": SHA,
    "binary_path": "faux-mineur",
    "version": "1.0.0",
    "cmdline": (
        "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} --pool {pool} "
        "--worker {worker} --gpus {gpus} --api-port {api_port}"
    ),
    "api_port": API_PORT,
    "execution_mode": "host",
}


def show(title: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    print(f"\n### {title}\n{text}")


async def _target_command(machine_id: int, command: str) -> str:
    """Run one read-only command on the throwaway target as the fossor account."""
    from app.database import SessionLocal
    from app.models import Machine
    from app.miner_remote import default_runner

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        db.expunge(machine)
    finally:
        db.close()
    result = await default_runner(machine, command, timeout=30)
    return (result.get("stdout") or "") + (result.get("stderr") or "")


async def test_host_mode_end_to_end(client, capsys):
    # ── machine jetable préparée en fossor_key via provision ─────────
    created = await client.post(
        "/api/machines",
        json={
            "name": "cible-3a",
            "host": HOST,
            "ssh_port": PORT,
            "ssh_user": TEMP_USER,
            "auth_type": "password",
            "secret": TEMP_PASSWORD,
            "sudo": "sudo with password",
        },
    )
    assert created.status_code == 200, created.text
    machine_id = created.json()["id"]

    provisioned = await client.post(
        f"/api/machines/{machine_id}/provision",
        json={"temp_user": TEMP_USER, "temp_password": TEMP_PASSWORD, "account": "fossor"},
    )
    assert provisioned.status_code == 200, provisioned.text
    assert provisioned.json()["ok"] is True, provisioned.text
    machine = (await client.get(f"/api/machines/{machine_id}")).json()
    assert machine["auth_type"] == "fossor_key"
    assert machine["ssh_user"] == "fossor"
    show("MACHINE PRÊTE", {k: machine[k] for k in ("name", "host", "ssh_user", "auth_type")})

    # Un GPU coché : {gpus} devient « 0 ».
    from app.database import SessionLocal
    from app.models import Device

    db = SessionLocal()
    try:
        db.add(
            Device(
                uuid="33333333-3333-3333-3333-333333333333",
                machine_id=machine_id,
                kind="gpu",
                index=0,
                model="GPU jetable",
                present=True,
                enabled_for_mining=True,
            )
        )
        db.commit()
    finally:
        db.close()

    profile = (await client.post("/api/miner/profiles", json=BODY)).json()
    assert profile["id"], profile

    # Token par défaut : c'est lui que « start » sans profil explicite exécute.
    defaulted = await client.put("/api/miner/settings", json={"default_profile_id": profile["id"]})
    assert defaulted.json()["default_profile_id"] == profile["id"]

    # ── 1. simulation ON (défaut) : commandes renvoyées, rien n'existe ─
    sim = await client.post(f"/api/machines/{machine_id}/miner/start", json={})
    assert sim.status_code == 200, sim.text
    simulated = sim.json()
    assert simulated["simulated"] is True
    show("START EN SIMULATION", simulated)

    absent = await _target_command(machine_id, "test -d /opt/fossor/miners && echo PRESENT || echo ABSENT")
    show("ARBRE /opt/fossor/miners APRÈS SIMULATION", absent.strip())
    assert "ABSENT" in absent

    # ── 2. simulation OFF (mot de confirmation) puis start réel ───────
    off = await client.put("/api/miner/settings", json={"simulation": False, "confirm": "PILOTER"})
    assert off.json()["simulation"] is False

    started = await client.post(f"/api/machines/{machine_id}/miner/start", json={})
    assert started.status_code == 200, started.text
    real = started.json()
    assert real["simulated"] is False and real["ok"] is True
    show("START RÉEL", real)

    ps = await _target_command(
        machine_id, "ps -eo pid=,args= | grep faux-mineur | grep -v grep || true"
    )
    show("PS APRÈS START", ps.strip())
    assert "faux-mineur" in ps

    status = (await client.get(f"/api/machines/{machine_id}/miner/status")).json()
    show("STATUT", status)
    assert status["status"]["running"] is True
    assert status["status"]["pid"]
    assert status["status"]["hashrate_hs"] and status["status"]["hashrate_hs"] > 0

    # ── 3. arrêt ─────────────────────────────────────────────────────
    stopped = await client.post(f"/api/machines/{machine_id}/miner/stop", json={})
    assert stopped.status_code == 200, stopped.text
    show("STOP", stopped.json())

    ps_after = await _target_command(
        machine_id, "ps -eo pid=,args= | grep faux-mineur | grep -v grep || true"
    )
    show("PS APRÈS STOP", ps_after.strip() or "(vide)")
    assert "faux-mineur" not in ps_after

    # ── 4. mauvais SHA256 : refus, rien d'extrait ────────────────────
    bad = dict(BODY)
    bad.update({"name": "Faux mineur mauvais SHA", "sha256": "f" * 64, "version": "9.9.9",
                "cmdline": BODY["cmdline"].replace("1.0.0", "9.9.9")})
    bad_profile = await client.post("/api/miner/profiles", json=bad)
    assert bad_profile.status_code == 201, bad_profile.text
    refused = await client.post(
        f"/api/machines/{machine_id}/miner/start", json={"profile_id": bad_profile.json()["id"]}
    )
    show("REFUS SHA256", {"status": refused.status_code, "body": refused.json()})
    assert refused.status_code == 409
    assert "SHA256 différent" in refused.json()["detail"]

    extracted = await _target_command(
        machine_id, "ls -la /opt/fossor/miners/custom/9.9.9 2>/dev/null || echo VIDE"
    )
    show("CONTENU DU DOSSIER 9.9.9 APRÈS REFUS", extracted.strip())
    assert "faux-mineur" not in extracted

    leftover = await _target_command(machine_id, "ls /tmp/fossor-*.tar.gz 2>/dev/null || echo AUCUNE")
    show("ARCHIVES TEMPORAIRES RESTANTES", leftover.strip())
    assert "AUCUNE" in leftover

    # ── 5. profil au wallet injecté : refus à la création ────────────
    injected = dict(BODY)
    injected.update({"name": "Injection", "wallet": "x;rm -rf /"})
    rejected = await client.post("/api/miner/profiles", json=injected)
    show("PROFIL WALLET INJECTÉ", {"status": rejected.status_code, "body": rejected.json()})
    assert rejected.status_code == 400
