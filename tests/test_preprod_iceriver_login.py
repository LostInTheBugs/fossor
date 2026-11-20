"""Préprod — le login IceRiver part comme celui du navigateur et se trace."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.asic_client import AsicAuthError, _login

BASE = "https://192.0.2.170"


class _Jar:
    def __init__(self, names):
        self.jar = [SimpleNamespace(name=name) for name in names]


class _FakeClient:
    def __init__(self, response, cookie_names):
        self.response = response
        self.cookies = _Jar(cookie_names)
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class _FakeSession:
    def __init__(self, response, cookie_names=()):
        self.client = _FakeClient(response, list(cookie_names))

    async def _note_certificate(self, url):
        return None


def _response(status: int, location: str = "", body: str = "") -> httpx.Response:
    headers = {"location": location} if location else {}
    return httpx.Response(status, headers=headers, content=body.encode())


@pytest.mark.asyncio
async def test_login_sends_the_browser_headers():
    session = _FakeSession(_response(302, "/"), ["esp-app"])
    step = await _login(session, BASE, "admin", "secret")
    method, url, kwargs = session.client.calls[0]
    headers = kwargs["headers"]
    assert method == "POST"
    assert url == BASE + "/user/login"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Accept"].startswith("application/json")
    assert headers["Referer"] == BASE + "/user/loginpage"
    assert headers["Origin"] == BASE
    assert headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert "secret" not in step
    assert "cookie esp-app=présent" in step


@pytest.mark.asyncio
async def test_notfound_without_body_is_not_a_credential_refusal():
    session = _FakeSession(_response(302, "/notfound"), [])
    with pytest.raises(AsicAuthError) as caught:
        await _login(session, BASE, "admin", "secret")
    message = str(caught.value)
    assert "session refusée après login (302 → /notfound) — voir la trace" in message
    assert "cookie esp-app=absent" in message
    assert "Referer=https://192.0.2.170/user/loginpage" in message
    assert "secret" not in message


@pytest.mark.asyncio
async def test_notfound_with_an_error_body_is_a_credential_refusal():
    session = _FakeSession(_response(302, "/notfound", '{"error":1}'), [])
    with pytest.raises(AsicAuthError) as caught:
        await _login(session, BASE, "admin", "secret")
    assert "identifiants refusés" in str(caught.value)


@pytest.mark.asyncio
async def test_json_error_one_is_a_credential_refusal():
    session = _FakeSession(_response(200, body='{"error":1}'), [])
    with pytest.raises(AsicAuthError) as caught:
        await _login(session, BASE, "admin", "secret")
    assert "identifiants refusés" in str(caught.value)


@pytest.mark.asyncio
async def test_successful_login_returns_the_trace():
    session = _FakeSession(_response(302, "/"), ["esp-app"])
    step = await _login(session, BASE, "admin", "secret")
    assert "login : 302 → /" in step
    assert "X-Requested-With=XMLHttpRequest" in step
    assert "Origin=https://192.0.2.170" in step
