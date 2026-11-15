"""SSH client module for Fossor — asyncssh-based, host-key pinned, whitelisted commands only."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import logging
import textwrap
from typing import Any

import asyncssh

from app.secrets import decrypt_secret

logger = logging.getLogger("fossor.ssh")

# ── Whitelisted commands ──────────────────────────────────────────────
WHITELISTED_COMMANDS: dict[str, str] = {
    "identify": textwrap.dedent("""\
        hostname; whoami; uname -srm
        cat /etc/os-release 2>/dev/null | head -5
        test -d /hive && echo HIVEOS=1
    """).strip(),
}


# ── Host-key helpers ──────────────────────────────────────────────────
def _compute_fingerprint(raw_key_bytes: bytes) -> str:
    """Return SHA256 fingerprint of an SSH host-key blob."""
    digest = hashlib.sha256(raw_key_bytes).digest()
    b64 = base64.b64encode(digest).rstrip(b"=").decode()
    return f"SHA256:{b64}"


def _pin_host_key(machine_id: int, fingerprint: str) -> None:
    """Persist fingerprint in the Machine record (lazy import to avoid DB cycle)."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        m = db.query(Machine).filter(Machine.id == machine_id).first()
        if m:
            m.host_key = fingerprint
            db.commit()
    finally:
        db.close()


def _get_host_key(machine_id: int) -> str | None:
    """Return the pinned fingerprint, or None."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        m = db.query(Machine).filter(Machine.id == machine_id).first()
        return m.host_key if m else None
    finally:
        db.close()


# ── Audit helper ──────────────────────────────────────────────────────
def _audit_log(
    machine_id: int | None,
    action: str,
    result: str,
    output: str = "",
) -> None:
    """Write an audit log entry (lazy import to avoid DB cycle)."""
    from app.database import SessionLocal
    from app.models import AuditLog, ActorEnum

    db = SessionLocal()
    try:
        entry = AuditLog(
            actor=ActorEnum.hermes,
            action=action,
            machine_id=machine_id,
            result=result,
            output=output[:500],
        )
        db.add(entry)
        db.commit()
    finally:
        db.close()


# ── Host-key verification callback ────────────────────────────────────
def _make_host_key_callback(
    pinned_fp: str | None,
) -> Any:
    """Create a host_key_callback for asyncssh.connect.

    When pinned_fp is set, the callback verifies the remote host key
    matches the pinned SHA256 fingerprint. Raises asyncssh.HostKeyNotVerifiable
    on mismatch.
    """
    if not pinned_fp:
        return None

    expected = pinned_fp.replace("SHA256:", "")

    def callback(hostname: str, port: int, key_blob: bytes):
        actual_b64 = base64.b64encode(
            hashlib.sha256(key_blob).digest()
        ).rstrip(b"=").decode()
        if actual_b64 != expected:
            raise asyncssh.HostKeyNotVerifiable(
                f"Host key fingerprint changed. "
                f"Expected {pinned_fp}, got SHA256:{actual_b64}"
            )
        return True

    return callback


# ── Core SSH test ─────────────────────────────────────────────────────
async def _collect_identify(
    host: str,
    port: int,
    username: str,
    secret: str,
    auth_type: str,
    pinned_fp: str | None,
) -> dict[str, Any]:
    """Run the *identify* whitelisted command and return parsed results."""
    start = asyncio.get_running_loop().time() * 1000  # ms

    host_key_cb = _make_host_key_callback(pinned_fp)

    connect_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "known_hosts": None,
    }

    if host_key_cb is not None:
        connect_kwargs["host_key_callback"] = host_key_cb

    if auth_type == "key" and secret:
        connect_kwargs["client_keys"] = [secret]
    elif auth_type == "password" and secret:
        connect_kwargs["password"] = secret

    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(**connect_kwargs),
            timeout=10,
        )
        try:
            result = await asyncio.wait_for(
                conn.run(WHITELISTED_COMMANDS["identify"]),
                timeout=18,
            )
        finally:
            conn.close()
            await conn.wait_closed()

        elapsed = asyncio.get_running_loop().time() * 1000 - start

        # Parse output
        lines = (result.stdout or "").strip().splitlines()
        hostname_out = ""
        user_out = ""
        uname_out = ""
        os_kind = "other"

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("Linux") or stripped.startswith("Darwin"):
                uname_out = stripped
            elif stripped == "HIVEOS=1":
                os_kind = "hiveos"
            elif stripped.startswith("NAME="):
                if "ubuntu" in stripped.lower():
                    os_kind = "ubuntu"
                elif "proxmox" in stripped.lower():
                    os_kind = "proxmox"
            elif "@" in stripped:
                parts = stripped.split("@", 1)
                if parts[0] and len(parts[0]) < 64:
                    hostname_out = parts[0]
                if not user_out and parts[1]:
                    user_out = parts[1].split(".")[0]

        # Extract hostname from first line if no @-line found
        if not hostname_out and lines:
            first_line = lines[0].strip()
            if first_line and "@" not in first_line and len(first_line) < 64:
                hostname_out = first_line

        # Extract user from whoami line (non-empty, non-system, non-hostname)
        for wl in (result.stdout or "").splitlines():
            w = wl.strip()
            if not w:
                continue
            if "@" in w:
                continue
            if w.startswith("Linux") or w.startswith("Darwin"):
                continue
            if w.startswith("NAME=") or w.startswith("VERSION=") or w.startswith("PRETTY_NAME"):
                continue
            if w == "HIVEOS=1":
                continue
            # Skip lines already parsed as hostname (same line, no @)
            if not user_out and w == lines[0].strip() and "@" not in lines[0]:
                continue
            if not user_out and len(w) < 64 and not any(c.isdigit() for c in w):
                user_out = w

        # Get host key fingerprint
        fp_out = ""
        try:
            raw_key = conn.get_server_host_key().blob if hasattr(conn, 'get_server_host_key') else b""
            if raw_key:
                fp_out = _compute_fingerprint(raw_key)
        except Exception:
            pass

        return {
            "ok": True,
            "latency_ms": round(elapsed, 1),
            "hostname": hostname_out or "unknown",
            "user": user_out or username,
            "uname": uname_out or "unknown",
            "os_kind": os_kind,
            "host_key_fingerprint": fp_out,
        }

    except asyncssh.HostKeyNotVerifiable as exc:
        elapsed = asyncio.get_running_loop().time() * 1000 - start
        return {
            "ok": False,
            "latency_ms": round(elapsed, 1),
            "error": f"Host key fingerprint mismatch: {exc}",
            "os_kind": "other",
        }
    except asyncssh.PermissionDenied:
        elapsed = asyncio.get_running_loop().time() * 1000 - start
        return {
            "ok": False,
            "latency_ms": round(elapsed, 1),
            "error": "Permission denied — credentials rejected",
            "os_kind": "other",
        }
    except asyncio.TimeoutError:
        elapsed = asyncio.get_running_loop().time() * 1000 - start
        return {
            "ok": False,
            "latency_ms": round(elapsed, 1),
            "error": "SSH connection timed out (10 s connect / 20 s command)",
            "os_kind": "other",
        }
    except (asyncssh.Error, OSError, ConnectionError) as exc:
        elapsed = asyncio.get_running_loop().time() * 1000 - start
        return {
            "ok": False,
            "latency_ms": round(elapsed, 1),
            "error": f"Connection failed: {exc}",
            "os_kind": "other",
        }
    except Exception as exc:
        elapsed = asyncio.get_running_loop().time() * 1000 - start
        return {
            "ok": False,
            "latency_ms": round(elapsed, 1),
            "error": f"SSH error: {exc}",
            "os_kind": "other",
        }


# ── Public API ────────────────────────────────────────────────────────
async def ssh_test_machine(machine: Any) -> dict[str, Any]:
    """Test SSH connection to a Machine ORM object.

    Returns a dict with ok, latency_ms, hostname, user, uname,
    os_kind, host_key_fingerprint, and optionally error.
    """
    pinned_fp = machine.host_key  # may be None

    # Try to decrypt the secret
    secret = ""
    if machine.secret_ref:
        try:
            secret = decrypt_secret(machine.secret_ref)
        except Exception:
            secret = ""

    result = await _collect_identify(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=secret,
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
    )

    # Handle pinning on first successful connection
    if result["ok"] and result.get("host_key_fingerprint"):
        fp = result["host_key_fingerprint"]
        if not pinned_fp:
            # First contact — pin the key
            _pin_host_key(machine.id, fp)
            _audit_log(
                machine.id,
                "host-key-pinned",
                "ok",
                output=f"Host key pinned: {fp}",
            )

    # Log the audit entry (no secrets in output)
    if not result["ok"]:
        err_detail = result.get("error", "unknown error")
        _audit_log(machine.id, "ssh-test", "fail", output=err_detail)
    else:
        _audit_log(machine.id, "ssh-test", "ok", output=f"hostname={result.get('hostname')}, os={result.get('os_kind')}")

    return result


async def reset_machine_host_key(machine_id: int) -> dict[str, Any]:
    """Reset the pinned host key for a machine (admin action)."""
    _audit_log(machine_id, "host-key-reset", "ok", output="Host key reset by admin — next connection will pin new key")
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        m = db.query(Machine).filter(Machine.id == machine_id).first()
        if not m:
            return {"ok": False, "error": "Machine not found"}
        m.host_key = None
        db.commit()
        return {"ok": True, "message": "Host key cleared — next SSH test will pin a new key"}
    finally:
        db.close()
