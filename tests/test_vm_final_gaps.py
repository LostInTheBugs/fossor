"""Derniers trous de l'étape 3 : cache, hashrate VM, binaire Fossor, wallet de pool."""

from __future__ import annotations

import json
import uuid as uuid_lib

import pytest

from app import pve_vm as P
from app import spec_vm as V

KEY = P.PREVIEW_KEY

PROFILE = {
    "name": "Profil gaps",
    "coin": "NNN",
    "wallet": "demo1pna00000000000000000000000000000000000",
    "pool": "solo",
    "software": "custom",
    "binary_url": "http://127.0.0.1:9/mineur.tar.gz",
    "sha256": "a" * 64,
    "binary_path": "mineur",
    "version": "1.0.0",
    "cmdline": "/opt/fossor/miners/custom/1.0.0/mineur --wallet {wallet} --threads {threads}",
    "api_port": 0,
    "execution_mode": "host",
    "threads": 2,
}


def pve_model(**extra):
    data = {
        "host_id": 1,
        "node": "pve",
        "image_url": "https://example.invalid/debian-12.qcow2",
        "checksum": "b" * 64,
        "checksum_algo": "sha256",
    }
    data.update(extra)
    return P.validate_pve_model(data)


def test_cache_desactive_par_defaut_puis_active():
    assert pve_model()["keep_image_cache"] is False
    assert pve_model(keep_image_cache="true")["keep_image_cache"] is True
    assert pve_model(keep_image_cache=True)["keep_image_cache"] is True
    assert V.validate_vm_model({
        "host_id": 1, "base_image_url": "https://example.invalid/x.qcow2",
        "base_image_sha256": "c" * 64,
    })["keep_image_cache"] is False


async def test_creation_supprime_le_dossier_de_travail(monkeypatch, tmp_path):
    from app import pve_remote as PR

    commands: list[str] = []

    async def runner(host, command, stdin=None, timeout=60):
        commands.append(command)
        stdout = "status: running" if "qm status" in command else ""
        return {"ok": True, "status": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(P, "PVE_TMP_DIR", str(tmp_path / "fossor-pve"))
    monkeypatch.setattr(P, "IMAGE_DIR", str(tmp_path / "fossor-pve" / "images"))
    monkeypatch.setattr(P, "PUBKEY_PATH", str(tmp_path / "fossor-pve" / "fossor.pub"))
    monkeypatch.setattr(P, "KEY_PATH", str(tmp_path / "id"))
    monkeypatch.setattr(P, "KNOWN_HOSTS", str(tmp_path / "known"))

    result = await PR.create(object(), pve_model(), "fossor-gaps-abc", 9900,
                             ssh_public_key=KEY, private_key="cle", simulated=False)
    assert result["cache_kept"] is False
    assert f"rm -rf {tmp_path / 'fossor-pve'}" in result["commands"]

    kept = await PR.create(object(), pve_model(keep_image_cache=True), "fossor-gaps-abc", 9900,
                           ssh_public_key=KEY, private_key="cle", simulated=False)
    assert kept["cache_kept"] is True
    assert not any("rm -rf" in command for command in kept["commands"])


async def test_destruction_supprime_le_cache_et_garde_si_active(monkeypatch, tmp_path):
    from app import pve_remote as PR

    async def runner(host, command, stdin=None, timeout=60):
        stdout = ""
        if "qm config" in command:
            stdout = "name: fossor-gaps-abc\ntags: fossor\ndescription: fossor_vm=1\n"
        return {"ok": True, "status": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(PR, "RUNNER", runner)
    monkeypatch.setattr(P, "PVE_TMP_DIR", str(tmp_path / "fossor-pve"))
    backup = {"verified": True, "checked_at_epoch": 1_700_000_000}
    gone = await PR.destroy(object(), pve_model(), 9900, backup=backup,
                            now_epoch=1_700_000_100, max_age_days=7, simulated=False)
    assert gone["cache_kept"] is False
    assert f"rm -rf {tmp_path / 'fossor-pve'}" in gone["commands"]

    kept = await PR.destroy(object(), pve_model(keep_image_cache=True), 9900, backup=backup,
                            now_epoch=1_700_000_100, max_age_days=7, simulated=False)
    assert not any("rm -rf" in command for command in kept["commands"])


def test_hashrate_api_cpuminer_et_journal():
    assert V.parse_vm_hashrate('{"hashrate": {"total": [4048.0, 4250.5]}}') == 4250.5
    assert V.parse_vm_hashrate('{"hashrate": {"total": 777.0}}') == 777.0
    log = "accepted 1/1\n[12:00] 12.5 kH/s yes!\n[12:01] 4242.0 H/s"
    assert V.parse_vm_hashrate(log) == 4242.0
    assert V.parse_log_hashrate("total: 1.5 MH/s") == 1_500_000.0
    assert V.parse_vm_hashrate("rien") is None


def test_collecte_vm_interroge_api_ou_journal():
    api = [step for step in V.guest_collect_plan("192.0.2.10", dict(PROFILE, api_port=4048,
                                                                    api_kind="cpuminer"))
           if step.title == "hashrate du mineur dans la VM"]
    assert len(api) == 1 and "/summary" in api[0].command()

    journal = [step for step in V.guest_collect_plan("192.0.2.10", dict(PROFILE))
               if step.title == "hashrate du mineur dans la VM"]
    assert len(journal) == 1 and "tail -n 80" in journal[0].command()


def test_binaire_fossor_exige_un_envoi_verifie():
    profile = dict(PROFILE, binary_source="fossor")
    with pytest.raises(V.VmError):
        V.provision_plan(pve_model(), profile, "192.0.2.10", coin="NNN")
    plan = V.provision_plan(pve_model(), profile, "192.0.2.10", coin="NNN", binary_pushed=True)
    titles = [step.title for step in plan]
    assert "archive du mineur envoyée par Fossor (déjà vérifiée à l'envoi)" in titles
    assert "téléchargement du mineur dans la VM" not in titles
    assert "SHA256 du mineur vérifié dans la VM" in titles


def test_wallet_de_pool_sans_fichier_local():
    plan = V.provision_plan(pve_model(wallet_create_cmd="walletd --datadir {datadir}"),
                            dict(PROFILE, wallet_mode="pool_address"), "192.0.2.10", coin="NNN")
    assert not any(step.title.startswith("création du wallet") for step in plan)
    verified = V.evaluate_pool_wallet(["chain", "logs"], 1_700_000_000)
    assert verified["verified"] is True and verified["mode"] == "pool_address"
    refused = V.evaluate_pool_wallet(["wallet.dat"], 1_700_000_000)
    assert refused["verified"] is False and "wallet.dat" in refused["reason"]


async def test_depot_du_binaire_avec_sha256(client, monkeypatch, tmp_path):
    import base64
    import hashlib

    monkeypatch.setenv("FOSSOR_DATA_DIR", str(tmp_path))
    created = await client.post("/api/miner/profiles", json=PROFILE)
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    blob = b"\x1f\x8b" + b"archive-de-test-fossor" * 8
    uploaded = await client.post(f"/api/miner/profiles/{profile_id}/binary",
                           json={"content_b64": base64.b64encode(blob).decode()})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["sha256"] == hashlib.sha256(blob).hexdigest()
    assert uploaded.json()["size"] == len(blob)

    meta = (await client.get(f"/api/miner/profiles/{profile_id}/binary")).json()
    assert meta["present"] is True and meta["source"] == "fossor"
    profile = (await client.get(f"/api/miner/profiles/{profile_id}")).json()
    assert profile["binary_source"] == "fossor"
    assert profile["binary_size"] == len(blob)


async def test_campagne_vm_lit_le_hashrate_de_la_vm(client):
    from app.database import SessionLocal
    from app.models import Campaign, SpecVm
    from app.routers.campaigns import campaign_to_dict

    db = SessionLocal()
    try:
        profile_id = (await client.post("/api/miner/profiles", json=dict(
            PROFILE, name=f"Profil hashrate {uuid_lib.uuid4().hex[:6]}"))).json()["id"]
        campaign = db.query(Campaign).first()
        if campaign is None:
            campaign = Campaign(uuid=str(uuid_lib.uuid4()), coin="NNN", profile_id=profile_id,
                                start_at="2026-11-19T10:00:00+00:00", status="running",
                                execution_mode="vm")
            db.add(campaign)
            db.flush()
        vm = SpecVm(uuid=str(uuid_lib.uuid4()), campaign_id=campaign.id, host_machine_id=1,
                    name=f"fossor-{uuid_lib.uuid4().hex[:8]}", mode="vm", hypervisor="proxmox",
                    vmid=9901, state="running", hashrate_hs=4242.0,
                    hashrate_source="API du mineur (VM)")
        db.add(vm)
        db.commit()
        data = campaign_to_dict(db, campaign)
        assert data["hashrate_hs"] == 4242.0
        assert data["hashrate_source"] == "API du mineur (VM)"
    finally:
        db.close()
