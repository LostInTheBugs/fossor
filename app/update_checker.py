"""GitHub release client (§4.18, part 1): check, notify, show the notes.

The update itself is out of scope for this step.  Everything here is
best-effort: a network error, a rate limit or a malformed answer must never
block the application nor raise out of the background task.

Configuration: ``FOSSOR_UPDATE_CHECK_INTERVAL`` (seconds, default 3600,
``0`` disables the check).  The first check is delayed by a random
30–300 s jitter so a fleet restarting together does not hammer GitHub.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from app.version import compare_versions, is_valid_version, newest_version, parse_version

REPO = "LostInTheBugs/fossor"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"


def releases_url() -> str:
    """URL des releases GitHub, surchargeable hors ligne (``FOSSOR_RELEASES_URL``)."""
    return (os.environ.get("FOSSOR_RELEASES_URL") or RELEASES_URL).strip()
DEFAULT_INTERVAL = 3600
JITTER_MIN = 30
JITTER_MAX = 300
TIMEOUT = 10
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

STATE: dict = {
    "last_check_at": None,
    "etag": None,
    "releases": [],
    "latest_version": None,
    "error": None,
    "rate_limit_remaining": None,
    "not_modified": 0,
    "checks": 0,
}

_task: asyncio.Task | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def installed_version() -> str:
    return VERSION_FILE.read_text().strip()


def check_interval() -> int:
    raw = os.environ.get("FOSSOR_UPDATE_CHECK_INTERVAL")
    if raw is None or raw.strip() == "":
        return DEFAULT_INTERVAL
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_INTERVAL


def reset_state() -> None:
    """Test helper: forget everything cached from a previous run."""
    global _task
    STATE.update({
        "last_check_at": None,
        "etag": None,
        "releases": [],
        "latest_version": None,
        "error": None,
        "rate_limit_remaining": None,
        "not_modified": 0,
        "checks": 0,
    })
    _task = None


def _header(headers, name: str):
    """Case-insensitive header lookup (``dict(HTTPMessage)`` keeps the wire case)."""
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return value
    return None


def _http_get(url: str, headers: dict | None = None, timeout: int = TIMEOUT):
    """Blocking GET -> ``(status, headers, body)``; HTTP errors are returned, not raised."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"fossor/{installed_version()}",
            "Accept": "application/vnd.github+json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return exc.code, dict(exc.headers or {}), body


def _parse_releases(payload: str) -> list[dict]:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    releases = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not is_valid_version(tag):
            continue
        releases.append({
            "version": tag,
            "name": item.get("name") or tag,
            "body": item.get("body") or "",
            "published_at": item.get("published_at"),
            "url": item.get("html_url"),
            "prerelease": bool(item.get("prerelease")),
        })
    releases.sort(key=lambda r: parse_version(r["version"]), reverse=True)
    return releases


async def check(force: bool = False) -> dict:
    """One check against GitHub.  Never raises."""
    state = STATE
    headers = {}
    if state["etag"] and not force:
        headers["If-None-Match"] = state["etag"]

    try:
        status, response_headers, body = await asyncio.to_thread(
            _http_get, releases_url(), headers, TIMEOUT
        )
    except Exception as exc:  # timeout, DNS, TLS, …
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["last_check_at"] = _now()
        state["checks"] += 1
        return state

    remaining = _header(response_headers, "X-RateLimit-Remaining")
    state["rate_limit_remaining"] = (
        int(remaining) if remaining is not None and str(remaining).isdigit() else None
    )
    state["last_check_at"] = _now()
    state["checks"] += 1

    if status == 304:
        state["not_modified"] += 1
        state["error"] = None
    elif status == 200:
        releases = _parse_releases(body)
        state["releases"] = releases
        state["latest_version"] = newest_version(r["version"] for r in releases)
        state["etag"] = _header(response_headers, "ETag") or state["etag"]
        state["error"] = None
        state["not_modified"] = 0
    elif status == 403:
        state["error"] = (
            "Limite de requêtes GitHub atteinte"
            f" (X-RateLimit-Remaining={remaining if remaining is not None else '?'})"
        )
    else:
        state["error"] = f"Réponse GitHub inattendue : HTTP {status}"
    return state


def status() -> dict:
    """Payload of ``GET /api/update/status``."""
    installed = installed_version()
    latest = STATE["latest_version"]
    notes = [
        {
            "version": release["version"],
            "name": release["name"],
            "body": release["body"],
            "published_at": release["published_at"],
            "url": release["url"],
        }
        for release in sorted(STATE["releases"], key=lambda r: parse_version(r["version"]))
        if compare_versions(release["version"], installed) > 0
    ]
    update_available = bool(latest and compare_versions(latest, installed) > 0)
    interval = check_interval()
    return {
        "installed_version": installed,
        "latest_version": latest,
        "update_available": update_available,
        "last_check_at": STATE["last_check_at"],
        "check_interval_seconds": interval,
        "check_enabled": interval > 0,
        "rate_limit_remaining": STATE["rate_limit_remaining"],
        "error": STATE["error"],
        "notes": notes,
        "repository": REPO,
    }


def known_versions() -> list[str]:
    """Tags publiés connus du dernier ``check`` (2026.11.004-c5).

    ``status()`` ne liste que les versions plus récentes que celle installée
    (``notes``) ; une cible explicitement demandée doit pouvoir être validée
    même si elle est plus ancienne (retour arrière).
    """
    return [str(release.get("version")) for release in STATE["releases"]]


async def _loop() -> None:
    await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))
    while True:
        try:
            await check()
        except Exception:  # pragma: no cover - belt and braces
            pass
        await asyncio.sleep(max(60, check_interval()))


def start() -> asyncio.Task | None:
    """Start the background loop if enabled; safe to call twice."""
    global _task
    if check_interval() <= 0:
        return None
    if _task is not None and not _task.done():
        return _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    _task = loop.create_task(_loop())
    return _task


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
