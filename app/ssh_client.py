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

Authentication modes (§4.1 bis)
-------------------------------
* ``password``   — password stored encrypted (Fernet, master key);
* ``fossor_key`` — Fossor's own ed25519 key, private half encrypted in the
  settings table, public half pinned on the machine's ``authorized_keys``;
* ``key``        — legacy: private key pasted by the operator, still usable
  for machines created before the mode was removed from the UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
        echo '### GPU_COMPUTE'
        if command -v nvidia-smi >/dev/null 2>&1; then
          nvidia-smi --query-compute-apps=pid,process_name,gpu_id,used_memory --format=csv,noheader 2>/dev/null
        fi
        echo '### PS_MINERS'
        ps -eo args 2>/dev/null | grep -Ei 'srbminer|bzminer|xmrig|t-rex|lolminer|gminer|nbminer|onezerominer|peakminer|phoenixminer|teamredminer' | grep -v grep
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
        ps -eo user:32,pid,pcpu,etimes,args 2>/dev/null | awk 'NR==1{print;next} substr($5,1,1)=="["{next} $3+0>0 || /stratum|srbminer|cpuminer|wam-miner|xmrig|generatetoaddress|mine[-_]|miner[.]sh|--api-port|--pool|--url|korshd|bitcoind|-cli /{print}'
        echo '### UPTIME'
        cat /proc/uptime 2>/dev/null
        exit 0
    """).strip(),
    # Read-only inventory of the *other* components (§4.2 bis): RAM, disks,
    # non-GPU PCI cards and HiveOS fans.  sudo -n never prompts; a missing tool
    # leaves its section empty and the command still exits 0.
    "inventory": textwrap.dedent("""\
        echo '### DMIDECODE'
        sudo -n dmidecode -t memory 2>/dev/null
        echo '### MEMINFO'
        cat /proc/meminfo 2>/dev/null
        echo '### LSBLK'
        lsblk -d -o NAME,ROTA,TRAN,SIZE,MODEL 2>/dev/null
        echo '### LSPCI'
        lspci 2>/dev/null
        echo '### HIVE_STAT'
        cat /run/hive/last_stat.json 2>/dev/null
        exit 0
    """).strip(),
    # SRBMiner / miner HTTP API bound to 127.0.0.1 on the machine: the
    # container cannot reach it, so the request is relayed through SSH
    # (fallback used by the collector; {port} is validated as an int).
    "miner_api": (
        "curl -s -m 5 http://127.0.0.1:{port}/ 2>/dev/null "
        "|| wget -qO- -T 5 http://127.0.0.1:{port}/ 2>/dev/null "
        "|| exit 0"
    ),
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
    actor: str = "system",
) -> None:
    """Write an audit log entry (actor = connected user or ``system``)."""
    from app.database import SessionLocal
    from app.models import AuditLog

    db = SessionLocal()
    try:
        entry = AuditLog(
            actor=actor or "system",
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
    """Build credential kwargs; returns (kwargs, temporary private-key path).

    ``fossor_key`` always uses Fossor's own identity: the machine carries no
    secret in that mode, and a stale ``secret`` is deliberately ignored.
    """
    if auth_type == "fossor_key":
        from app.fossor_keys import private_key_text

        key = asyncssh.import_private_key(private_key_text())
        return {"client_keys": [key]}, None
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


async def _resolve_host_key(
    *,
    host: str,
    port: int,
    pinned_fp: str | None,
    pinned_public_key: str | None,
    machine_id: int | None,
    audit_kind: str,
    start: float,
    actor: str = "system",
) -> tuple[Any | None, str, dict[str, Any] | None]:
    """Read, verify and (on first contact) pin the host key.

    Must complete **before** any credential is handed to asyncssh.
    """
    try:
        server_key = await read_server_host_key(host, port)
    except asyncio.TimeoutError:
        return None, "", _failure(start, f"SSH host key read timed out ({HOST_KEY_TIMEOUT} s)")
    except (asyncssh.Error, OSError) as exc:
        return None, "", _failure(start, f"Connection failed: {exc}")

    new_fp = _fingerprint_of(server_key)
    new_pub = _public_key_of(server_key)

    # Fresh read: never trust a possibly stale machine object (double pin).
    if machine_id is not None:
        db_fp, db_pub = _get_host_key_data(machine_id)
        if db_fp:
            pinned_fp, pinned_public_key = db_fp, db_pub

    trusted_key = None
    if pinned_fp:
        if not pinned_public_key:
            msg = (
                f"Empreinte épinglée ({pinned_fp}) sans clé publique enregistrée — "
                "connexion refusée, lancez reset-host-key"
            )
            if machine_id is not None:
                _audit_log(machine_id, "host-key-check", "fail", msg, actor=actor)
                _audit_log(machine_id, audit_kind, "fail", msg, actor=actor)
            return None, new_fp, _failure(start, msg, new_fp)
        if new_fp != pinned_fp:
            msg = f"Empreinte changée : ancienne {pinned_fp}, nouvelle {new_fp}"
            if machine_id is not None:
                _audit_log(machine_id, "host-key-mismatch", "fail", msg, actor=actor)
                _audit_log(machine_id, audit_kind, "fail", msg, actor=actor)
            return None, new_fp, _failure(start, msg, new_fp)
        try:
            trusted_key = asyncssh.import_public_key(pinned_public_key)
        except Exception:
            msg = "Clé publique épinglée illisible — connexion refusée, lancez reset-host-key"
            if machine_id is not None:
                _audit_log(machine_id, "host-key-check", "fail", msg, actor=actor)
                _audit_log(machine_id, audit_kind, "fail", msg, actor=actor)
            return None, new_fp, _failure(start, msg, new_fp)

    if trusted_key is None:
        if machine_id is not None:
            _pin_host_key(machine_id, new_fp, new_pub)
            _audit_log(machine_id, "host-key-pinned", "ok", f"Host key pinned: {new_fp}", actor=actor)
        trusted_key = server_key

    return trusted_key, new_fp, None


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
    actor: str = "system",
) -> dict[str, Any]:
    """Pin/verify the host key, connect, then run one whitelisted *command*."""
    start = asyncio.get_running_loop().time() * 1000

    trusted_key, new_fp, failure = await _resolve_host_key(
        host=host,
        port=port,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        machine_id=machine_id,
        audit_kind=audit_kind,
        start=start,
        actor=actor,
    )
    if failure is not None:
        return failure

    # Connect with the pinned key handed to asyncssh: verified during the
    # handshake, i.e. before authentication.
    connect_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "known_hosts": ([trusted_key], [], []),
        "connect_timeout": CONNECT_TIMEOUT,
    }
    try:
        auth_kwargs, tmp_key = _auth_kwargs(auth_type, secret)
    except Exception as exc:
        msg = f"Credentials unavailable: {exc}"
        if machine_id is not None:
            _audit_log(machine_id, audit_kind, "fail", msg, actor=actor)
        return _failure(start, msg, new_fp)
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
            _audit_log(machine_id, audit_kind, "fail", msg, actor=actor)
        return _failure(start, msg, new_fp)
    except asyncssh.PermissionDenied:
        return _failure(start, "Le login ou le mot de passe temporaires ont été refusés par la machine", new_fp)
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


async def open_pinned_connection(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str = "",
    pinned_fp: str | None = None,
    pinned_public_key: str | None = None,
    machine_id: int | None = None,
    audit_kind: str = "ssh",
    actor: str = "system",
) -> tuple[Any | None, str, dict[str, Any] | None]:
    """Open an authenticated connection after the same host-key policy.

    Returns ``(connection, fingerprint, failure)``; the caller owns the
    connection and must close it. Used by the provisioning flow, which needs
    several commands inside one session.
    """
    start = asyncio.get_running_loop().time() * 1000
    trusted_key, new_fp, failure = await _resolve_host_key(
        host=host,
        port=port,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        machine_id=machine_id,
        audit_kind=audit_kind,
        start=start,
        actor=actor,
    )
    if failure is not None:
        return None, new_fp, failure

    connect_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "known_hosts": ([trusted_key], [], []),
        "connect_timeout": CONNECT_TIMEOUT,
    }
    try:
        auth_kwargs, tmp_key = _auth_kwargs(auth_type, secret)
    except Exception as exc:
        msg = f"Credentials unavailable: {exc}"
        if machine_id is not None:
            _audit_log(machine_id, audit_kind, "fail", msg, actor=actor)
        return None, new_fp, _failure(start, msg, new_fp)
    connect_kwargs.update(auth_kwargs)

    try:
        conn = await asyncio.wait_for(asyncssh.connect(**connect_kwargs), timeout=CONNECT_TIMEOUT)
    except asyncssh.PermissionDenied:
        return None, new_fp, _failure(start, "Le login ou le mot de passe temporaires ont été refusés par la machine", new_fp)
    except asyncio.TimeoutError:
        return None, new_fp, _failure(start, f"SSH connection timed out ({CONNECT_TIMEOUT} s)", new_fp)
    except (asyncssh.Error, OSError, ConnectionError) as exc:
        return None, new_fp, _failure(start, f"Connection failed: {exc}", new_fp)
    except Exception as exc:  # pragma: no cover - defensive
        return None, new_fp, _failure(start, f"SSH error: {exc}", new_fp)
    finally:
        if tmp_key:
            try:
                os.unlink(tmp_key)
            except OSError:
                pass

    return conn, new_fp, None


async def _collect_identify(
    host: str,
    port: int,
    username: str,
    secret: str,
    auth_type: str,
    pinned_fp: str | None,
    pinned_public_key: str | None,
    machine_id: int | None = None,
    actor: str = "system",
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
        actor=actor,
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
def _machine_secret(machine: Any) -> str:
    """Decrypted password / legacy private key of a machine (empty if none)."""
    if getattr(machine, "secret_ref", None):
        try:
            return decrypt_secret(machine.secret_ref)
        except Exception:
            return ""
    return ""


def ps_miners_command() -> str:
    """The ``PS_MINERS`` filter of ``collect_stats``, as executed on machines.

    Extracted from the whitelisted command so a test exercises the *same*
    string, never a copy that drifts.  2026.11.004-c4 : le filtre garde aussi
    les scripts solo (``*-miner.sh``) même à 0 % de CPU — ``korsh-miner.sh``
    dort entre deux scans et disparaissait, donc aucune sonde de log n'était
    planifiée pour le solo KORSH.
    """
    for line in WHITELISTED_COMMANDS["collect_stats"].splitlines():
        if line.strip().startswith("ps -eo user:32"):
            return line.strip()
    raise LookupError("commande PS_MINERS introuvable")


async def ssh_test_machine(machine: Any, actor: str = "system") -> dict[str, Any]:
    """Test the SSH connection to a Machine ORM object."""
    # The caller passes the Machine row it just loaded: use its pinned key
    # (a fresh read, so no stale cache can weaken the check).
    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    result = await _collect_identify(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=_machine_secret(machine),
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        machine_id=machine.id,
        actor=actor,
    )

    if result["ok"]:
        _audit_log(
            machine.id,
            "ssh-test",
            "ok",
            f"hostname={result.get('hostname')}, os={result.get('os_kind')}, "
            f"fp={result.get('host_key_fingerprint')}",
            actor=actor,
        )
    # Failures are already audited by _collect_identify.
    return result


async def ssh_detect_hw(machine: Any, actor: str = "system") -> dict[str, Any]:
    """Run the whitelisted ``detect_hw`` inventory through the pinned host key.

    Returns the parsed detection (``cpu``, ``gpus``, ``virt``, ``os_kind``) or
    an ``ok: False`` failure — a changed fingerprint is refused exactly like
    ``ssh_test_machine``, before any credential is sent.
    """
    from app.hw_parser import parse_detect_hw

    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    raw = await _run_whitelisted(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=_machine_secret(machine),
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=WHITELISTED_COMMANDS["detect_hw"],
        machine_id=machine.id,
        audit_kind="detect",
        actor=actor,
    )
    if not raw.get("ok"):
        return raw

    return {
        "ok": True,
        "latency_ms": raw["latency_ms"],
        "host_key_fingerprint": raw["host_key_fingerprint"],
        **parse_detect_hw(raw["stdout"]),
    }


async def ssh_collect_stats(machine: Any, actor: str = "system") -> dict[str, Any]:
    """Run the whitelisted ``collect_stats`` snapshot through the pinned host key.

    Returns ``{"ok": True, "stdout": ...}`` (raw sections, parsed by
    :func:`app.stats_parser.parse_collect_stats`) or an ``ok: False`` failure.
    Nothing is audited on success: a 60 s collector would flood the log.
    """
    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    return await _run_whitelisted(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=_machine_secret(machine),
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=WHITELISTED_COMMANDS["collect_stats"],
        machine_id=machine.id,
        audit_kind="collect",
        actor=actor,
    )


CPU_PROBE_PATH_RE = re.compile(r"^/[A-Za-z0-9_.\-/]{1,300}$")
#: ``~`` accepté pour la redirection du lanceur d'un script solo (c1).
CPU_PROBE_LOG_RE = re.compile(r"^~?/[A-Za-z0-9_.\-/]{1,300}$")
CPU_PROBE_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: Relative form accepted for a ``*-cli`` / ``-datadir`` resolved from cwd.
CPU_PROBE_REL_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,300}$")
CPU_PROBE_BIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


def cpu_probe_command(probes: dict[str, Any]) -> str:
    """Whitelisted, read-only ``collect_cpu`` command built from *probes*.

    Every interpolated value is validated here: an absolute path with no shell
    metacharacter, a digit port, a safe basename.  Anything else is dropped, so
    a hostile command line can never reach the shell of the machine.
    """
    from app.cpu_stats import DEFAULT_API_PORTS  # noqa: F401  (documented coupling)

    lines = [
        # Read-only helper: the plain run first, sudo only when needed (fossor
        # account with a NOPASSWD sudoers entry), never an interactive prompt.
        "RD() { \"$@\" 2>/dev/null || sudo -n \"$@\" 2>/dev/null; }",
        "echo '### PROC_STAT'",
        "head -n 8 /proc/stat 2>/dev/null",
        "echo '### POWER'",
        # RAPL est souvent réservé à root : lecture directe puis `sudo -n`.
        "for f in /sys/class/powercap/*/energy_uj; do [ -e \"$f\" ] && "
        "printf '%s|%s|%s\\n' \"$f\" \"$(RD cat \"$f\" 2>/dev/null)\" "
        "\"$(RD cat \"${f%energy_uj}max_energy_range_uj\" 2>/dev/null)\"; done 2>/dev/null",
        "echo '### CPUINFO'",
        "grep -iE '^(processor|cpu mhz|model name)' /proc/cpuinfo 2>/dev/null",
        "echo '### FREQ'",
        "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do [ -e \"$f\" ] && "
        "RD cat \"$f\" 2>/dev/null; done 2>/dev/null",
        "echo '### HWMON'",
        "for d in /sys/class/hwmon/hwmon*/; do "
        "n=$(cat \"${d}name\" 2>/dev/null); "
        "for f in \"${d}\"temp*_input; do [ -r \"$f\" ] || continue; "
        "l=$(cat \"${f%_input}_label\" 2>/dev/null); "
        "printf '%s|%s|%s\\n' \"$n\" \"$l\" \"$(cat \"$f\" 2>/dev/null)\"; done; "
        "done 2>/dev/null",
        "echo '### POWERH'",
        # AMD amd_energy / zenpower exposent power1_input (microwatts) là où
        # RAPL manque : même lecture, avec la source dans la ligne.
        "for d in /sys/class/hwmon/hwmon*/; do "
        "n=$(cat \"${d}name\" 2>/dev/null); "
        "case \"$n\" in amd_energy|zenpower) "
        "for f in \"${d}\"power*_input; do [ -e \"$f\" ] || continue; "
        "printf '%s|%s|%s\\n' \"$n\" \"${f##*/}\" \"$(RD cat \"$f\" 2>/dev/null)\"; "
        "done;; esac; done 2>/dev/null",
        "sleep 1",
        "echo '### PROC_STAT2'",
        "head -n 8 /proc/stat 2>/dev/null",
        "echo '### POWER2'",
        "for f in /sys/class/powercap/*/energy_uj; do [ -r \"$f\" ] && "
        "printf '%s|%s|%s\\n' \"$f\" \"$(cat \"$f\" 2>/dev/null)\" "
        "\"$(cat \"${f%energy_uj}max_energy_range_uj\" 2>/dev/null)\"; done 2>/dev/null",
    ]

    for probe in probes.get("logs") or []:
        path = str(probe.get("path") or "")
        if not CPU_PROBE_PATH_RE.match(path):
            continue
        lines += [
            f"echo '### LOG {path}'",
            f"stat -c %Y {path} 2>/dev/null",
            f"RD tail -n 80 {path} 2>/dev/null",
        ]

    # Log deduced from the process's own stdout/stderr redirection, including
    # the one a systemd unit sets (``StandardOutput=append:…``): the symlink
    # /proc/<pid>/fd/1 is the redirection, without any path hard-coded.
    pids = [
        int(probe["pid"]) for probe in (probes.get("fds") or [])
        if isinstance(probe.get("pid"), int) and probe["pid"] > 0
    ]
    if pids:
        targets = " ".join(str(pid) for pid in pids)
        lines += [
            # 2026.11.003-c8 : `readlink` sur /proc/<pid>/fd exige les droits du
            # propriétaire (mineur lancé par root) → RD (direct puis sudo -n).
            # Un mineur dont la sortie est un socket/pty (screen, unité systemd)
            # n'a pas de fichier de log : journald le porte, lu par _PID.
            f"for p in {targets}; do "
            "f=$(RD readlink -f /proc/$p/fd/1 2>/dev/null); "
            "g=$(RD readlink -f /proc/$p/fd/2 2>/dev/null); "
            "k=0; for c in \"$f\" \"$g\"; do "
            "case \"$c\" in "
            "/var/log/*|/var/lib/*|/root/*|/home/*|/opt/*|/srv/*|/tmp/*) "
            # 2026.11.004-c3 : un « \" » échappé dans la chaîne Python donnait
            # un mot entouré de guillemets littéraux → le `case` ne matchait
            # jamais et la sonde retombait toujours sur journald.
            "{ [ -e \"$c\" ] || RD test -e \"$c\"; } && k=1 && "
            "echo \"### LOGI $p $c\" && "
            "RD stat -c %Y \"$c\" 2>/dev/null && "
            "RD tail -n 80 \"$c\" 2>/dev/null;; esac; done; "
            "[ $k = 0 ] && { echo \"### JLOG $p\"; "
            "RD journalctl _PID=$p -n 80 --no-pager 2>/dev/null; }; "
            "done 2>/dev/null",
        ]

    for probe in probes.get("apis") or []:
        try:
            port = int(probe.get("port"))
        except (TypeError, ValueError):
            continue
        if not 1 <= port <= 65535:
            continue
        lines += [
            f"echo '### API {port}'",
            f"curl -s -m 4 http://127.0.0.1:{port}/summary 2>/dev/null || "
            f"curl -s -m 4 http://127.0.0.1:{port}/ 2>/dev/null",
        ]

    for probe in probes.get("nodes") or []:
        key = str(probe.get("key") or "")
        if not CPU_PROBE_BIN_RE.match(key):
            continue
        cli = str(probe.get("cli") or "")
        token = str(probe.get("token") or "")
        datadir = str(probe.get("datadir") or "")
        pid = probe.get("pid")
        cli = cli if CPU_PROBE_PATH_RE.match(cli) else ""
        token = token if CPU_PROBE_REL_RE.match(token) else ""
        datadir = datadir if CPU_PROBE_REL_RE.match(datadir) else ""
        directory = f"/proc/{int(pid)}/cwd" if isinstance(pid, int) and pid > 0 else ""
        # 2026.11.003-c8 : le client est souvent lancé en relatif
        # (« ./bigcoin-cli ») depuis le dossier du script.  Le dossier de
        # travail vient de /proc/<pid>/cwd (RD : le démon peut être root), le
        # datadir relatif s'y résout aussi.  Getters seulement.
        lines += [
            f"echo '### NODE {key}'",
            "P=" + (cli or ""),
            "D=" + datadir,
        ]
        if directory:
            lines.append(f"C=$(RD readlink -f {directory} 2>/dev/null)")
            if token:
                lines.append(f"[ -n \"$P\" ] || P=\"$C/{token}\"")
            lines.append(f"[ -n \"$P\" ] || P=\"$C/{key}-cli\"")
            lines.append("[ \"${D#/}\" != \"$D\" ] || D=\"$C/$D\"")
        lines.append(f"[ -n \"$P\" ] || P=$(command -v {key}-cli 2>/dev/null)")
        datadir_arg = ' -datadir="$D"' if datadir else ""
        lines += [
            f"[ -n \"$P\" ] && RD \"$P\"{datadir_arg} getmininginfo",
            f"echo '### CHAIN {key}'",
            f"[ -n \"$P\" ] && RD \"$P\"{datadir_arg} getblockchaininfo",
            f"echo '### NETPS {key}'",
            f"[ -n \"$P\" ] && RD \"$P\"{datadir_arg} getnetworkhashps",
        ]
        # (b) redirection du lanceur du script solo : « ~/… » est développé
        # avec le home du compte du processus (getent passwd).  Cette voie ne
        # dépend pas de /proc/<pid>/fd, réservé au propriétaire et à root.
        redirect = str(probe.get("redirect") or "")
        if CPU_PROBE_LOG_RE.match(redirect):
            user = str(probe.get("redirect_user") or "")
            if CPU_PROBE_USER_RE.match(user):
                lines.append(
                    f"RH=$(RD getent passwd {user} 2>/dev/null | cut -d: -f6)"
                )
            else:
                lines.append('RH="$HOME"')
            lines += [
                f"RL='{redirect}'",
                'case "$RL" in "~/"*) RL="$RH/${RL#\\~/}";; esac',
                f'echo "### LOGS {key} $RL"',
                'RD stat -c %Y "$RL" 2>/dev/null',
                'RD tail -n 80 "$RL" 2>/dev/null',
            ]
        # 2026.11.004-c4 : le script solo déclare son propre journal.  Son
        # ``/proc/<pid>/fd/255`` est le fichier de script ouvert par bash : on y
        # lit les chemins ``*.log`` qu'il redirige, on résout les relatifs sur
        # son cwd et on garde le plus récent.  Aucun nom de log n'est deviné.
        solo = probe.get("solo_pids") or {}
        script_pids = [
            pid for pid in (solo.get("script") or []) + (solo.get("parent") or [])
            if isinstance(pid, int) and pid > 0
        ]
        if script_pids and not probe.get("redirect"):
            lines.append("SF=''")
            lines.append("for sp in " + " ".join(str(pid) for pid in script_pids) + "; do")
            lines.append("  SD=$(RD readlink -f /proc/$sp/cwd 2>/dev/null)")
            lines.append(
                "  for f in $(RD grep -hoE '[A-Za-z0-9_./-]+[.]log'"
                " /proc/$sp/fd/255 2>/dev/null | sort -u); do"
            )
            lines.append('    case "$f" in /*) ;; *) f="$SD/$f";; esac')
            lines.append('    [ -n "$(RD stat -c %Y "$f" 2>/dev/null)" ] && SF="$f"')
            lines.append("  done")
            lines.append("done")
            lines.append(
                f'[ -n "$SF" ] && {{ echo "### LOGI {script_pids[0]} $SF"; '
                'RD stat -c %Y "$SF" 2>/dev/null; RD tail -n 80 "$SF" 2>/dev/null; }'
            )

        log_path = str(probe.get("log") or "")
        if CPU_PROBE_PATH_RE.match(log_path):
            lines += [
                f"echo '### LOGS {key} {log_path}'",
                f"stat -c %Y {log_path} 2>/dev/null",
                f"RD tail -n 80 {log_path} 2>/dev/null",
            ]

    lines.append("exit 0")
    return "\n".join(lines) + "\n"


async def ssh_collect_cpu(machine: Any, probes: dict[str, Any], actor: str = "system") -> dict[str, Any]:
    """Run the read-only ``collect_cpu`` probe (hashrate CPU + état du CPU).

    Nothing is audited on success, like ``collect_stats``: this runs every
    collection round.  Only ``tail``/``cat``/``stat``/``curl`` local and the
    node getters are ever executed — no miner or node command that changes state.
    """
    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    return await _run_whitelisted(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=_machine_secret(machine),
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=cpu_probe_command(probes),
        machine_id=machine.id,
        audit_kind="collect",
        actor=actor,
    )


async def ssh_collect_inventory(machine: Any, actor: str = "system") -> dict[str, Any]:
    """Run the read-only ``inventory`` command (§4.2 bis).

    dmidecode via ``sudo -n``, /proc/meminfo, lsblk, lspci and the HiveOS fan
    list: nothing is written on the machine and a missing tool is not an error.
    """
    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    return await _run_whitelisted(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=_machine_secret(machine),
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=WHITELISTED_COMMANDS["inventory"],
        machine_id=machine.id,
        audit_kind="collect",
        actor=actor,
    )


async def ssh_miner_api(machine: Any, port: int, actor: str = "system") -> dict[str, Any]:
    """Read a miner HTTP API **from the machine** (repli SSH, §4.6 003-c2).

    SRBMiner usually listens on ``127.0.0.1`` only, so the container's direct
    ``GET http://host:port/`` fails in silence.  This relays
    ``curl http://127.0.0.1:<port>/`` (``wget`` fallback) through the pinned
    SSH connection.  The port is validated here, never interpolated raw.
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "error": "port d'API invalide"}
    if not 1 <= port <= 65535:
        return {"ok": False, "error": "port d'API invalide"}

    pinned_fp = (getattr(machine, "host_key", None) or "").strip() or None
    pinned_public_key = (getattr(machine, "host_key_public", None) or "").strip() or None

    result = await _run_whitelisted(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        secret=_machine_secret(machine),
        auth_type=machine.auth_type,
        pinned_fp=pinned_fp,
        pinned_public_key=pinned_public_key,
        command=WHITELISTED_COMMANDS["miner_api"].format(port=port),
        machine_id=machine.id,
        audit_kind="miner_api",
        actor=actor,
    )
    if result.get("ok") and not str(result.get("stdout") or "").strip():
        return {"ok": False, "error": "API du mineur injoignable"}
    return result


async def reset_machine_host_key(machine_id: int, actor: str = "system") -> dict[str, Any]:
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
        actor=actor,
    )
    return {"ok": True, "message": "Host key cleared — next SSH test will pin a new key"}
