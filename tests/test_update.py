"""Version comparison and the GitHub update check (§4.18, mocked API)."""

import json

import pytest

from app import update_checker
from app.version import compare_versions, is_valid_version, newest_version, versions_since


# ── Version ordering ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "older,newer",
    [
        ("2026.11.001", "2026.11.001-c1"),
        ("2026.11.001-c1", "2026.11.002"),
        ("2026.11.001-c1", "2026.11.001-c2"),
        ("2026.11.002", "2026.12.001"),
        ("2026.11.002-dev", "2026.11.002"),
        ("2026.11.002-dev", "2026.11.002-c1"),
        ("2025.12.099", "2026.01.001"),
    ],
)
def test_version_ordering(older, newer):
    assert compare_versions(older, newer) == -1
    assert compare_versions(newer, older) == 1


def test_equal_versions():
    assert compare_versions("2026.11.002-dev", "2026.11.002-dev") == 0


def test_malformed_version_is_rejected():
    assert is_valid_version("2026.11.2") is False
    assert is_valid_version("v2026.11.002") is False


def test_newest_and_versions_since():
    values = ["2026.11.001", "2026.11.002-dev", "2026.10.005", "pas-une-version"]
    assert newest_version(values) == "2026.11.002-dev"
    assert versions_since("2026.10.005", values) == ["2026.11.001", "2026.11.002-dev"]


# ── Mocked GitHub API ────────────────────────────────────────────────
RELEASES = [
    {"tag_name": "2026.12.001", "name": "Décembre", "body": "Notes 12.001",
     "published_at": "2026-12-01T10:00:00Z", "html_url": "https://example/12001"},
    {"tag_name": "2026.11.002", "name": "Novembre", "body": "Notes 11.002",
     "published_at": "2026-11-10T10:00:00Z", "html_url": "https://example/11002"},
    {"tag_name": "2026.11.001-c1", "name": "Correctif", "body": "Notes 11.001-c1",
     "published_at": "2026-11-02T10:00:00Z", "html_url": "https://example/11001c1"},
    {"tag_name": "2026.11.001", "name": "Novembre initial", "body": "Notes 11.001",
     "published_at": "2026-11-01T10:00:00Z", "html_url": "https://example/11001"},
]


@pytest.fixture(autouse=True)
def reset_update_state():
    update_checker.reset_state()
    yield
    update_checker.reset_state()


def fake_get(status, headers=None, body=""):
    calls = []

    def _get(url, request_headers=None, timeout=10):
        calls.append({"url": url, "headers": request_headers or {}, "timeout": timeout})
        return status, headers or {}, body

    _get.calls = calls
    return _get


def fake_raise(exc):
    def _get(url, request_headers=None, timeout=10):
        raise exc

    return _get


async def test_check_200_stores_releases_etag_and_notes(monkeypatch):
    monkeypatch.setattr(update_checker, "installed_version", lambda: "2026.11.001-c1")
    http = fake_get(200, {"ETag": 'W/"abc123"', "X-RateLimit-Remaining": "57"}, json.dumps(RELEASES))
    monkeypatch.setattr(update_checker, "_http_get", http)

    await update_checker.check()
    status = update_checker.status()

    assert http.calls[0]["url"].endswith("/repos/LostInTheBugs/fossor/releases")
    assert "If-None-Match" not in http.calls[0]["headers"]
    assert status["latest_version"] == "2026.12.001"
    assert status["update_available"] is True
    # Every release newer than the installed one, oldest first.
    assert [note["version"] for note in status["notes"]] == ["2026.11.002", "2026.12.001"]
    assert status["notes"][0]["body"] == "Notes 11.002"
    assert status["rate_limit_remaining"] == 57
    assert status["last_check_at"]
    assert status["error"] is None
    assert update_checker.STATE["etag"] == 'W/"abc123"'


async def test_304_reuses_cache(monkeypatch):
    monkeypatch.setattr(update_checker, "installed_version", lambda: "2026.11.001")
    monkeypatch.setattr(update_checker, "_http_get", fake_get(200, {"ETag": '"v1"'}, json.dumps(RELEASES)))
    await update_checker.check()
    before = update_checker.status()

    http = fake_get(304, {"ETag": '"v1"', "X-RateLimit-Remaining": "59"}, "")
    monkeypatch.setattr(update_checker, "_http_get", http)
    await update_checker.check()

    after = update_checker.status()
    assert http.calls[0]["headers"]["If-None-Match"] == '"v1"'
    assert after["latest_version"] == before["latest_version"]
    assert after["update_available"] is True
    assert after["error"] is None
    assert update_checker.STATE["not_modified"] == 1


async def test_403_rate_limit_does_not_crash(monkeypatch):
    monkeypatch.setattr(update_checker, "installed_version", lambda: "2026.11.001")
    monkeypatch.setattr(
        update_checker, "_http_get",
        fake_get(403, {"X-RateLimit-Remaining": "0"}, '{"message": "API rate limit exceeded"}'),
    )
    await update_checker.check()
    status = update_checker.status()
    assert "Limite de requêtes GitHub atteinte" in status["error"]
    assert status["rate_limit_remaining"] == 0
    assert status["update_available"] is False


async def test_timeout_is_swallowed(monkeypatch):
    monkeypatch.setattr(update_checker, "installed_version", lambda: "2026.11.001")
    monkeypatch.setattr(update_checker, "_http_get", fake_raise(TimeoutError("timed out")))
    await update_checker.check()  # must not raise
    status = update_checker.status()
    assert "TimeoutError" in status["error"]
    assert status["last_check_at"]


async def test_installed_dev_is_newer_than_the_last_published_c1(monkeypatch):
    """The real case of this step: installed 2026.11.002-dev, published 2026.11.001-c1."""
    monkeypatch.setattr(update_checker, "installed_version", lambda: "2026.11.002-dev")
    published = [
        {"tag_name": "2026.11.001-c1", "name": "Correctif 1b-2", "body": "Corrections",
         "published_at": "2026-11-16T09:00:00Z", "html_url": "https://example/c1"},
        {"tag_name": "2026.11.001", "name": "Phase 1", "body": "Socle",
         "published_at": "2026-11-15T09:00:00Z", "html_url": "https://example/001"},
    ]
    monkeypatch.setattr(update_checker, "_http_get", fake_get(200, {}, json.dumps(published)))
    await update_checker.check()
    status = update_checker.status()
    assert status["latest_version"] == "2026.11.001-c1"
    assert status["update_available"] is False
    assert status["notes"] == []


async def test_disabled_by_configuration(monkeypatch):
    monkeypatch.setenv("FOSSOR_UPDATE_CHECK_INTERVAL", "0")
    status = update_checker.status()
    assert status["check_enabled"] is False
    assert update_checker.start() is None


@pytest.mark.network
async def test_real_github_api_optional():
    """Optional real call: ``pytest -m network`` (never part of the standard suites)."""
    await update_checker.check(force=True)
    status = update_checker.status()
    assert status["error"] is None
    assert status["latest_version"]


async def test_status_endpoint(client, monkeypatch):
    monkeypatch.setattr(update_checker, "installed_version", lambda: "2026.11.001")
    monkeypatch.setattr(update_checker, "_http_get", fake_get(200, {}, json.dumps(RELEASES)))
    await update_checker.check()
    response = await client.get("/api/update/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["installed_version"] == "2026.11.001"
    assert payload["latest_version"] == "2026.12.001"
    assert payload["update_available"] is True
    assert len(payload["notes"]) == 3
