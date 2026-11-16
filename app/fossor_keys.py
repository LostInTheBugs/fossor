"""Fossor's own SSH identity (§4.1 bis).

One ed25519 key pair per installation, generated at the first start and shared
by every node of the cluster once §4.11 lands.

* the private key is encrypted with the master key and stored in the
  ``settings`` table (``fossor_ssh_private_key``) — never on disk in clear,
  never exported in clear;
* the public key is stored in clear (``fossor_ssh_public_key``) with the
  comment ``fossor@<instance>``;
* both survive a restart because they live in the database, and the private
  half travels (encrypted with the export passphrase) inside the ``secrets``
  block of the configuration export.
"""

from __future__ import annotations

import logging
import re
import socket
from typing import Any

import asyncssh

logger = logging.getLogger("fossor.keys")

PRIVATE_KEY_SETTING = "fossor_ssh_private_key"
PUBLIC_KEY_SETTING = "fossor_ssh_public_key"
INSTANCE_NAME_SETTING = "instance_name"

_KEY_TYPE = "ssh-ed25519"


def _session():
    from app.database import SessionLocal

    return SessionLocal()


def instance_name(db) -> str:
    """Sanitised instance name used in the public key comment."""
    from app.ai_config import get_setting

    raw = str(get_setting(db, INSTANCE_NAME_SETTING, "") or "").strip()
    if not raw:
        raw = socket.gethostname().split(".")[0] or "fossor"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-") or "fossor"
    return cleaned[:32]


def _public_part(line: str) -> str:
    parts = (line or "").split()
    return " ".join(parts[:2]) if len(parts) >= 2 else ""


def ensure_key(db=None) -> dict[str, Any]:
    """Return the stored key pair, generating it on the very first call.

    ``{"public_key": ..., "created": bool}`` — the private half never leaves
    this module in clear except through :func:`private_key_text`.
    """
    from app.ai_config import get_setting, set_setting
    from app.secrets import decrypt_secret, encrypt_secret

    own = db is None
    db = db or _session()
    try:
        stored_private = get_setting(db, PRIVATE_KEY_SETTING, None)
        stored_public = get_setting(db, PUBLIC_KEY_SETTING, None)
        if stored_private and stored_public:
            try:
                key = asyncssh.import_private_key(decrypt_secret(stored_private))
                if _public_part(key.export_public_key().decode()) == _public_part(stored_public):
                    return {"public_key": stored_public, "created": False}
                logger.warning("Fossor SSH key mismatch with its public half — regenerating")
            except Exception:
                # A master key rotation makes the stored private key unreadable:
                # the public key is then useless, regenerate a coherent pair.
                logger.warning("Fossor SSH private key unreadable — regenerating")
        key = asyncssh.generate_private_key(_KEY_TYPE, comment=f"fossor@{instance_name(db)}")
        public_line = key.export_public_key().decode().strip()
        set_setting(db, PRIVATE_KEY_SETTING, encrypt_secret(key.export_private_key().decode()))
        set_setting(db, PUBLIC_KEY_SETTING, public_line)
        db.commit()
        logger.info("Fossor SSH identity generated (%s)", _public_part(public_line)[:40])
        return {"public_key": public_line, "created": True}
    finally:
        if own:
            db.close()


def public_key_text(db=None) -> str:
    """Full OpenSSH public key line, comment ``fossor@<instance>`` included."""
    from app.ai_config import get_setting

    own = db is None
    db = db or _session()
    try:
        ensure_key(db)
        return str(get_setting(db, PUBLIC_KEY_SETTING, "") or "").strip()
    finally:
        if own:
            db.close()


def private_key_text(db=None) -> str:
    """Decrypted OpenSSH private key (in memory only, for asyncssh)."""
    from app.ai_config import get_setting
    from app.secrets import decrypt_secret

    own = db is None
    db = db or _session()
    try:
        ensure_key(db)
        return decrypt_secret(str(get_setting(db, PRIVATE_KEY_SETTING, "") or ""))
    finally:
        if own:
            db.close()


def restore_private_key(private_text: str, db=None) -> str:
    """Install a private key coming from an import; returns the public line."""
    from app.ai_config import set_setting
    from app.secrets import encrypt_secret

    key = asyncssh.import_private_key(private_text)
    comment = f"fossor@{instance_name(db) if db is not None else 'fossor'}"
    public_line = key.export_public_key().decode().strip()
    # Keep the imported key material, but refresh the comment so the public key
    # matches this instance.
    if key.algorithm == _KEY_TYPE:
        public_line = f"{_public_part(public_line)} {comment}"
    own = db is None
    db = db or _session()
    try:
        set_setting(db, PRIVATE_KEY_SETTING, encrypt_secret(private_text))
        set_setting(db, PUBLIC_KEY_SETTING, public_line)
        db.commit()
        return public_line
    finally:
        if own:
            db.close()
