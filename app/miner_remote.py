"""Execution of miner actions on a machine — host mode (§4.6), simulation by default.

The module builds an explicit **plan** of steps (each one an argv list), then
either returns it untouched (simulation) or runs it over SSH. Nothing here
builds a shell string from operator input: values are validated by
:mod:`app.miners`, kept as separate argv elements and quoted with
``shlex.join`` when the SSH channel needs one line.

``RUNNER`` is the only door to the outside world. The simulated path returns
**before** touching it, which the unit tests assert by installing a runner
that fails when called.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app import miners as M

DEFAULT_TIMEOUT = 120
INSTALL_TIMEOUT = 420


class MinerError(RuntimeError):
    """A miner action failed or was refused — message safe for the operator."""


@dataclass(frozen=True)
class Step:
    """One remote command, as an argument list."""

    title: str
    argv: tuple[str, ...]
    stdin: str | None = None
    allow_fail: bool = False
    timeout: int = DEFAULT_TIMEOUT

    def command(self) -> str:
        return shlex.join(self.argv)


Runner = Callable[..., Awaitable[dict[str, Any]]]
RUNNER: Runner | None = None


async def default_runner(
    machine: Any, command: str, stdin: str | None = None, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Run one command over the pinned, fossor-key connection."""
    from app.ssh_client import open_pinned_connection
    from app.secrets import decrypt_secret

    secret = ""
    if getattr(machine, "secret_ref", None):
        try:
            secret = decrypt_secret(machine.secret_ref)
        except Exception:
            secret = ""
    conn, fingerprint, failure = await open_pinned_connection(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        auth_type=machine.auth_type,
        secret=secret,
        pinned_fp=(machine.host_key or "").strip() or None,
        pinned_public_key=(machine.host_key_public or "").strip() or None,
        machine_id=machine.id,
        audit_kind="miner",
    )
    if failure is not None:
        return {"status": 255, "stdout": "", "stderr": failure.get("error", ""), "ok": False}
    assert conn is not None  # no failure ⇒ a live connection
    try:
        result = await conn.run(command, input=stdin, timeout=timeout, check=False)
        return {
            "status": int(result.exit_status or 0),
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "ok": True,
        }
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:  # pragma: no cover - defensive
            pass


async def _run(machine: Any, step: Step) -> dict[str, Any]:
    runner = RUNNER or default_runner
    result = await runner(machine, step.command(), stdin=step.stdin, timeout=step.timeout)
    if not result.get("ok", True):
        raise MinerError(
            f"{step.title} : connexion impossible — {result.get('stderr') or 'erreur SSH'}"
        )
    if result.get("status", 0) != 0 and not step.allow_fail:
        detail = (result.get("stderr") or result.get("stdout") or "").strip()[:300]
        raise MinerError(f"{step.title} : code {result.get('status')} — {detail}")
    return result


# ── Plan building ─────────────────────────────────────────────────────
def _sudo(user: str, *argv: str) -> tuple[str, ...]:
    return argv if user == "root" else ("sudo", "-n", *argv)


def _sh(script: str, *args: str) -> tuple[str, ...]:
    return ("sh", "-c", script, "fossor", *args)


PROBE_SCRIPT = (
    "for t in curl wget tar sha256sum systemctl setsid nvidia-smi; do "
    'if command -v "$t" >/dev/null 2>&1; then echo "TOOL:$t"; '
    'else echo "MISSING:$t"; fi; done; '
    'echo "INIT:$(ps -p 1 -o comm= 2>/dev/null | tr -d " ")"; '
    "echo \"USER:$(id -un)\""
)

# Foreign miners: everything we know how to recognise, minus our own tree.
FOREIGN_PS = (
    "ps -eo pid=,args= 2>/dev/null | grep -Ei "
    '"srbminer|lolminer|bzminer|xmrig|t-rex|gminer|nbminer|phoenixminer|teamredminer" '
    "| grep -v grep || true"
)
GPU_APPS = (
    "if command -v nvidia-smi >/dev/null 2>&1; then "
    "nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null; "
    "fi"
)


def precheck_plan(user: str) -> list[Step]:
    steps = [
        Step("outils disponibles", _sh(PROBE_SCRIPT)),
        Step("occupation des GPU", _sh(GPU_APPS), allow_fail=True),
        Step("processus mineurs présents", _sh(FOREIGN_PS), allow_fail=True),
    ]
    if user != "root":
        steps.insert(0, Step("droits sudo", _sudo(user, "true")))
    return steps


def install_tmp(profile: dict[str, Any]) -> str:
    """Chemin temporaire de l'archive (ou du binaire ELF) sur la machine cible."""
    kind = str(profile.get("binary_kind") or "tar.gz").strip().lower()
    suffix = {"elf": "bin", "zip": "zip", "tar.xz": "tar.xz"}.get(kind, "tar.gz")
    return f"/tmp/fossor-{profile['sha256'][:12]}.{suffix}"


def install_dirs(profile: dict[str, Any], user: str) -> list[Step]:
    """Dossiers d'installation, d'exécution et de journaux."""
    directory = M.install_dir(profile["software"], profile["version"])
    return [
        Step("dossier du binaire", _sudo(user, "install", "-d", "-m", "0755", directory)),
        Step(
            "dossiers d'exécution",
            _sudo(user, "install", "-d", "-m", "0775", "-o", user, "-g", user, M.RUN_DIR),
        ),
        Step(
            "dossier des journaux",
            _sudo(user, "install", "-d", "-m", "0775", "-o", user, "-g", user, M.LOG_DIR),
        ),
    ]


def install_rest(profile: dict[str, Any], user: str) -> list[Step]:
    """Contrôle de l'empreinte, extraction (ou copie ELF), contrôles, nettoyage."""
    directory = M.install_dir(profile["software"], profile["version"])
    tmp = install_tmp(profile)
    kind = str(profile.get("binary_kind") or "tar.gz").strip().lower()
    steps = [Step("empreinte de l'archive", _sudo(user, "sha256sum", tmp))]
    if kind == "elf":
        # Binaire ELF brut : installé tel quel, aucune extraction.
        steps.append(
            Step("binaire ELF installé tel quel", _sudo(user, "install", "-m", "0755", tmp, M.binary_path(profile)))
        )
    elif kind == "zip":
        steps.append(
            Step(
                "extraction (zip)",
                _sudo(user, "python3", "-m", "zipfile", "-e", tmp, directory),
                timeout=INSTALL_TIMEOUT,
            )
        )
    else:
        steps.append(
            Step("extraction", _sudo(user, "tar", "-xf", tmp, "-C", directory), timeout=INSTALL_TIMEOUT)
        )
    steps += [
        Step("binaire présent", _sudo(user, "test", "-f", M.binary_path(profile))),
        Step("binaire exécutable", _sudo(user, "chmod", "0755", M.binary_path(profile))),
        Step("nettoyage de l'archive", _sudo(user, "rm", "-f", tmp), allow_fail=True),
    ]
    return steps


def install_plan(profile: dict[str, Any], user: str) -> list[Step]:
    """Dossiers, (téléchargement si URL), extraction et contrôles.

    ``binary_source=fossor`` : **aucun téléchargement** — l'archive est poussée
    par Fossor lui-même (:func:`push_binary`) avant l'extraction. Une machine
    sans route vers le réseau de Fossor n'a donc plus besoin de ``binary_url``.
    """
    steps = install_dirs(profile, user)
    if str(profile.get("binary_source") or "url").strip().lower() != "fossor":
        download = (
            'if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 300 -o "$1" "$2"; '
            'else wget -q -O "$1" "$2"; fi'
        )
        steps.append(
            Step(
                "téléchargement par la machine cible",
                _sudo(user, *_sh(download, install_tmp(profile), profile["binary_url"])),
                timeout=INSTALL_TIMEOUT,
            )
        )
    return steps + install_rest(profile, user)


async def _sftp_put(machine: Any, local_path: Any, remote_path: str) -> None:
    from app.ssh_client import ssh_put_file

    await ssh_put_file(machine, local_path, remote_path)


async def push_binary(
    machine: Any,
    profile: dict[str, Any],
    *,
    user: str,
    put: Any = None,
    run: Any = None,
) -> dict[str, Any]:
    """Pousse l'archive Fossor par SFTP, puis vérifie son SHA256 à distance.

    L'empreinte locale est revérifiée avant l'envoi, celle de la machine après :
    une archive qui ne correspond pas est supprimée et l'extraction refusée.
    """
    from app import binaries as B

    path, digest = B.verified_path(profile)
    remote = install_tmp(profile)
    putter = put if put is not None else _sftp_put
    await putter(machine, path, remote)
    runner = run if run is not None else _run
    result = await runner(machine, Step("empreinte de l'archive", _sudo(user, "sha256sum", remote)))
    actual = (result.get("stdout", "").split() or [""])[0].strip().lower()
    if actual != profile["sha256"]:
        await runner(
            machine,
            Step("nettoyage après refus", _sudo(user, "rm", "-f", remote), allow_fail=True),
        )
        raise MinerError(
            "SHA256 différent après envoi — extraction refusée, archive supprimée "
            f"(attendu {profile['sha256']}, obtenu {actual or 'illisible'})"
        )
    return {"pushed": True, "remote": remote, "sha256": digest, "bytes": path.stat().st_size}


def cleanup_plan(profile: dict[str, Any], user: str) -> list[Step]:
    tmp = install_tmp(profile)
    return [Step("nettoyage après refus", _sudo(user, "rm", "-f", tmp), allow_fail=True)]


def unit_step(profile: dict[str, Any], unit: str, user: str) -> Step:
    return Step(
        "écriture de l'unité systemd",
        _sudo(user, "tee", f"{M.UNIT_DIR}/{unit}"),
        stdin=profile["_unit"],
    )


def start_plan_systemd(profile: dict[str, Any], unit: str, user: str) -> list[Step]:
    return [
        unit_step(profile, unit, user),
        Step("rechargement systemd", _sudo(user, "systemctl", "daemon-reload")),
        Step("activation au démarrage", _sudo(user, "systemctl", "enable", unit)),
        Step("démarrage du service", _sudo(user, "systemctl", "start", unit), timeout=60),
    ]


def start_plan_nohup(profile: dict[str, Any], argv: list[str], user: str) -> list[Step]:
    script = (
        'log="$1"; pid="$2"; shift 2; '
        'setsid nohup "$@" >> "$log" 2>&1 < /dev/null & '
        'echo $! > "$pid"'
    )
    return [
        Step(
            "lancement setsid nohup (sans systemd)",
            _sh(script, M.log_file(profile), M.pid_file(profile), *argv),
            timeout=60,
        )
    ]


def stop_plan_systemd(unit: str, user: str) -> list[Step]:
    return [
        Step("arrêt du service", _sudo(user, "systemctl", "stop", unit), timeout=60),
        Step("état après arrêt", _sudo(user, "systemctl", "is-active", unit), allow_fail=True),
    ]


def stop_plan_nohup(profile: dict[str, Any], user: str) -> list[Step]:
    """Fallback stop, kept as one readable step for the simulated command list."""
    script = (
        'pid=$(cat "$1" 2>/dev/null) || exit 0; '
        'test -n "$pid" || exit 0; '
        "grep -q /opt/fossor/miners /proc/$pid/cmdline 2>/dev/null || exit 2; "
        'kill -TERM "$pid"; '
        'for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done; '
        'rm -f "$1"'
    )
    return [
        Step(
            "arrêt par PID vérifié (sans systemd)",
            _sh(script, M.pid_file(profile)),
            timeout=30,
        )
    ]


def status_plan(profile: dict[str, Any], unit: str, user: str) -> list[Step]:
    steps = [
        Step("service actif", _sudo(user, "systemctl", "is-active", unit), allow_fail=True),
        Step("PID principal", _sudo(user, "systemctl", "show", "-p", "MainPID", "--value", unit), allow_fail=True),
        Step("PID du repli", ("cat", M.pid_file(profile)), allow_fail=True),
    ]
    if profile.get("api_port"):
        steps.append(
            Step(
                "hashrate (API du mineur)",
                ("curl", "-s", "--max-time", "3", f"http://127.0.0.1:{profile['api_port']}/"),
                allow_fail=True,
            )
        )
    return steps


# ── Parsing helpers (pure) ────────────────────────────────────────────
def parse_probe(stdout: str) -> dict[str, Any]:
    tools, missing, init = set(), set(), ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("TOOL:"):
            tools.add(line[5:])
        elif line.startswith("MISSING:"):
            missing.add(line[8:])
        elif line.startswith("INIT:"):
            init = line[5:]
    return {
        "tools": tools,
        "missing": missing,
        "systemd": "systemctl" in tools and init == "systemd",
        "init": init,
    }


def parse_occupancy(gpu_stdout: str, ps_stdout: str) -> list[dict[str, str]]:
    """Foreign processes holding a GPU or running a known miner binary."""
    found: list[dict[str, str]] = []
    for line in (gpu_stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            found.append({"pid": parts[0], "what": parts[1], "source": "nvidia-smi"})
    for line in (ps_stdout or "").splitlines():
        line = line.strip()
        if not line or M.INSTALL_ROOT in line:
            continue
        pid, _, args = line.partition(" ")
        if pid.isdigit():
            found.append({"pid": pid, "what": args.strip()[:120] or "mineur", "source": "ps"})
    # De-duplicate by pid, nvidia-smi first.
    seen: dict[str, dict[str, str]] = {}
    for item in found:
        seen.setdefault(item["pid"], item)
    return list(seen.values())


def parse_hashrate(text: str) -> float | None:
    """Tolerant hashrate extraction (H/s) from a miner API response."""
    if not (text or "").strip():
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        match = re.search(r'"hashrate"\s*:\s*([0-9.]+)', text or "")
        return float(match.group(1)) if match else None

    def _walk(node: Any, depth: int = 0) -> float | None:
        if depth > 4:
            return None
        if isinstance(node, dict):
            for key, value in node.items():
                low = str(key).lower()
                if low in ("hashrate", "hashrate_total", "hash_rate", "total_hashrate", "hashrate_hs"):
                    number = _number(value)
                    if number is not None:
                        return number
            for value in node.values():
                found = _walk(value, depth + 1)
                if found is not None:
                    return found
        if isinstance(node, list):
            for value in node:
                found = _walk(value, depth + 1)
                if found is not None:
                    return found
        return None

    return _walk(payload)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", value)
        return float(match.group(1)) if match else None
    if isinstance(value, dict):
        for key in ("total", "value", "hs"):
            if key in value:
                return _number(value[key])
    if isinstance(value, list) and value:
        return _number(value[0])
    return None


# ── Public actions ────────────────────────────────────────────────────
def service_user(machine: Any) -> str:
    """Dedicated non-root account when possible (§4.6)."""
    user = (getattr(machine, "ssh_user", "") or "").strip()
    if user and user != "root" and re.match(r"^[a-z_][a-z0-9_-]{0,31}$", user):
        return user
    return "root"


def _simulated(commands: list[Step], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "simulated": True,
        "commands": [step.command() for step in commands],
        "note": "Simulation : aucune commande n'est exécutée.",
    }
    if extra:
        payload.update(extra)
    return payload


async def start(
    machine: Any,
    profile: dict[str, Any],
    argv: list[str],
    *,
    simulated: bool,
    user: str,
) -> dict[str, Any]:
    """Start a miner profile on one machine (host mode)."""
    unit = M.unit_name(profile)
    unit_profile = dict(profile)
    unit_profile["_unit"] = M.render_unit(profile, argv, user=user)
    commands = precheck_plan(user) + install_plan(profile, user) + start_plan_systemd(
        unit_profile, unit, user
    )
    fallback = start_plan_nohup(profile, argv, user)

    if simulated:
        return _simulated(commands, {"fallback_commands": [s.command() for s in fallback]})
    if os.environ.get("FOSSOR_E2E_FAKE_DRIVER") == "1":
        return {"simulated": False, "ok": True, "mode": "fake", "fake_driver": True,
                "commands": [step.command() for step in commands]}

    executed: list[str] = []
    probe: dict[str, Any] = {}
    outputs: dict[str, str] = {}
    for step in precheck_plan(user):
        result = await _run(machine, step)
        executed.append(step.command())
        outputs[step.title] = result.get("stdout", "")
        if step.title == "outils disponibles":
            probe = parse_probe(result.get("stdout", ""))

    occupied = parse_occupancy(
        outputs.get("occupation des GPU", ""), outputs.get("processus mineurs présents", "")
    )
    if occupied:
        first = occupied[0]
        raise MinerError(
            "Un autre programme occupe les GPU (PID {} : « {} »). "
            "Fossor ne touche pas à un mineur qu'il n'a pas lancé — "
            "cohabitation HiveOS prévue à l'étape 3e.".format(first["pid"], first["what"])
        )

    missing = sorted(probe.get("missing", set()) & {"tar", "sha256sum"})
    if missing:
        raise MinerError(f"outils requis absents sur la machine : {', '.join(missing)}")

    # Install: download (ou push Fossor), verify *before* extraction, clean up.
    if str(profile.get("binary_source") or "url").strip().lower() == "fossor":
        for step in install_dirs(profile, user):
            await _run(machine, step)
            executed.append(step.command())
        await push_binary(machine, profile, user=user)
        install_steps = install_rest(profile, user)
    else:
        install_steps = install_plan(profile, user)
    for step in install_steps:
        result = await _run(machine, step)
        executed.append(step.command())
        if step.title == "empreinte de l'archive":
            actual = (result.get("stdout", "").split() or [""])[0].strip().lower()
            if actual != profile["sha256"]:
                for cleanup in cleanup_plan(profile, user):
                    await _run(machine, cleanup)
                    executed.append(cleanup.command())
                raise MinerError(
                    "SHA256 différent — extraction refusée, archive supprimée "
                    f"(attendu {profile['sha256']}, obtenu {actual or 'illisible'})"
                )

    if probe.get("systemd"):
        for step in start_plan_systemd(unit_profile, unit, user):
            await _run(machine, step)
            executed.append(step.command())
        mode = "systemd"
    else:
        for step in fallback:
            await _run(machine, step)
            executed.append(step.command())
        mode = "nohup"

    status = await _status(machine, profile, unit, user, executed)
    return {
        "simulated": False,
        "ok": True,
        "mode": mode,
        "commands": executed,
        "status": status,
    }


async def stop(
    machine: Any, profile: dict[str, Any], *, simulated: bool, user: str
) -> dict[str, Any]:
    unit = M.unit_name(profile)
    systemd_steps = stop_plan_systemd(unit, user)
    if simulated:
        return _simulated(systemd_steps, {"fallback_commands": [s.command() for s in stop_plan_nohup(profile, user)]})
    if os.environ.get("FOSSOR_E2E_FAKE_DRIVER") == "1":
        return {"simulated": False, "ok": True, "mode": "fake", "fake_driver": True,
                "commands": [step.command() for step in systemd_steps]}

    executed: list[str] = []
    probe_step = Step("outils disponibles", _sh(PROBE_SCRIPT))
    probe = await _run(machine, probe_step)
    executed.append(probe_step.command())
    parsed = parse_probe(probe.get("stdout", ""))

    if parsed.get("systemd"):
        for index, step in enumerate(systemd_steps):
            if index == 0:
                # L'unité peut être absente sur cette cible (code 5, « not
                # loaded ») : c'est « rien à arrêter », pas un échec (constat n°2).
                step = Step(step.title, step.argv, allow_fail=True, timeout=step.timeout)
            result = await _run(machine, step)
            executed.append(step.command())
            if index == 0 and int(result.get("status") or 0) != 0:
                detail = (result.get("stderr") or result.get("stdout") or "").strip()
                lowered = detail.lower()
                absent = (
                    int(result.get("status") or 0) == 5
                    or "not loaded" in lowered
                    or "not found" in lowered
                    or "could not be found" in lowered
                )
                if not absent:
                    raise MinerError(f"{step.title} : code {result.get('status')} — {detail[:300]}")
                executed.append(f"# unité {unit} absente : rien à arrêter")
                return {
                    "simulated": False,
                    "ok": True,
                    "mode": "systemd",
                    "commands": executed,
                    "note": f"unité {unit} absente : rien à arrêter",
                    "status": await _status(machine, profile, unit, user, executed),
                }
            if step.title == "état après arrêt":
                state = (result.get("stdout") or "").strip()
                executed.append(f"# état après arrêt : {state}")
        return {"simulated": False, "ok": True, "mode": "systemd", "commands": executed,
                "status": await _status(machine, profile, unit, user, executed)}

    # Fallback: read the PID, prove it is ours, then kill it.
    pid_step = Step("lecture du PID Fossor", ("cat", M.pid_file(profile)), allow_fail=True)
    pid_result = await _run(machine, pid_step)
    executed.append(pid_step.command())
    pid = (pid_result.get("stdout") or "").strip()
    if not pid.isdigit():
        return {"simulated": False, "ok": True, "mode": "nohup", "commands": executed,
                "status": await _status(machine, profile, unit, user, executed),
                "note": "Aucun processus Fossor en cours."}
    ident = await _run(machine, Step("identité du processus", ("ps", "-p", pid, "-o", "args="), allow_fail=True))
    executed.append(shlex.join(("ps", "-p", pid, "-o", "args=")))
    if M.INSTALL_ROOT not in (ident.get("stdout") or ""):
        raise MinerError(
            "PID refusé : ce processus n'a pas été lancé par Fossor — arrêt annulé."
        )
    await _run(machine, Step("arrêt du processus", _sudo(user, "kill", "-TERM", pid), allow_fail=True))
    executed.append(shlex.join(_sudo(user, "kill", "-TERM", pid)))
    wait = 'for i in $(seq 1 20); do kill -0 "$1" 2>/dev/null || exit 0; sleep 0.5; done; exit 1'
    await _run(machine, Step("attente de l'arrêt", _sh(wait, pid), allow_fail=True, timeout=30))
    executed.append(shlex.join(_sh(wait, pid)))
    await _run(machine, Step("retrait du fichier PID", _sudo(user, "rm", "-f", M.pid_file(profile)), allow_fail=True))
    executed.append(shlex.join(_sudo(user, "rm", "-f", M.pid_file(profile))))
    return {"simulated": False, "ok": True, "mode": "nohup", "commands": executed,
            "status": await _status(machine, profile, unit, user, executed)}


async def _status(
    machine: Any,
    profile: dict[str, Any],
    unit: str,
    user: str,
    executed: list[str],
) -> dict[str, Any]:
    steps = status_plan(profile, unit, user)
    active = ""
    pid = ""
    hashrate_text = ""
    for step in steps:
        result = await _run(machine, step)
        executed.append(step.command())
        if step.title == "service actif":
            active = (result.get("stdout") or "").strip()
        elif step.title == "PID principal":
            candidate = (result.get("stdout") or "").strip()
            if candidate.isdigit() and candidate != "0":
                pid = candidate
        elif step.title == "PID du repli" and not pid:
            candidate = (result.get("stdout") or "").strip()
            if candidate.isdigit():
                pid = candidate
        elif step.title.startswith("hashrate"):
            hashrate_text = result.get("stdout") or ""

    uptime_s: int | None = None
    if pid:
        etimes = await _run(
            machine, Step("durée de vie", ("ps", "-p", pid, "-o", "etimes="), allow_fail=True)
        )
        executed.append(shlex.join(("ps", "-p", pid, "-o", "etimes=")))
        raw = (etimes.get("stdout") or "").strip()
        uptime_s = int(raw) if raw.isdigit() else None

    running = bool(pid) or active in ("active", "activating")
    return {
        "running": running,
        "state": active or ("running" if running else "inactive"),
        "pid": int(pid) if pid else None,
        "uptime_s": uptime_s,
        "hashrate_hs": parse_hashrate(hashrate_text),
        "service": unit,
    }


async def status(
    machine: Any, profile: dict[str, Any], *, simulated: bool, user: str
) -> dict[str, Any]:
    unit = M.unit_name(profile)
    if simulated:
        return _simulated(status_plan(profile, unit, user))
    executed: list[str] = []
    state = await _status(machine, profile, unit, user, executed)
    return {"simulated": False, "ok": True, "commands": executed, "status": state}
