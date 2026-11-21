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


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "[SILENT]"}}]},
        request=httpx.Request("POST", "http://hermes.test/v1/chat/completions"),
    )


def test_tick_id_is_stable_from_trigger_to_final_state(client, monkeypatch):
    """Un seul identifiant : celui du POST, repris au début et à la fin."""
    import threading

    db = SessionLocal()
    try:
        configure_provider(db)
        captured = {}
        done = threading.Event()

        def fake_run(trigger, url, tick_id=None, started=None):
            captured["tick_id"] = tick_id
            captured["started"] = started
            done.set()
            return {"id": tick_id, "status": "ok"}

        monkeypatch.setattr(veille, "run_tick", fake_run)
        pending = veille.trigger(db, "ui", FOSSOR_URL)
        assert pending["status"] == "running"
        assert done.wait(5)
        assert captured["tick_id"] == pending["id"]
        assert veille.get_state(db)["id"] == pending["id"]
    finally:
        db.close()


def test_post_chat_before_cancels_past_deadline(monkeypatch):
    """Au-delà de la borne, l'appel est annulé et le tick rend la main."""
    import time as time_lib

    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    tick = veille._Tick("tick-expire", started, 1)

    def slow(*_args):
        time_lib.sleep(20)
        return _ok_response()

    monkeypatch.setattr(veille, "_post_chat", slow)
    t0 = time_lib.monotonic()
    response, error = veille._post_chat_before({"base_url": "http://x"}, None, "p", tick)
    elapsed = time_lib.monotonic() - t0
    assert response is None
    assert error == "timeout"
    assert elapsed < 10


def test_run_tick_timeout_status_and_warning(client, monkeypatch):
    db = SessionLocal()
    try:
        configure_provider(db)
    finally:
        db.close()
    monkeypatch.setattr(veille, "_post_chat_before", lambda *args, **kwargs: (None, "timeout"))
    state = veille.run_tick("ui", FOSSOR_URL)
    assert state["status"] == "timeout"
    assert state["warning"] is True
    assert state["analyses"] == 0
    assert "durée maximale" in state["result"]
    assert state["id"]


def test_run_tick_without_analysis_is_ok_sans_analyse(client, monkeypatch):
    db = SessionLocal()
    try:
        configure_provider(db)
    finally:
        db.close()
    monkeypatch.setattr(veille, "_post_chat", lambda *args: _ok_response())
    state = veille.run_tick("ui", FOSSOR_URL)
    assert state["status"] == "ok_sans_analyse"
    assert state["warning"] is True
    assert state["analyses"] == 0
    assert "aucune analyse" in state["result"]


def test_journal_and_received_counters_during_tick(client, monkeypatch):
    """Le journal prouve les requêtes reçues ; les compteurs suivent le dépôt."""
    db = SessionLocal()
    try:
        configure_provider(db)
    finally:
        db.close()

    def fake_post(*_args):
        veille.note_request("GET", "/api/hermes/context", 200)
        veille.note_request("POST", "/api/hermes/analyses", 201)
        veille.note_analysis(2)
        return _ok_response()

    monkeypatch.setattr(veille, "_post_chat", fake_post)
    state = veille.run_tick("ui", FOSSOR_URL)
    assert state["status"] == "ok"
    assert state["analyses"] == 1
    assert state["candidates"] == 2
    assert [(j["method"], j["path"], j["status"]) for j in state["journal"]] == [
        ("GET", "/api/hermes/context", 200),
        ("POST", "/api/hermes/analyses", 201),
    ]


def test_close_interrupted_on_startup(client):
    """Un tick resté « running » est clos en « interrupted » au démarrage."""
    db = SessionLocal()
    try:
        started = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        veille._set_state(db, {"id": "running-tick", "status": "running",
                               "started_at": started, "finished_at": None,
                               "journal": []})
        state = veille.close_interrupted(db)
        assert state is not None
        assert state["status"] == "interrupted"
        assert state["finished_at"]
        assert state["warning"] is True
        assert state["duration_s"] >= 300
        assert veille.get_state(db)["status"] == "interrupted"
    finally:
        db.close()
