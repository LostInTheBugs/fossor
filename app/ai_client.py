"""Minimal OpenAI-compatible probe (« Tester », §4.12).

One ``POST {base_url}/chat/completions`` with a two-word question; the answer
is only used to prove the credential and the model work.  The API key travels
in the ``Authorization`` header and is redacted from every error we return, so
it cannot leak into the API response, the audit log or the container logs.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

# Overridden by tests (httpx.MockTransport) — never set in production.
TRANSPORT: httpx.AsyncBaseTransport | None = None

PROBE_PROMPT = "Réponds uniquement OK"
PROBE_MAX_TOKENS = 20


def build_payload(provider: dict) -> dict:
    """Request body: defaults, optional reasoning effort, then ``extra_body``.

    ``extra_body`` is merged **last** on purpose: Hermes uses it to pick the
    underlying provider/model (``{"provider": "deepseek", "model": "…",
    "require_model_lock": true}``), and that must win over the form's model.
    """
    payload: dict[str, Any] = {
        "model": provider.get("model") or "hermes-agent",
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }
    if provider.get("reasoning_effort"):
        payload["reasoning_effort"] = provider["reasoning_effort"]
    extra = provider.get("extra_body") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    if isinstance(extra, dict) and extra:
        payload.update(extra)
    return payload


def _redact(text: str, secret: str | None, limit: int = 200) -> str:
    text = str(text or "")
    if secret and secret in text:
        text = text.replace(secret, "***")
    text = " ".join(text.split())
    return text[:limit]


def _extract_content(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    content = (choice.get("message") or {}).get("content")
    if content is None:
        content = choice.get("text")
    if isinstance(content, list):  # OpenAI "parts" form
        content = " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


async def probe_provider(provider: dict, api_key: str | None) -> dict:
    """Return ``{ok, latency_ms, model, extract}`` or ``{ok: False, error}``."""
    base_url = (provider.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return {"ok": False, "error": "Adresse (base_url) vide"}

    payload = build_payload(provider)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = float(provider.get("timeout_s") or 120)

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=TRANSPORT) as client:
            response = await client.post(
                f"{base_url}/chat/completions", json=payload, headers=headers
            )
    except httpx.TimeoutException:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"Délai dépassé ({timeout:.0f} s)",
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": _redact(f"Connexion impossible : {exc}", api_key),
        }

    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code != 200:
        if response.status_code in (401, 403):
            error = f"HTTP {response.status_code} : clé refusée"
        else:
            error = f"HTTP {response.status_code} : {_redact(response.text, api_key, 120)}"
        return {"ok": False, "latency_ms": latency_ms, "error": error}

    try:
        data = response.json()
    except ValueError:
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": _redact(f"Réponse non JSON ({response.text[:120]})", api_key),
        }
    if not isinstance(data, dict):
        return {"ok": False, "latency_ms": latency_ms, "error": "Réponse non JSON (objet attendu)"}

    extract = _extract_content(data)
    if not extract:
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": "Réponse sans contenu (choices vide)",
        }
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "model": data.get("model") or payload.get("model"),
        "extract": extract[:60],
    }
