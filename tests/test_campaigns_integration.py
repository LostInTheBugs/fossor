"""Intégration réelle des campagnes (§4.7) — cible jetable, simulation désactivée.

Rien n'est contacté hors du réseau Docker ``fossor-test`` : la cible est le
conteneur Ubuntu sshd de ``tests/docker/sshd-ubuntu.Dockerfile`` (sans systemd →
repli ``setsid nohup``) et le binaire est le faux mineur de l'étape 3a, servi
par un conteneur HTTP local.  Les preuves se lisent avec ``ps`` **dans la
cible**, à chaque étape du cycle.

Variables attendues (sinon le module est ignoré) :
  FOSSOR_TEST_MINER_HOST   nom du conteneur cible
  FOSSOR_TEST_MINER_URL    URL de l'archive du faux mineur
  FOSSOR_TEST_MINER_SHA    SHA256 de cette archive
  FOSSOR_TEST_SSH_USER / FOSSOR_TEST_SSH_PASSWORD  compte temporaire de provision
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

HOST = os.environ.get("FOSSOR_TEST_MINER_HOST", "")
PORT = int(os.environ.get("FOSSOR_TEST_MINER_PORT", "2222"))
URL = os.environ.get("FOSSOR_TEST_MINER_URL", "")
SHA = os.environ.get("FOSSOR_TEST_MINER_SHA", "")
TEMP_USER = os.environ.get("FOSSOR_TEST_SSH_USER", "tester")
TEMP_PASSWORD = os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass")
API_A = 21399  # token par défaut
API_B = 21398  # profil de campagne
WALLET = "prl1demo" + "q" * 30
GPU_UUID = "44444444-4444-4444-4444-444444444444"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (HOST and URL and SHA), reason="cible/faux mineur absents"),
]


def show(title: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    print(f"\n### {title}\n{text}")


def profile_body(name: str, worker: str, api_port: int, url: str | None = None) -> dict:
    return {
        "name": name,
        "coin": "FAKE",
        "wallet": WALLET,
        "pool": "stratum+tcp://pool.exemple.tld:3333",
        "software": "custom",
        "binary_url": url or URL,
        "sha256": SHA,
        "binary_path": "faux-mineur",
        "version": "1.0.0",
        "cmdline": (
            "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} --pool {pool} "
            f"--worker {worker} --gpus {{gpus}} --api-port {api_port}"
        ),
        "api_port": api_port,
        "execution_mode": "host",
    }


async def target_command(machine_id: int, command: str) -> str:
    """Commande en lecture seule sur la cible jetable, compte dédié."""
    from app.database import SessionLocal
    from app.miner_remote import default_runner
    from app.models import Machine

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        db.expunge(machine)
    finally:
        db.close()
    result = await default_runner(machine, command, timeout=30)
    return (result.get("stdout") or "") + (result.get("stderr") or "")


async def ps_miner(machine_id: int) -> str:
    return (
        await target_command(
            machine_id,
            "ps -eo pid=,args= | grep faux-mineur | grep -v grep || true",
        )
    ).strip()


async def tick() -> list[dict]:
    from app import campaign_runner as RUN
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return await RUN.tick(db)
    finally:
        db.close()


def campaign_status(campaign_id: int) -> str:
    from app.database import SessionLocal
    from app.models import Campaign

    db = SessionLocal()
    try:
        row = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        db.expunge(row)
        return row.status
    finally:
        db.close()


def journal(campaign_id: int) -> str:
    from app.database import SessionLocal
    from app.models import CampaignEvent

    db = SessionLocal()
    try:
        rows = (
            db.query(CampaignEvent)
            .filter(CampaignEvent.campaign_id == campaign_id)
            .order_by(CampaignEvent.id.asc())
            .all()
        )
        db.expunge_all()
    finally:
        db.close()
    return "\n".join(
        f"{row.ts} {row.actor:<9} {row.kind:<22} {row.result:<4} "
        f"{'simulé' if row.simulated else 'réel  '} {row.message}"
        for row in rows
    )


async def _prepare_target(client) -> tuple[int, int, int]:
    """Machine préparée en ``fossor_key`` + GPU semé + profils A/B en base."""
    created = await client.post(
        "/api/machines",
        json={
            "name": "cible-3b",
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

    from app.database import SessionLocal
    from app.models import Device

    db = SessionLocal()
    try:
        db.add(
            Device(
                uuid=GPU_UUID,
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

    # §5.7 : la simulation est le défaut — on la coupe explicitement pour ce test.
    settings = await client.put(
        "/api/miner/settings", json={"simulation": False, "confirm": "PILOTER"}
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["simulation"] is False

    default_profile = await client.post(
        "/api/miner/profiles", json=profile_body("Token par défaut", "defaut", API_A)
    )
    assert default_profile.status_code == 201, default_profile.text
    campaign_profile = await client.post(
        "/api/miner/profiles", json=profile_body("Campagne spec", "campagne", API_B)
    )
    assert campaign_profile.status_code == 201, campaign_profile.text
    await client.put("/api/miner/settings", json={"default_profile_id": default_profile.json()["id"]})
    return machine_id, default_profile.json()["id"], campaign_profile.json()["id"]


async def test_cycle_complet_done_puis_failed(client, capsys):
    machine_id, default_id, campaign_id_profile = await _prepare_target(client)
    show("PRÉPARATION", {"machine_id": machine_id, "profil_defaut": default_id,
                         "profil_campagne": campaign_id_profile,
                         "simulation": "désactivée"})

    # 1. Le token par défaut tourne (faux mineur A).
    started = await client.post(f"/api/machines/{machine_id}/miner/start", json={"profile_id": default_id})
    assert started.status_code == 200, started.text
    assert started.json()["simulated"] is False
    ps_default = await ps_miner(machine_id)
    show("PS — 1. TOKEN PAR DÉFAUT", ps_default or "(rien)")
    assert "--worker defaut" in ps_default

    # Matrice de notifications : sans canal, aucune ligne n'est écrite.
    from app import notifications
    from app.ai_config import set_setting
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        config = notifications.get_config(db)
        for event in ("campaign_started", "campaign_finished", "campaign_failed",
                      "default_miner_restored", "default_miner_restore_failed"):
            config["matrix"][event] = ["discord"]
        set_setting(db, notifications.CONFIG_KEY, config)
        db.commit()
    finally:
        db.close()

    # 2. Campagne de 2 min : A est arrêté, B démarre.
    created = await client.post(
        "/api/campaigns",
        json={
            "profile_id": campaign_id_profile,
            "coin": "FAKE",
            "start_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "end_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 120)) + "Z",
            "devices": [{"machine_id": machine_id, "device_uuid": GPU_UUID}],
        },
    )
    assert created.status_code == 201, created.text
    first_id = created.json()["id"]
    approved = await client.post(f"/api/campaigns/{first_id}/approve")
    assert approved.status_code == 200, approved.text
    launched = await client.post(f"/api/campaigns/{first_id}/start")
    assert launched.status_code == 200, launched.text
    assert launched.json()["campaign"]["status"] == "running"
    ps_campaign = await ps_miner(machine_id)
    show("PS — 2. CAMPAGNE EN COURS (B)", ps_campaign or "(rien)")
    assert "--worker campagne" in ps_campaign
    assert "--worker defaut" not in ps_campaign

    # 3. À la fin : B arrêté, A redémarre, hashrate revenu → done.
    deadline = time.time() + 240
    while time.time() < deadline:
        await asyncio.sleep(5)
        await tick()
        if campaign_status(first_id) in ("done", "failed"):
            break
    ps_after = await ps_miner(machine_id)
    show("PS — 3. RETOUR AU TOKEN PAR DÉFAUT", ps_after or "(rien)")
    show("JOURNAL — CAMPAGNE done", journal(first_id))
    detail = await client.get(f"/api/campaigns/{first_id}")
    payload = detail.json()
    assert payload["status"] == "done", payload
    assert payload["verified"] is True, payload
    assert "--worker defaut" in ps_after
    assert "--worker campagne" not in ps_after

    # 4. Variante : le binaire du token par défaut ne peut plus être installé.
    broken = profile_body("Token par défaut", "defaut", API_A, url="http://127.0.0.1:9/faux-mineur.tar.gz")
    updated = await client.put(f"/api/miner/profiles/{default_id}", json=broken)
    assert updated.status_code == 200, updated.text
    db = SessionLocal()
    try:
        set_setting(db, "campaign_restore_timeout_s", 6)
        db.commit()
    finally:
        db.close()

    second = await client.post(
        "/api/campaigns",
        json={
            "profile_id": campaign_id_profile,
            "coin": "FAKE",
            "start_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "end_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 40)) + "Z",
            "devices": [{"machine_id": machine_id, "device_uuid": GPU_UUID}],
        },
    )
    second_id = second.json()["id"]
    assert (await client.post(f"/api/campaigns/{second_id}/approve")).status_code == 200
    assert (await client.post(f"/api/campaigns/{second_id}/start")).status_code == 200
    show("PS — 4. CAMPAGNE B (variante échec)", await ps_miner(machine_id) or "(rien)")

    deadline = time.time() + 180
    while time.time() < deadline:
        await asyncio.sleep(4)
        await tick()
        if campaign_status(second_id) in ("done", "failed"):
            break
    show("PS — 4. APRÈS ÉCHEC DU RETOUR", await ps_miner(machine_id) or "(rien)")
    show("JOURNAL — CAMPAGNE failed", journal(second_id))
    failed = await client.get(f"/api/campaigns/{second_id}")
    body = failed.json()
    assert body["status"] == "failed", body
    assert body["attempts"] == 3, body["attempts"]

    from app.models import NotificationLog

    db = SessionLocal()
    try:
        rows = (
            db.query(NotificationLog)
            .filter(NotificationLog.event == "default_miner_restore_failed")
            .all()
        )
        show(
            "NOTIFICATIONS",
            [{"event": row.event, "level": row.level, "canal": row.channel,
              "statut": row.status, "sujet": row.subject} for row in rows],
        )
        assert rows, "notification critique attendue"
        assert rows[0].level == "critique"
    finally:
        db.close()
    capsys.readouterr()
