"""Étape 4a — règles, schémas, historique, API Hermes, propositions, import.

Chaque contrôle vérifie l'**effet** : la valeur relue par l'API, la ligne
d'historique, le refus motivé, l'absence de secret dans le contexte.
"""

import json
import uuid
from pathlib import Path

import pytest

from app import analyses as A
from app import rules as R
from app.ai_config import new_token
from app.database import SessionLocal
from app.models import (
    Analysis,
    AuditLog,
    Campaign,
    Device,
    Machine,
    MinerProfile,
    Rule,
    RuleChange,
    Wallet,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mining-analyses.json"


def seed_machine(db, name="rig-test", host="10.99.0.7"):
    db.expire_on_commit = False
    machine = Machine(
        uuid=str(uuid.uuid4()),
        name=name,
        host=host,
        ssh_user="tester",
        auth_type="password",
        secret_ref="SECRET-PASSWORD-XYZ",
        usage="both",
        enabled=True,
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def seed_device(db, machine, kind="gpu", role="default_and_spec"):
    device = Device(
        uuid=str(uuid.uuid4()),
        machine_id=machine.id,
        kind=kind,
        index=0,
        vendor="Test",
        model="Carte test",
        present=True,
        enabled_for_mining=True,
        role=role,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def seed_profile(db, coin="NEB"):
    profile = MinerProfile(
        uuid=str(uuid.uuid4()),
        name=f"Profil {coin}",
        coin=coin,
        wallet="WALLET-SECRET-123",
        pool="stratum+tcp://pool.exemple.tld:3333",
        software="custom",
        binary_url="http://127.0.0.1:9/mineur.tar.gz",
        sha256="b" * 64,
        binary_path="mineur",
        version="1.0.0",
        cmdline="mineur --wallet {wallet} --pool {pool}",
        execution_mode="host",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def token_for_hermes(db) -> str:
    return new_token(db)["token"]


def proposal_body(device, profile, **overrides):
    body = {
        "coin": "NEB",
        "profile_id": profile.id,
        "device_uuids": [device.uuid],
        "start_at": "2026-11-20T10:00:00+00:00",
        "end_at": "2026-11-24T10:00:00+00:00",
        "execution_mode": "host",
        "note": "proposition de test",
        "candidate": {"age_hours": 5, "network_share_pct": 1.5},
    }
    body.update(overrides)
    return body


# ── Schémas et règles par défaut ──────────────────────────────────────
def test_default_rules_match_spec_table():
    assert len(R.RULE_SPECS) == 12
    assert {spec["id"] for spec in R.RULE_SPECS} == {
        "freshness",
        "exclude_asic_only",
        "cpu_only_vm",
        "auto_launch",
        "gpu_duration",
        "cpu_duration",
        "parallel_cpu_gpu",
        "solo_to_pool",
        "max_network_share",
        "electricity_cost",
        "notifications",
        "watch_cadence",
    }
    assert R.RULE_BY_ID["auto_launch"]["enabled"] is False
    assert R.default_params("freshness") == {
        "min_hours": 0,
        "max_hours": 72,
        "watch_days": 7,
    }
    assert R.default_params("electricity_cost")["eur_per_kwh"] == 0.20
    assert R.default_params("gpu_duration")["hours"] == 96
    assert R.default_params("max_network_share")["max_pct"] == 10
    assert R.default_params("watch_cadence")["hours"] == 6


def test_invalid_params_rejected_with_field_name():
    with pytest.raises(R.RuleError) as exc:
        R.validate_params("freshness", {"max_hours": 10000})
    assert "max_hours" in str(exc.value)

    with pytest.raises(R.RuleError) as exc:
        R.validate_params("freshness", {"champ_inconnu": 1})
    assert "champ_inconnu" in str(exc.value)

    with pytest.raises(R.RuleError) as exc:
        R.validate_params("freshness", {"min_hours": 80, "max_hours": 10})
    assert "max_hours" in str(exc.value)

    with pytest.raises(R.RuleError):
        R.validate_params("regle_inconnue", {})


def test_seed_rules_is_idempotent():
    db = SessionLocal()
    try:
        assert db.query(Rule).count() == 12
        assert R.seed_rules(db) == 0
        assert db.query(Rule).count() == 12
    finally:
        db.close()


# ── Routes admin : édition + historique + audit ───────────────────────
async def test_patch_rule_edited_and_history_written(client):
    response = await client.patch("/api/rules/freshness", json={"params": {"max_hours": 48}})
    assert response.status_code == 200, response.text
    assert response.json()["rule"]["params"]["max_hours"] == 48

    history = await client.get("/api/rules/freshness/history")
    changes = history.json()["changes"]
    assert len(changes) == 1
    assert changes[0]["actor"] == "admin"
    assert changes[0]["before"]["params"]["max_hours"] == 72
    assert changes[0]["after"]["params"]["max_hours"] == 48

    db = SessionLocal()
    try:
        assert db.query(RuleChange).count() == 1
        audits = db.query(AuditLog).filter(AuditLog.action == "règle modifiée").all()
        assert len(audits) == 1
        assert "rule:freshness" == audits[0].object_ref
    finally:
        db.close()


async def test_patch_invalid_params_returns_422_and_writes_nothing(client):
    response = await client.patch("/api/rules/freshness", json={"params": {"max_hours": 10000}})
    assert response.status_code == 422
    assert "max_hours" in response.json()["detail"]

    history = await client.get("/api/rules/freshness/history")
    assert history.json()["changes"] == []
    rules = await client.get("/api/rules")
    freshness = next(r for r in rules.json()["rules"] if r["id"] == "freshness")
    assert freshness["params"]["max_hours"] == 72


async def test_toggle_rule_changes_state(client):
    response = await client.patch("/api/rules/watch_cadence", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["rule"]["enabled"] is False
    rules = await client.get("/api/rules")
    rule = next(r for r in rules.json()["rules"] if r["id"] == "watch_cadence")
    assert rule["enabled"] is False


# ── API Hermes : jeton, contexte sans secret ──────────────────────────
async def test_invalid_token_returns_401(unauth_client):
    response = await unauth_client.get(
        "/api/hermes/context", headers={"Authorization": "Bearer pas-un-jeton"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Jeton Hermes invalide"


async def test_valid_token_on_admin_route_returns_403(unauth_client):
    db = SessionLocal()
    try:
        token = token_for_hermes(db)
    finally:
        db.close()
    response = await unauth_client.get(
        "/api/rules", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert "droits insuffisants" in response.json()["detail"]


async def test_context_has_no_secret_and_no_ip(unauth_client):
    db = SessionLocal()
    try:
        machine = seed_machine(db)
        device = seed_device(db, machine)
        seed_profile(db)
        db.add(Wallet(coin_ticker="NEB", address="WALLET-SECRET-123", label="test"))
        db.commit()
        token = token_for_hermes(db)
    finally:
        db.close()

    response = await unauth_client.get(
        "/api/hermes/context", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    text = json.dumps(data)

    assert "10.99.0.7" not in text
    assert "SECRET-PASSWORD-XYZ" not in text
    assert "WALLET-SECRET-123" not in text
    assert {rule["id"] for rule in data["rules"]} == {
        spec["id"] for spec in R.RULE_SPECS if spec["enabled"]
    }
    assert "auto_launch" in data["disabled_rules"]
    assert data["auto_launch"]["stage"] == "4b"
    assert data["capacity"]["counts"] == {"cpu": 0, "gpu": 1}
    assert data["capacity"]["gpu"][0]["uuid"] == device.uuid
    assert data["capacity"]["gpu"][0]["machine"] == "rig-test"
    assert data["campaigns"] == {"running": [], "proposed": []}
    assert data["limits"]["forbidden"] == [
        "wallets",
        "secrets",
        "machine_ips",
        "ssh",
        "launch",
    ]


# ── Propositions : refus motivés, dépôt sans lancement ────────────────
async def test_proposal_refused_when_device_role_forbidden(unauth_client):
    db = SessionLocal()
    try:
        machine = seed_machine(db)
        device = seed_device(db, machine, role="default_only")
        profile = seed_profile(db)
        token = token_for_hermes(db)
    finally:
        db.close()

    response = await unauth_client.post(
        "/api/hermes/proposals",
        headers={"Authorization": f"Bearer {token}"},
        json=proposal_body(device, profile),
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "Proposition refusée"
    codes = {reason["code"] for reason in detail["reasons"]}
    assert "device_role_not_allowed" in codes
    assert any("default_only" in reason["message"] for reason in detail["reasons"])

    db = SessionLocal()
    try:
        assert db.query(Campaign).count() == 0
    finally:
        db.close()


async def test_proposal_refused_when_candidate_outside_freshness(unauth_client):
    db = SessionLocal()
    try:
        machine = seed_machine(db)
        device = seed_device(db, machine)
        profile = seed_profile(db)
        token = token_for_hermes(db)
    finally:
        db.close()

    response = await unauth_client.post(
        "/api/hermes/proposals",
        headers={"Authorization": f"Bearer {token}"},
        json=proposal_body(device, profile, candidate={"age_hours": 200}),
    )
    assert response.status_code == 422
    codes = {reason["code"] for reason in response.json()["detail"]["reasons"]}
    assert "candidate_stale" in codes


async def test_proposal_accepted_stays_proposed_even_if_auto_launch_enabled(client, unauth_client):
    db = SessionLocal()
    try:
        machine = seed_machine(db)
        device = seed_device(db, machine)
        profile = seed_profile(db)
        token = token_for_hermes(db)
    finally:
        db.close()

    # La règle « lancement auto » est activée : elle ne doit pourtant rien lancer.
    enabled = await client.patch("/api/rules/auto_launch", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["rule"]["enabled"] is True

    response = await unauth_client.post(
        "/api/hermes/proposals",
        headers={"Authorization": f"Bearer {token}"},
        json=proposal_body(device, profile),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "proposed"
    assert data["auto_launch"] is False
    assert data["stage"] == "4b"
    campaign_id = data["campaign"]["id"]

    db = SessionLocal()
    try:
        campaign = db.get(Campaign, campaign_id)
        assert campaign.status == "proposed"
        assert campaign.origin == "hermes"
        assert campaign.analysis_id is None
        assert db.query(Campaign).count() == 1
    finally:
        db.close()

    # Visible dans le contexte, puis approuvable par le cycle 3b.
    context = await unauth_client.get(
        "/api/hermes/context", headers={"Authorization": f"Bearer {token}"}
    )
    assert [c["id"] for c in context.json()["campaigns"]["proposed"]] == [campaign_id]

    approved = await client.post(f"/api/campaigns/{campaign_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


async def test_proposal_links_analysis_id(unauth_client):
    db = SessionLocal()
    try:
        machine = seed_machine(db)
        device = seed_device(db, machine)
        profile = seed_profile(db)
        token = token_for_hermes(db)
    finally:
        db.close()

    analysis = await unauth_client.post(
        "/api/hermes/analyses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "ts": "2026-11-20T06:00:00Z",
            "run": "veille 6h",
            "action": "rien",
            "detail": "Aucun lancement.",
            "candidates": [
                {
                    "name": "NEB",
                    "ticker": "NEB",
                    "algo": "x",
                    "links": ["https://exemple.tld/neb"],
                    "verdict": "retenu",
                    "pour": ["réseau actif"],
                    "contre": ["jeune"],
                }
            ],
        },
    )
    assert analysis.status_code == 200, analysis.text
    analysis_id = analysis.json()["analysis"]["id"]

    response = await unauth_client.post(
        "/api/hermes/proposals",
        headers={"Authorization": f"Bearer {token}"},
        json=proposal_body(device, profile, analysis_id=analysis_id),
    )
    assert response.status_code == 201, response.text
    assert response.json()["campaign"]["analysis_id"] == analysis_id


async def test_hermes_analysis_is_idempotent(unauth_client):
    db = SessionLocal()
    try:
        token = token_for_hermes(db)
    finally:
        db.close()
    payload = {
        "ts": "2026-11-20T06:00:00Z",
        "run": "veille 6h",
        "action": "bascule",
        "detail": "NEB retenu.",
        "candidates": [
            {
                "name": "Neural Engine Byte",
                "ticker": "NEB",
                "algo": "NEBHASH",
                "links": ["https://exemple.tld/neb"],
                "verdict": "retenu",
                "pour": ["mainnet"],
                "contre": ["jeune"],
            }
        ],
    }
    first = await unauth_client.post(
        "/api/hermes/analyses", headers={"Authorization": f"Bearer {token}"}, json=payload
    )
    assert first.status_code == 200
    assert first.json()["created"] is True

    second = await unauth_client.post(
        "/api/hermes/analyses", headers={"Authorization": f"Bearer {token}"}, json=payload
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    db = SessionLocal()
    try:
        assert db.query(Analysis).count() == 1
    finally:
        db.close()


# ── Import : prévisualisation puis idempotence ────────────────────────
async def test_import_preview_then_no_duplicate(client):
    content = FIXTURE.read_bytes()

    preview = await client.post(
        "/api/analyses/import",
        data={"dry_run": "true"},
        files={"file": ("mining-analyses.json", content, "application/json")},
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["dry_run"] is True
    assert plan["total"] == 4
    assert plan["new"] == 4
    assert plan["duplicates"] == 0
    assert len(plan["preview"]) == 4

    db = SessionLocal()
    try:
        assert db.query(Analysis).count() == 0  # la prévisualisation n'écrit rien
    finally:
        db.close()

    first = await client.post(
        "/api/analyses/import",
        data={"dry_run": "false"},
        files={"file": ("mining-analyses.json", content, "application/json")},
    )
    assert first.status_code == 200
    assert first.json()["created"] == 4

    second = await client.post(
        "/api/analyses/import",
        data={"dry_run": "false"},
        files={"file": ("mining-analyses.json", content, "application/json")},
    )
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["duplicates"] == 4

    db = SessionLocal()
    try:
        assert db.query(Analysis).count() == 4
    finally:
        db.close()


def test_fixture_is_anonymised():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "192.168." not in text
    import re

    assert re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text) == []
    data = json.loads(text)
    assert len(data["analyses"]) == 4
    assert data["analyses"][0]["candidates"][0]["verdict"] == "retenu"


def test_analysis_normalisation_from_legacy_shape():
    entry = A.normalize_analysis(
        {
            "ts": "2026-11-19T17:07Z",
            "action_detail": "détail",
            "candidates": [
                {
                    "nom": "NEB (Neural Engine Byte)",
                    "lien": "https://exemple.tld",
                    "verdict": "REJETÉ",
                    "pour": "un",
                    "contre": ["deux"],
                }
            ],
        }
    )
    assert entry["detail"] == "détail"
    candidate = entry["candidates"][0]
    assert candidate["ticker"] == "NEB"
    assert candidate["name"] == "Neural Engine Byte"
    assert candidate["verdict"] == "rejete"
    assert candidate["links"] == ["https://exemple.tld"]
    assert candidate["pour"] == ["un"]
