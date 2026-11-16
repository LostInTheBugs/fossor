"""AI analysis configuration (§4.12) — unit + API tests, no real network.

Every HTTP call goes through ``httpx.MockTransport`` installed on
``app.ai_client.TRANSPORT``; the API key must never appear in an API response
or in the audit log.
"""

import hashlib
import json

import httpx
import pytest

API_KEY = "sk-test-ai-0123456789abcdef"
BASE = "http://ai.local/v1"


async def _configure(client, which="primary", **fields):
    body = {
        "provider_type": "openai-compatible-local",
        "name": "local",
        "base_url": BASE,
        "model": "mock-model",
        "timeout_s": 5,
        "max_tokens": 256,
        "reasoning_effort": "",
        "extra_body": None,
        "enabled": True,
    }
    body.update(fields)
    response = await client.patch("/api/config/ai", json={which: body})
    assert response.status_code == 200, response.text
    return response.json()


def _use(monkeypatch, handler):
    from app import ai_client

    monkeypatch.setattr(ai_client, "TRANSPORT", httpx.MockTransport(handler), raising=False)


def _audit_rows():
    from app.database import SessionLocal
    from app.models import AuditLog

    db = SessionLocal()
    try:
        return [(row.action, row.result, row.output or "") for row in db.query(AuditLog).order_by(AuditLog.id)]
    finally:
        db.close()


async def test_defaults(client):
    response = await client.get("/api/config/ai")
    assert response.status_code == 200
    body = response.json()
    assert body["primary"] is None and body["fallback"] is None
    assert body["analysis_cadence_hours"] == 6
    assert body["fallback_enabled"] is True
    assert body["hermes_token"] == {"has_token": False, "created_at": None}


async def test_patch_stores_key_encrypted_and_never_returns_it(client):
    body = await _configure(client, api_key=API_KEY, extra_body={"provider": "deepseek"})
    assert body["primary"]["has_api_key"] is True
    assert API_KEY not in json.dumps(body)

    fetched = await client.get("/api/config/ai")
    assert API_KEY not in fetched.text
    assert "api_key_ref" not in fetched.text

    assert API_KEY not in json.dumps(_audit_rows())

    from app.database import SessionLocal
    from app.models import AiProvider

    db = SessionLocal()
    try:
        row = db.query(AiProvider).filter(AiProvider.which == "primary").one()
        assert row.api_key_ref and API_KEY not in row.api_key_ref
        assert row.extra_body == '{"provider": "deepseek"}'
    finally:
        db.close()


async def test_partial_patch_keeps_key_and_extra_body(client):
    await _configure(client, api_key=API_KEY, extra_body={"provider": "deepseek"})
    response = await client.patch("/api/config/ai", json={"primary": {"model": "other-model"}})
    assert response.status_code == 200
    primary = response.json()["primary"]
    assert primary["has_api_key"] is True
    assert primary["extra_body"] == {"provider": "deepseek"}
    assert primary["model"] == "other-model"


@pytest.mark.parametrize(
    "fields",
    [
        {"provider_type": "magic-llm"},
        {"base_url": "ftp://nope"},
        {"max_tokens": 0},
        {"timeout_s": 900},
        {"reasoning_effort": "ultra"},
        {"extra_body": "{pas du json"},
    ],
)
async def test_patch_validation(client, fields):
    response = await client.patch("/api/config/ai", json={"primary": fields})
    assert response.status_code == 400, response.text
    assert "detail" in response.json()


async def test_probe_success(client, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "mock-model-7",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}],
            },
        )

    await _configure(client, api_key=API_KEY)
    _use(monkeypatch, handler)
    response = await client.post("/api/config/ai/test?which=primary")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "mock-model-7"
    assert body["extract"] == "OK"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert API_KEY not in response.text

    assert seen["url"] == BASE + "/chat/completions"
    assert seen["headers"]["authorization"] == f"Bearer {API_KEY}"
    assert seen["body"]["max_tokens"] == 20
    assert seen["body"]["messages"] == [{"role": "user", "content": "Réponds uniquement OK"}]


async def test_probe_extra_body_overrides_model(client, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "hermes-agent", "choices": [{"message": {"content": "OK"}}]})

    await _configure(
        client,
        provider_type="hermes-agent",
        model="hermes-agent",
        extra_body={"provider": "deepseek", "model": "deepseek-flash", "require_model_lock": True},
    )
    _use(monkeypatch, handler)
    response = await client.post("/api/config/ai/test?which=primary")
    assert response.json()["ok"] is True
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["body"]["provider"] == "deepseek"
    assert seen["body"]["require_model_lock"] is True


async def test_probe_401_never_leaks_key(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {API_KEY}"}})

    await _configure(client, api_key=API_KEY)
    _use(monkeypatch, handler)
    response = await client.post("/api/config/ai/test?which=primary")
    body = response.json()
    assert body["ok"] is False
    assert "401" in body["error"]
    assert API_KEY not in response.text
    assert API_KEY not in json.dumps(_audit_rows())
    assert any(action == "ai-test" and result == "erreur" for action, result, _ in _audit_rows())


async def test_probe_timeout(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    await _configure(client, timeout_s=3)
    _use(monkeypatch, handler)
    body = (await client.post("/api/config/ai/test?which=primary")).json()
    assert body["ok"] is False
    assert "élai dépassé" in body["error"]
    assert "3 s" in body["error"]


async def test_probe_non_json(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>", headers={"content-type": "text/html"})

    await _configure(client)
    _use(monkeypatch, handler)
    body = (await client.post("/api/config/ai/test?which=primary")).json()
    assert body["ok"] is False
    assert "non JSON" in body["error"]


async def test_probe_missing_provider(client):
    body = (await client.post("/api/config/ai/test?which=fallback")).json()
    assert body["ok"] is False
    assert "non configuré" in body["error"]
    bad = await client.post("/api/config/ai/test?which=tertiary")
    assert bad.status_code == 400


async def test_token_hashed_and_regeneration_invalidates_old(client):
    first = await client.post("/api/config/ai/token")
    assert first.status_code == 200
    token = first.json()["token"]
    assert len(token) >= 40

    # Ping: 200 with the token, 401 without.
    assert (await client.get("/api/hermes/ping", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    assert (await client.get("/api/hermes/ping")).status_code == 401
    assert (await client.get("/api/hermes/ping", headers={"Authorization": "Bearer wrong-token"})).status_code == 401

    # Stored value is a digest, not the token.
    from app.database import SessionLocal
    from app.models import AppSetting

    db = SessionLocal()
    try:
        stored = db.get(AppSetting, "hermes_token_hash")
        assert stored is not None
        assert json.loads(stored.value) == hashlib.sha256(token.encode()).hexdigest()
        assert token not in stored.value
    finally:
        db.close()

    info = (await client.get("/api/config/ai")).json()["hermes_token"]
    assert info["has_token"] is True and info["created_at"]
    assert token not in (await client.get("/api/config/ai")).text

    second = (await client.post("/api/config/ai/token")).json()["token"]
    assert second != token
    assert (await client.get("/api/hermes/ping", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    assert (await client.get("/api/hermes/ping", headers={"Authorization": f"Bearer {second}"})).status_code == 200

    actions = [action for action, _, _ in _audit_rows()]
    assert "hermes-token" in actions
    assert token not in json.dumps(_audit_rows())


async def test_token_never_exported(client):
    token = (await client.post("/api/config/ai/token")).json()["token"]
    await _configure(client, api_key=API_KEY)

    plain = await client.get("/api/config/export")
    assert token not in plain.text
    assert "hermes_token" not in plain.text
    assert API_KEY not in plain.text

    sealed = await client.post(
        "/api/config/export", json={"include_secrets": True, "passphrase": "phrase-export-1"}
    )
    assert sealed.status_code == 200
    doc = sealed.json()
    assert token not in sealed.text
    assert "hermes_token" not in json.dumps(doc["settings"])
    assert doc["secrets"]["ai"]["primary"]
    assert API_KEY not in sealed.text
    assert doc["ai"]["providers"][0]["has_api_key"] is True


async def test_export_import_restores_ai_key(client):
    await _configure(client, api_key=API_KEY, model="deepseek-flash")
    sealed = (await client.post(
        "/api/config/export", json={"include_secrets": True, "passphrase": "phrase-import-1"}
    )).json()

    cleared = await client.patch("/api/config/ai", json={"primary": {"clear_api_key": True}})
    assert cleared.json()["primary"]["has_api_key"] is False

    # Wrong passphrase: 400 and nothing written.
    wrong = await client.post(
        "/api/config/import?dry_run=false&mode=merge",
        json=sealed,
        headers={"X-Fossor-Passphrase": "mauvaise-phrase"},
    )
    assert wrong.status_code == 400, wrong.text
    assert (await client.get("/api/config/ai")).json()["primary"]["has_api_key"] is False

    # Dry run announces the AI change without writing.
    preview = await client.post(
        "/api/config/import?dry_run=true&mode=merge",
        json=sealed,
        headers={"X-Fossor-Passphrase": "phrase-import-1"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["diff"]["ai"]["modified"] or preview.json()["diff"]["ai"]["added"]
    assert (await client.get("/api/config/ai")).json()["primary"]["has_api_key"] is False

    applied = await client.post(
        "/api/config/import?dry_run=false&mode=merge",
        json=sealed,
        headers={"X-Fossor-Passphrase": "phrase-import-1"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["stats"]["ai"]["updated"] == 1

    from app.database import SessionLocal
    from app.models import AiProvider
    from app.secrets import decrypt_secret

    db = SessionLocal()
    try:
        row = db.query(AiProvider).filter(AiProvider.which == "primary").one()
        assert row.api_key_ref
        assert decrypt_secret(row.api_key_ref) == API_KEY
    finally:
        db.close()
    assert (await client.get("/api/config/ai")).json()["primary"]["has_api_key"] is True


async def test_import_rejects_invalid_ai_block(client):
    doc = (await client.get("/api/config/export")).json()
    doc["ai"] = {"providers": [{"which": "tertiary", "provider_type": "hermes-agent"}]}
    response = await client.post("/api/config/import?dry_run=true", json=doc)
    assert response.status_code == 400
    assert "which" in response.json()["detail"]
