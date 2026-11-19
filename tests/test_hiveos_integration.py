"""Intégration réelle du pilotage HiveOS (§3e) — cible jetable, faux HiveOS minimal.

La cible est le conteneur Ubuntu sshd ``fossor-hive-3e`` du réseau Docker
``fossor-test``, dans lequel ``tests/docker/hiveos-fake`` a été copié : un
``/hive/bin/miner`` qui lance/arrête un faux mineur de flight sheet, un
``/hive/bin/wd`` qui simule le watchdog (un ``miner stop`` volontaire est annulé
2 s plus tard tant que ``wd stop`` n'a pas été appelé), ``/hive-config/{wallet,rig}.conf``
et ``/run/hive/{cur_miner,last_stat.json,gpu-stats.json}``.

Rien n'est contacté hors de ce réseau : aucun rig réel, aucun paramètre de Fred.

Variables attendues (sinon module ignoré) :
  FOSSOR_TEST_HIVEOS_HOST   nom du conteneur cible
  FOSSOR_TEST_MINER_URL     URL de l'archive du faux mineur de campagne
  FOSSOR_TEST_MINER_SHA     SHA256 de cette archive
  FOSSOR_TEST_SSH_USER / FOSSOR_TEST_SSH_PASSWORD  compte temporaire de provision
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid as uuid_lib
from datetime import datetime, timezone

import pytest

from app import hiveos as H
from app import hiveos_remote as HR

HOST = os.environ.get("FOSSOR_TEST_HIVEOS_HOST", "")
PORT = int(os.environ.get("FOSSOR_TEST_HIVEOS_PORT", "2222"))
URL = os.environ.get("FOSSOR_TEST_MINER_URL", "")
SHA = os.environ.get("FOSSOR_TEST_MINER_SHA", "")
TEMP_USER = os.environ.get("FOSSOR_TEST_SSH_USER", "tester")
TEMP_PASSWORD = os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (HOST and URL and SHA), reason="cible HiveOS/faux mineur absents"),
]

BODY = {
    "name": "Faux mineur de campagne HiveOS",
    "coin": "FAKE",
    "wallet": "prl1demo" + "q" * 30,
    "pool": "stratum+tcp://pool.exemple.tld:3333",
    "software": "custom",
    "binary_url": URL,
    "sha256": SHA,
    "binary_path": "faux-mineur",
    "version": "1.0.0",
    "cmdline": (
        "/opt/fossor/miners/custom/1.0.0/faux-mineur --wallet {wallet} --pool {pool} "
        "--worker {worker} --gpus {gpus}"
    ),
    "execution_mode": "host",
}

PS_HIVE = "ps -eo pid,args | grep -F /hive/miners/custom/hivefake | grep -v grep || true"


def show(title: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    print(f"\n### {title}\n{text}")


async def _run(machine_id: int, command: str) -> str:
    from app.database import SessionLocal
    from app.models import Machine
    from app.miner_remote import default_runner

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        db.expunge(machine)
    finally:
        db.close()
    result = await default_runner(machine, command, timeout=60)
    return ((result.get("stdout") or "") + (result.get("stderr") or "")).strip()


def _machine(machine_id: int):
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        return db.query(Machine).filter(Machine.id == machine_id).first()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_hiveos_stop_start_et_campagne(client):
    created = await client.post(
        "/api/machines",
        json={
            "name": "rig-hiveos-3e",
            "host": HOST,
            "ssh_port": PORT,
            "ssh_user": TEMP_USER,
            "auth_type": "password",
            "secret": TEMP_PASSWORD,
            "sudo": "sudo with password",
            "os_kind": "hiveos",
            "usage": "gpu",
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
    uuids = []
    try:
        for index in range(5):
            device_uuid = str(uuid_lib.uuid4())
            uuids.append(device_uuid)
            db.add(
                Device(
                    uuid=device_uuid,
                    machine_id=machine_id,
                    kind="gpu",
                    index=index,
                    model=f"GPU jetable {index}",
                    present=True,
                    enabled_for_mining=True,
                )
            )
        db.commit()
    finally:
        db.close()

    machine = _machine(machine_id)
    data = await HR.probe(machine)
    show("SONDE HIVEOS (masquée)", H.mask_probe(data))
    assert data["detected"] is True
    assert data["miner"] == "hivefake"
    assert data["rig"]["watchdog"] is True
    assert data["gpu_total"] == 5 and data["mining_gpus"] == 5
    assert data["hashrate_hs"] and data["hashrate_hs"] > 0
    assert "MotDePasseHiveosFactice" not in json.dumps(H.mask_probe(data), ensure_ascii=False)

    # 1. Le refus, avant toute commande : watchdog actif, option absente.
    with pytest.raises(H.HiveosError):
        H.plan_stop(data)
    show("REFUS SANS OPTION", H.watchdog_verdict(data)["reason"])

    # 2. `miner stop` seul : le faux watchdog relance le mineur.
    show("MINER STOP SANS WD STOP", await _run(machine_id, "sudo -n /hive/bin/miner stop"))
    await asyncio.sleep(3)
    back = await _run(machine_id, PS_HIVE)
    show("PS 3 s APRÈS miner stop SANS wd stop", back or "(aucun processus)")
    assert "hivefake" in back

    # 2 bis. La campagne, elle, exige l'option explicite (§3e) : on la valide.
    ack = await client.put("/api/hiveos/watchdog-ack", json={"ack": True, "confirm": "VALIDER"})
    assert ack.status_code == 200 and ack.json()["ack"] is True, ack.text
    show("OPTION WATCHDOG VALIDÉE", ack.json())

    # 3. Arrêt réel par les commandes HiveOS, vérifié par ps et chute du hashrate.
    stopped = await HR.stop(machine, data, ack_watchdog=True, wait_s=12, interval_s=2)
    show("ARRÊT HIVEOS (wd stop + miner stop)", {k: stopped[k] for k in (
        "commands", "verified", "processes_remaining", "before_hashrate_hs",
        "after_hashrate_hs", "hashrate_dropped", "note")})
    assert stopped["verified"] is True
    assert stopped["hashrate_dropped"] is True
    ps_stop = await _run(machine_id, PS_HIVE)
    show("PS APRÈS ARRÊT", ps_stop or "(aucun processus)")
    assert "hivefake" not in ps_stop

    # 4. Campagne réelle (simulation OFF) : toutes les GPU, mineur lancé par Fossor.
    off = await client.put("/api/miner/settings", json={"simulation": False, "confirm": "PILOTER"})
    assert off.json()["simulation"] is False, off.text
    profile = (await client.post("/api/miner/profiles", json=BODY)).json()
    assert profile.get("id"), profile

    refused = await client.post(
        "/api/campaigns",
        json={
            "coin": "FAKE",
            "profile_id": profile["id"],
            "start_at": datetime.now(timezone.utc).isoformat(),
            "devices": [
                {"machine_id": machine_id, "device_uuid": uuids[0]},
                {"machine_id": machine_id, "device_uuid": uuids[1]},
            ],
        },
    )
    show("REFUS CAMPAGNE PARTIELLE", {"status": refused.status_code, "detail": refused.json().get("detail")})
    assert refused.status_code == 409
    assert "campagne partielle impossible" in refused.json()["detail"]

    campaign = await client.post(
        "/api/campaigns",
        json={
            "coin": "FAKE",
            "profile_id": profile["id"],
            "start_at": datetime.now(timezone.utc).isoformat(),
            "devices": [{"machine_id": machine_id, "device_uuid": u} for u in uuids],
        },
    )
    assert campaign.status_code == 201, campaign.text
    campaign_id = campaign.json()["id"]
    approved = await client.post(f"/api/campaigns/{campaign_id}/approve", json={})
    assert approved.status_code == 200, approved.text
    started = await client.post(f"/api/campaigns/{campaign_id}/start")
    assert started.status_code == 200, started.text
    show("CAMPAGNE DÉMARRÉE", started.json()["campaign"]["status"])
    ps_campaign = await _run(machine_id, "ps -eo pid,args | grep -F faux-mineur | grep -v grep || true")
    show("PS — MINEUR DE CAMPAGNE", ps_campaign or "(aucun processus)")
    assert "faux-mineur" in ps_campaign
    ps_hive_down = await _run(machine_id, PS_HIVE)
    show("PS — MINEUR HIVEOS APRÈS DÉMARRAGE CAMPAGNE", ps_hive_down or "(aucun processus)")
    assert "hivefake" not in ps_hive_down

    stopped_campaign = await client.post(f"/api/campaigns/{campaign_id}/stop")
    assert stopped_campaign.status_code == 200, stopped_campaign.text
    for _ in range(20):
        current = (await client.get(f"/api/campaigns/{campaign_id}")).json()
        if current["status"] in ("done", "failed"):
            break
        await asyncio.sleep(2)
    show("STATUT FINAL", (await client.get(f"/api/campaigns/{campaign_id}")).json()["status"])
    events = (await client.get(f"/api/campaigns/{campaign_id}/events")).json()
    show("JOURNAL DE CAMPAGNE", [
        {"kind": row.get("kind"), "result": row.get("result"), "message": row.get("message")}
        for row in events
    ])
    kinds = " ".join(f"{row.get('kind')} {row.get('message')}" for row in events)
    assert "hiveos-stopped" in kinds
    assert "reprise par HiveOS (miner start)" in kinds
    assert "restore-verified" in kinds
    ps_back = await _run(machine_id, PS_HIVE)
    show("PS — MINEUR HIVEOS APRÈS REPRISE", ps_back or "(aucun processus)")
    assert "hivefake" in ps_back
    after = await HR.probe(machine)
    show("HASHRATE APRÈS REPRISE", {"hashrate_hs": after["hashrate_hs"], "source": after["hashrate_source"]})
    assert after["hashrate_hs"] and after["hashrate_hs"] > 0
