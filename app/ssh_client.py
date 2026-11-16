"""SSH client module for Fossor — asyncssh-based, host-key pinned, whitelisted commands only.

Host-key policy
---------------
* First contact (nothing pinned): the server host key is read **without
  authentication** through ``asyncssh.get_server_host_key()``, then pinned
  (full public key + SHA256 fingerprint) *before* any credential is sent.
* Following contacts: the pinned public key is handed to ``asyncssh.connect()``
  via ``known_hosts=([key], [], [])`` so asyncssh itself rejects a different
  key during the handshake, i.e. before password/public-key auth.
* A fingerprint mismatch is refused explicitly, with both fingerprints in the
  error message, and written to the audit log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import textwrap
from typing import Any

import asyncssh

from app.secrets import decrypt_secret

logger = logging.getLogger("fossor.ssh")

CONNECT_TIMEOUT = 10
COMMAND_TIMEOUT = 20
HOST_KEY_TIMEOUT = 8

# ── Whitelisted commands ──────────────────────────────────────────────
WHITELISTED_COMMANDS: dict[str, str] = {
    "identify": textwrap.dedent("""\
        hostname; whoami; uname -srm
        cat /etc/os-release 2>/dev/null | head -5
        test -d /hive && echo HIVEOS=1
    """).strip(),
    # Read-only hardware inventory. Every tool is optional: a missing tool
    # leaves its section empty instead of failing the command (exit 0).
    "detect_hw": textwrap.dedent("""\
        echo '### CPU'
        lscpu -J 2>/dev/null || cat /proc/cpuinfo 2>/dev/null
        nproc 2>/dev/null
        grep -m1 '^flags' /proc/cpuinfo 2>/dev/null
        echo '### MEM'
        free -b 2>/dev/null
        echo '### GPU_NVIDIA'
        if command -v nvidia-smi >/dev/null 2>&1; then
          nvidia-smi --query-gpu=index,name,memory.total,pci.bus_id,driver_version,uuid --format=csv,noheader,nounits 2>/dev/null
        fi
        echo '### GPU_PCI'
        if command -v lspci >/dev/null 2>&1; then
          lspci -nn 2>/dev/null | grep -Ei 'vga|3d|display'
        fi
        echo '### VIRT'
        test -e /dev/kvm && echo KVM=1
        command -v virsh >/dev/null 2>&1 && echo VIRSH=1
        command -v pveversion >/dev/null 2>&1 && echo PVERSION=1
        echo '### OS'
        hostname 2>/dev/null
        uname -srm 2>/dev/null
        cat /etc/os-release 2>/dev/null | head -5
        test -d /hive && echo HIVEOS=1
        exit 0
    """).strip(),
    # Live mining snapshot (§8 phase 2). Read-only, no sudo: HiveOS dumps are
    # world-readable, nvidia-smi and ps are unprivileged. Every tool is
    # optional; a missing file/tool leaves its section empty (exit 0).
    "collect_stats": textwrap.dedent("""\
        echo '### HIVE_STAT'
        cat /run/hive/last_stat.json 2>/dev/null
        echo '### HIVE_GPU'
        cat /run/hive/gpu-stats.json 2>/dev/null
        echo '### NVSMI'
        if command -v nvidia-smi >/dev/null 2>&1; then
          nvidia-smi --query-gpu=index,pci.bus_id,temperature.gpu,power.draw,fan.speed,utilization.gpu --format=csv,noheader,nounits 2>/dev/null
        fi
        echo '### PS_MINERS'
        ps -eo args 2>/dev/null | grep -Ei 'srbminer|xmrig|t-rex|lolminer|gminer|nbminer|phoenixminer|teamredminer' | grep -v grep
        echo '### UPTIME'
        cat /proc/uptime 2>/dev/null
        exit 0
    """).strip(),
}


# ── Host-key helpers ──────────────────────────────────────────────────
def _fingerprint_of(key: Any) -> str:
    """SHA256 fingerprint of an asyncssh public key, OpenSSH format."""
    return key.get_fingerprint("sha256")


def _public_key_of(key: Any) -> str:
    """Full OpenSSH public key line of an asyncssh public key."""
    return key.export_public_key().decode().strip()


async def read_server_host_key(host: str, port: int) -> Any:
    """Read the server host key without authenticating anything."""
    return await asyncio.wait_for(
        asyncssh.get_server_host_key(host, port),
        timeout=HOST_KEY_TIMEOUT,
    )


def _pin_host_key(machine_id: int, fingerprint: str, public_key: str) -> None:
    """Persist host key data in the Machine record."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        m = db.query(Machine).filter(Machine.id == machine_id).first()
        if m:
            m.host_key = fingerprint
            m.host_key_public = public_key
            db.commit()
    finally:
        db.close()


def _get_host_key_data(machine_id: int) -> tuple[str | None, str | None]:
    """Return (fingerprint, full_public_key) or (None, None)."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        m = db.query(Machine).filter(Machine.id == machine_id).first()
        if not m:
            return None, None
        return m.host_key, m.host_key_public
    finally:
        db.close()


# ── Audit helper ──────────────────────────────────────────────────────
def _audit_log(
    machine_id: int | None,
    action: str,
    result: str,
    output: str = "",
) -> None:
    """Write an audit log entry."""
    from app.database import SessionLocal
    from app.models import ActorEnum, AuditLog

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


# ── Internals ─────────────────────────────────────────────────────────
def _failure(start_ms: float, message: str, fingerprint: str = "") -> dict[str, Any]:
    elapsed = asyncio.get_running_loop().time() * 1000 - start_ms
    return {
        "ok": False,
        "latency_ms": round(elapsed, 1),
        "error": message,
        "os_kind": "other",
        "host_key_fingerprint": fingerprint,
    }


def _parse_identify(stdout: str, fallback_user: str) -> dict[str, str]:
    """Parse identify output (hostname / whoami / uname -srm / os-release)."""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    hostname = ""
    user = ""
    uname = ""
    os_kind = "other"

    for line in lines:
        if line.startswith("Linux") or line.startswith("Darwin"):
            uname = line
        elif line == "HIVEOS=1":
            os_kind = "hiveos"
        elif line.startswith("NAME="):
            low = line.lower()
            if "ubuntu" in low:
                os_kind = "ubuntu"
            elif "proxmox" in low:
                os_kind = "proxmox"
        elif not hostname:
            hostname = line
        elif not user:
            user = line

    return {
        "hostname": hostname or "unknown",
        "user": user or fallback_user,
        "uname": uname or "unknown",
        "os_kind": os_kind,
    }


def _auth_kwargs(auth_type: str, secret: str) -> tuple[dict[str, Any], str | None]:
    """Build credential kwargs; returns (kwargs, temporary private-key path)."""
    if auth_type == "key" and secret:
        if os.path.isfile(secret):
            return {"client_keys": [secret]}, None
        fd, tmp_key = tempfile.mkstemp(suffix="_fossor_key")
        with os.fdopen(fd, "wb") as fh:
            fh.write(secret.encode())
        os.chmod(tmp_key, 0o600)
        return {"client_keys": [tmp_key]}, tmp_key
    if auth_type == "password" and secret:
        return {"password": secret}, None
    return {}, None


async def _run_whitelisted(
    host: str,
    port: int,
    username: str,
    secret: str,
    auth_type: str,
    pinned_fp: str | None,
    pinned_public_key: str | None,
    command: str,
    machine_id: int | None = None,
    audit_kind: str = "ssh-test",
) -> dict[str, Any]:
    """Pin/verify the host key, connect, then run one whitelisted *command*."""
    start = asyncio.get_running_loop().time() * 1000

    # 1) Read the host key without authenticating anything.
    try:
        server_key = await read_server_host_key(host, port)
    except asyncio.TimeoutError:
        return _failure(start, f"SSH host key read timed out ({HOST_KEY_TIMEOUT} s)")
    except (asyncssh.Error, OSError) as exc:
        return _failure(start, f"Connection failed: {exc}")

    new_fp = _fingerprint_of(server_key)
    new_pub = _public_key_of(server_key)

    # 2) Pinned? Verify before the credential leaves the process.
    trusted_key = None
    if pinned_fp:
        if not pinned_public_key:
            msg = (
                f"Empreinte épinglée ({pinned_fp}) sans clé publique enregistrée — "
                "connexion refusée, lancez reset-host-key"
            )
            if machine_id is not None:
                _audit_log(machine_id, "host-key-check", "fail", msg)
                _audit_log(machine_id, audit_kind, "fail", msg)
            return _failure(start, msg, new_fp)
        if new_fp != pinned_fp:
            msg = f"Empreinte changée : ancienne {pinned_fp}, nouvelle {new_fp}"
            if machine_id is not None:
                _audit_log(machine_id, "host-key-mismatch", "fail", msg)
                _audit_log(machine_id, audit_kind, "fail", msg)
            return _failure(start, msg, new_fp)
        try:
            trusted_key = asyncssh.import_public_key(pinned_public_key)
        except Exception:
            msg = "Clé publique épinglée illisible — connexion refusée, lancez reset-host-key"
            if machine_id is not None:
                _audit_log(machine_id, "host-key-check", "fail", msg)
                _audit_log(machine_id, audit_kind, "fail", msg)
            return _failure(start, msg, new_fp)

    # 3) First contact: pin now, before sending any credential.
    if trusted_key is None:
        if machine_id is not None:
            _pin_host_key(machine_id, new_fp, new_pub)
            _audit_log(machine_id, "host-key-pinned", "ok", f"Host key pinned: {new_fp}")
        trusted_key = server_key

    # 4) Connect with the pinned key handed to asyncssh: verified during the
    #    handshake, i.e. before authentication.
    connect_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "known_hosts": ([trusted_key], [], []),
        "connect_timeout": CONNECT_TIMEOUT,
    }
    auth_kwargs, tmp_key = _auth_kwargs(auth_type, secret)
    connect_kwargs.update(auth_kwargs)

    try:
        conn = await asyncio.wait_for(asyncssh.connect(**connect_kwargs), timeout=CONNECT_TIMEOUT)
        try:
            result = await asyncio.wait_for(
                conn.run(command),
                timeout=COMMAND_TIMEOUT,
            )
        finally:
            conn.close()
            await conn.wait_closed()
    except asyncssh.HostKeyNotVerifiable:
        msg = f"Empreinte changée : ancienne {pinned_fp or 'aucune'}, nouvelle {new_fp}"
        if machine_id is not None:
            _audit_log(machine_id, audit_kind, "fail", msg)
        return _failure(start, msg, new_fp)
    except asyncssh.PermissionDenied:
        return _failure(start, "Permission denied — credentials rejected", new_fp)
    except asyncio.TimeoutError:
        return _failure(
            start,
            f"SSH connection timed out ({CONNECT_TIMEOUT} s connect / {COMMAND_TIMEOUT} s command)",
            new_fp,
        )
    except (asyncssh.Error, OSError, ConnectionError) as exc:
        return _failure(start, f"Connection failed: {exc}", new_fp)
    except Exception as exc:  # pragma: no cover - defensive
        return _failure(start, f"SSH error: {exc}", new_fp)
    finally:
        if tmp_key:
            try:
                os.unlink(tmp_key)
            except OSError:
                pass

    elapsed = asyncio.get_running_loop().time() * 1000 - start
    return {
        "ok": True,
        "latency_ms": round(elapsed, 1),
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "host_key_fingerprint": new_fp,
    }


async def _collect_identify(
    host: str,
    port: int,
    username: str,
    secret: str,
    auth_type: str,
    pinned_fp: str | None,
    pinned_public_key: str | None,
    machine_id: int | None = None,
) -> dict[str, Any]:
    """Pin/verify the host key, connect, then run the *identify* command."""
    raw = await _run_whitelisted(
        host=host,
        port=port,
        username=username,
        secret=secret,
        auth_type=auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=WHITELISTED_COMMANDS["identify"],
        machine_id=machine_id,
        audit_kind="ssh-test",
    )
    if not raw.get("ok"):
        return raw

    parsed = _parse_identify(raw["stdout"], username)
    return {
        "ok": True,
        "latency_ms": raw["latency_ms"],
        "hostname": parsed["hostname"],
        "user": parsed["user"],
        "uname": parsed["uname"],
        "os_kind": parsed["os_kind"],
        "host_key_fingerprint": raw["host_key_fingerprint"],
    }


# ── Public API ────────────────────────────────────────────────────────
async def ssh_test_machine(machine: Any) -> dict[str, Any]:
    """Test the SSH connection to a Machine ORM object."""
    # The caller passes the Machine row it just loaded: use its pinned key
    # (a fresh read, so no stale cache can weaken the check).
    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

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
        pinned_public_key=pinned_public_key,
        machine_id=machine.id,
    )

    if result["ok"]:
        _audit_log(
            machine.id,
            "ssh-test",
            "ok",
            f"hostname={result.get('hostname')}, os={result.get('os_kind')}, "
            f"fp={result.get('host_key_fingerprint')}",
        )
    # Failures are already audited by _collect_identify.
    return result


async def ssh_detect_hw(machine: Any) -> dict[str, Any]:
    """Run the whitelisted ``detect_hw`` inventory through the pinned host key.

    Returns the parsed detection (``cpu``, ``gpus``, ``virt``, ``os_kind``) or
    an ``ok: False`` failure — a changed fingerprint is refused exactly like
    ``ssh_test_machine``, before any credential is sent.
    """
    from app.hw_parser import parse_detect_hw

    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    secret = ""
    if machine.secret_ref:
        try:
            secret = decrypt_secret(machine.secret_ref)
        except Exception:
            secret = ""

    raw = await _run_whitelisted(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=secret,
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=WHITELISTED_COMMANDS["detect_hw"],
        machine_id=machine.id,
        audit_kind="detect",
    )
    if not raw.get("ok"):
        return raw

    return {
        "ok": True,
        "latency_ms": raw["latency_ms"],
        "host_key_fingerprint": raw["host_key_fingerprint"],
        **parse_detect_hw(raw["stdout"]),
    }


async def ssh_collect_stats(machine: Any) -> dict[str, Any]:
    """Run the whitelisted ``collect_stats`` snapshot through the pinned host key.

    Returns ``{"ok": True, "stdout": ...}`` (raw sections, parsed by
    :func:`app.stats_parser.parse_collect_stats`) or an ``ok: False`` failure.
    Nothing is audited on success: a 60 s collector would flood the log.
    """
    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    secret = ""
    if machine.secret_ref:
        try:
            secret = decrypt_secret(machine.secret_ref)
        except Exception:
            secret = ""

    return await _run_whitelisted(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=secret,
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=WHITELISTED_COMMANDS["collect_stats"],
        machine_id=machine.id,
        audit_kind="collect",
    )


async def reset_machine_host_key(machine_id: int) -> dict[str, Any]:
    """Clear the pinned host key of a machine (admin action)."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        m = db.query(Machine).filter(Machine.id == machine_id).first()
        if not m:
            return {"ok": False, "error": "Machine not found"}
        previous = m.host_key
        m.host_key = None
        m.host_key_public = None
        db.commit()
    finally:
        db.close()

    _audit_log(
        machine_id,
        "host-key-reset",
        "ok",
        f"Host key cleared (was {previous or 'none'}) — next test pins the new key",
    )
    return {"ok": True, "message": "Host key cleared — next SSH test will pin a new key"}
