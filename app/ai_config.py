"""AI analysis configuration (§4.12).

* providers live in ``ai_providers``: exactly one ``primary`` and an optional
  ``fallback``; the API key is encrypted with the master key like a machine
  secret and is **never** returned (only ``has_api_key``);
* global switches (``analysis_cadence_hours``, ``fallback_enabled``) live in the
  ``settings`` table so they travel with the export;
* the inbound Hermes token (§5.4) is stored **hashed** (SHA-256) and shown once,
  at creation time, then never again — and it is never exported.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets as pysecrets
import uuid as uuid_lib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AiProvider, AppSetting
from app.secrets import decrypt_secret, encrypt_secret

PROVIDER_TYPES = ("hermes-agent", "openai-compatible-local", "cloud-api")
WHICHES = ("primary", "fallback")
REASONING_EFFORTS = ("", "low", "medium", "high", "xhigh")

CADENCE_KEY = "analysis_cadence_hours"
FALLBACK_KEY = "fallback_enabled"
TOKEN_HASH_KEY = "hermes_token_hash"
TOKEN_CREATED_KEY = "hermes_token_created_at"
# Never written to an export, never returned by the API.
NON_EXPORTED_SETTINGS = frozenset(
    {
        TOKEN_HASH_KEY,
        TOKEN_CREATED_KEY,
        # §4.1 bis: the private half is master-key encrypted at rest and only
        # travels inside the passphrase-encrypted ``secrets`` block.
        "fossor_ssh_private_key",
    }
)

DEFAULT_CADENCE_HOURS = 6
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_TOKENS = 1024
PROVIDER_FIELDS = (
    "provider_type", "name", "base_url", "model", "timeout_s", "max_tokens",
    "reasoning_effort", "extra_body", "enabled",
)
LIMITS = {"timeout_s": (1, 600), "max_tokens": (1, 32768)}


class AIError(ValueError):
    """Invalid AI configuration — surfaced as HTTP 400."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Settings ───────────────────────────────────────────────────────────
def get_setting(db: Session, key: str, default):
    row = db.get(AppSetting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return row.value


def set_setting(db: Session, key: str, value) -> None:
    row = db.get(AppSetting, key)
    payload = json.dumps(value, ensure_ascii=False)
    if row is None:
        db.add(AppSetting(key=key, value=payload, updated_at=_now()))
    else:
        row.value = payload
        row.updated_at = _now()


def cadence_hours(db: Session) -> int:
    value = get_setting(db, CADENCE_KEY, DEFAULT_CADENCE_HOURS)
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_CADENCE_HOURS


def fallback_enabled(db: Session) -> bool:
    return bool(get_setting(db, FALLBACK_KEY, True))


# ── Providers ──────────────────────────────────────────────────────────
def _parse_extra_body(raw) -> dict | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw or None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise AIError("extra_body n'est pas un JSON valide")
        if not isinstance(parsed, dict):
            raise AIError("extra_body doit être un objet JSON")
        return parsed or None
    raise AIError("extra_body doit être un objet JSON")


def provider_dict(row: AiProvider | None) -> dict | None:
    """API view of a provider: never the key, only ``has_api_key``."""
    if row is None:
        return None
    extra = None
    if row.extra_body:
        try:
            extra = json.loads(row.extra_body)
        except ValueError:
            extra = None
    return {
        "which": row.which,
        "provider_type": row.provider_type,
        "name": row.name,
        "base_url": row.base_url,
        "has_api_key": bool(row.api_key_ref),
        "model": row.model,
        "timeout_s": row.timeout_s,
        "max_tokens": row.max_tokens,
        "reasoning_effort": row.reasoning_effort or "",
        "extra_body": extra,
        "enabled": bool(row.enabled),
        "updated_at": row.updated_at,
    }


def _validate(values: dict) -> dict:
    """Check the fields we are about to store, return them normalized."""
    normalized = dict(values)
    ptype = normalized.get("provider_type")
    if ptype not in PROVIDER_TYPES:
        raise AIError(
            "provider_type invalide : " + ", ".join(f"« {p} »" for p in PROVIDER_TYPES) + " attendu"
        )
    base_url = (normalized.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise AIError("base_url doit commencer par http:// ou https://")
    normalized["base_url"] = base_url
    for field, (low, high) in LIMITS.items():
        try:
            number = int(normalized.get(field))
        except (TypeError, ValueError):
            raise AIError(f"{field} doit être un entier")
        if not low <= number <= high:
            raise AIError(f"{field} doit être entre {low} et {high}")
        normalized[field] = number
    effort = normalized.get("reasoning_effort")
    if effort is not None and effort not in REASONING_EFFORTS:
        raise AIError("reasoning_effort invalide (low, medium, high, xhigh ou vide)")
    normalized["reasoning_effort"] = effort or None
    return normalized


def _apply_provider(db: Session, which: str, payload: dict) -> tuple[AiProvider, bool]:
    row = db.query(AiProvider).filter(AiProvider.which == which).first()
    created = row is None
    if row is None:
        row = AiProvider(which=which, name=which)
        db.add(row)

    # Only the fields actually sent are touched: a partial PATCH never wipes
    # the API key, the extra_body or the reasoning effort.
    values = {field: payload[field] for field in PROVIDER_FIELDS if field in payload}
    if created:
        values.setdefault("provider_type", "hermes-agent")
        values.setdefault("timeout_s", DEFAULT_TIMEOUT_S)
        values.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        values.setdefault("enabled", True)
    if "extra_body" in values:
        parsed = _parse_extra_body(values["extra_body"])
        values["extra_body"] = json.dumps(parsed, ensure_ascii=False) if parsed else None

    effective = {
        field: values.get(field, getattr(row, field, None))
        for field in ("provider_type", "base_url", "timeout_s", "max_tokens", "reasoning_effort")
    }
    values.update({f: v for f, v in _validate(effective).items() if f in values})

    if payload.get("api_key"):
        row.api_key_ref = encrypt_secret(str(payload["api_key"]))
    elif payload.get("clear_api_key"):
        row.api_key_ref = None

    for field, value in values.items():
        setattr(row, field, value)
    row.updated_at = _now()
    return row, created


def patch_config(db: Session, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise AIError("Corps JSON attendu")
    unknown = set(payload) - set(WHICHES) - {CADENCE_KEY, FALLBACK_KEY}
    if unknown:
        raise AIError("Champ inconnu : " + ", ".join(sorted(unknown)))

    providers = []
    for which in WHICHES:
        if which in payload:
            if payload[which] is None:
                row = db.query(AiProvider).filter(AiProvider.which == which).first()
                if row is not None:
                    db.delete(row)
                continue
            if not isinstance(payload[which], dict):
                raise AIError(f"{which} doit être un objet JSON")
            row, _created = _apply_provider(db, which, payload[which])
            providers.append(row)

    if CADENCE_KEY in payload:
        try:
            cadence = int(payload[CADENCE_KEY])
        except (TypeError, ValueError):
            raise AIError("analysis_cadence_hours doit être un entier")
        if not 1 <= cadence <= 168:
            raise AIError("analysis_cadence_hours doit être entre 1 et 168")
        set_setting(db, CADENCE_KEY, cadence)
    if FALLBACK_KEY in payload:
        set_setting(db, FALLBACK_KEY, bool(payload[FALLBACK_KEY]))

    db.commit()
    return providers


def snapshot(db: Session) -> dict:
    rows = {row.which: row for row in db.query(AiProvider).all()}
    return {
        "primary": provider_dict(rows.get("primary")),
        "fallback": provider_dict(rows.get("fallback")),
        "analysis_cadence_hours": cadence_hours(db),
        "fallback_enabled": fallback_enabled(db),
        "hermes_token": token_info(db),
    }


def get_provider(db: Session, which: str) -> AiProvider | None:
    return db.query(AiProvider).filter(AiProvider.which == which).first()


def provider_secret(row: AiProvider) -> str | None:
    if not row.api_key_ref:
        return None
    try:
        return decrypt_secret(row.api_key_ref)
    except ValueError:
        return None


# ── Inbound Hermes token (§5.4) ────────────────────────────────────────
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_info(db: Session) -> dict:
    hashed = get_setting(db, TOKEN_HASH_KEY, None)
    created = get_setting(db, TOKEN_CREATED_KEY, None)
    return {"has_token": bool(hashed), "created_at": created}


def new_token(db: Session) -> dict:
    """Generate a token; the clear value is returned **once** and never stored."""
    token = pysecrets.token_urlsafe(32)
    created_at = _now()
    set_setting(db, TOKEN_HASH_KEY, hash_token(token))
    set_setting(db, TOKEN_CREATED_KEY, created_at)
    db.commit()
    return {"token": token, "created_at": created_at}


def check_token(db: Session, token: str) -> bool:
    stored = get_setting(db, TOKEN_HASH_KEY, None)
    if not stored:
        return False
    return hmac.compare_digest(hash_token(token), str(stored))


def new_uuid() -> str:
    return str(uuid_lib.uuid4())
