"""Veille déclenchée par Fossor (§4.8/§4.12) — tests unitaires."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import ai_config, veille
from app.database import SessionLocal
from app.models import Analysis, MinerProfile, Rule

FOSSOR_URL = "http://fossor.test"


def configure_provider(db, **overrides):
    payload = {
        "primary": {
            "provider_type": "hermes-agent",
            "name": "Hermes test",
            "base_url": "http://hermes.test/v1",
            "model": "hermes-agent",
            "timeout_s": 30,
            "api_key": "secret-key",
        }
    }
    payload["primary"].update(overrides)
    ai_config.patch_config(db, payload)


@pytest.mark.asyncio
async def test_run_requires_configured_hermes_provider(client):
    response = await client.post("/api/config/ai/veille/run")
    assert response.status_code == 400
    assert "non configuré" in response.json()["detail"]


@pytest.mark.asyncio
async def test_tick_posts_prompt_and_counts_analysis(client, monkeypatch):
    db = SessionLocal()
    try:
        configure_provider(db)
    finally:
        db.close()

    captured = {}

    def fake_post(provider, api_key, prompt):
        captured["provider"] = provider
        captured["key"] = api_key
        captured["prompt"] = prompt
        db2 = SessionLocal()
        try:
            db2.add(
                Analysis(
                    ts=datetime.now(timezone.utc).isoformat(),
                    run="veille-fossor",
                    action="rien",
                    detail="tick de test",
                    candidates=[{"ticker": "NEB", "verdict": "retenu"}],
                    delivered=False,
                    source="hermes",
                    fingerprint="fp-test-4b",
                )
            )
            db2.commit()
        finally:
            db2.close()
        return httpx.Response(
            200,
            json={"model": "hermes-agent", "choices": [{"message": {"content": "[SILENT]"}}]},
            request=httpx.Request("POST", "http://hermes.test/v1/chat/completions"),
        )

    monkeypatch.setattr(veille, "_post_chat", fake_post)
    state = veille.run_tick("ui", FOSSOR_URL)

    assert state["status"] == "ok"
    assert state["candidates"] == 1
    assert state["analyses"] == 1
    assert state["proposals"] == 0
    assert "spec-mining-fossor" in captured["prompt"]
    assert captured["key"] == "secret-key"
    assert captured["provider"]["base_url"] == "http://hermes.test/v1"

    db = SessionLocal()
    try:
        saved = veille.get_state(db)
        assert saved is not None
        assert saved["status"] == "ok"
        assert saved["finished_at"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_tick_http_error_is_reported(client, monkeypatch):
    db = SessionLocal()
    try:
        configure_provider(db)
    finally:
        db.close()

    monkeypatch.setattr(
        veille,
        "_post_chat",
        lambda *args: httpx.Response(502, text="bad gateway"),
    )
    state = veille.run_tick("ui", FOSSOR_URL)
    assert state["status"] == "error"
    assert "502" in state["result"]


@pytest.mark.asyncio
async def test_trigger_refuses_when_running(client):
    db = SessionLocal()
    try:
        configure_provider(db)
        veille._set_state(db, {"status": "running", "id": "x"})
        state = veille.trigger(db, "ui", FOSSOR_URL)
        assert state["already_running"] is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cadence_rule_drives_due(client):
    db = SessionLocal()
    try:
        configure_provider(db)
        rule = db.get(Rule, "watch_cadence")
        assert rule is not None
        rule.params = {**(rule.params or {}), "hours": 2}
        db.commit()

        finished = datetime.now(timezone.utc) - timedelta(hours=1)
        veille._set_state(db, {"status": "ok", "finished_at": finished.isoformat()})
        assert veille.due(db) is False

        finished = datetime.now(timezone.utc) - timedelta(hours=3)
        veille._set_state(db, {"status": "ok", "finished_at": finished.isoformat()})
        assert veille.due(db) is True

        rule.enabled = False
        db.commit()
        assert veille.due(db) is False
    finally:
        db.close()


@pytest.mark.asyncio
async def test_context_exposes_profiles_without_secret(client):
    db = SessionLocal()
    try:
        import uuid as uuid_lib

        db.add(
            MinerProfile(
                uuid=str(uuid_lib.uuid4()),
                name="Profil veille",
                coin="NEB",
                wallet="WALLET-SECRET-4B",
                pool="stratum+tcp://pool.exemple.tld:3333",
                software="srbminer",
                binary_url="https://exemple.tld/miner.tar.gz",
                sha256="a" * 64,
                binary_path="miner",
                version="1.0.0",
                cmdline="--wallet {wallet} --pool {pool}",
                execution_mode="host",
            )
        )
        db.commit()
        issued = ai_config.new_token(db)
    finally:
        db.close()

    response = await client.get(
        "/api/hermes/context", headers={"Authorization": f"Bearer {issued['token']}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["profiles"][0]["coin"] == "NEB"
    assert "WALLET-SECRET-4B" not in response.text
    assert "pool.exemple.tld" not in response.text
