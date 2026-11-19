"""Exécution du pilotage HiveOS — sonde en lecture seule, arrêt et reprise.

La sonde est une commande **de la liste blanche** (``hiveos_probe``) : aucun
fragment n'est assemblé depuis une entrée.  Les plans viennent de
:mod:`app.hiveos` (pur) ; ce module ne fait que les exécuter, ou les rendre tels
quels en simulation.

``RUNNER`` est la seule porte vers l'extérieur.  Le chemin simulé retourne
**avant** de le toucher (les tests installent un runner qui lève).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from app import hiveos as H

PROBE_TIMEOUT = 45
STEP_TIMEOUT = 90
#: Attente du retour (ou de la chute) du hashrate : HiveOS réécrit
#: ``last_stat.json`` à chaque cycle de collecte (quelques dizaines de secondes).
#: Grâce accordée au mineur pour mourir après ``miner stop`` (avant de conclure).
EXIT_GRACE_S = 8
HASHRATE_WAIT_S = 90
HASHRATE_INTERVAL_S = 15

Runner = Callable[..., Awaitable[dict[str, Any]]]
RUNNER: Runner | None = None


class HiveosError(H.HiveosError):
    """Échec d'exécution HiveOS."""


async def default_runner(
    machine: Any, command: str, stdin: str | None = None, timeout: int = STEP_TIMEOUT
) -> dict[str, Any]:
    """Une commande sur la connexion épinglée (même porte que les mineurs)."""
    from app import miner_remote as MR

    return await MR.default_runner(machine, command, stdin, timeout)


def probe_command() -> str:
    from app.ssh_client import WHITELISTED_COMMANDS

    return WHITELISTED_COMMANDS["hiveos_probe"]


async def _run(runner: Runner, machine: Any, argv: tuple[str, ...], timeout: int) -> dict[str, Any]:
    from shlex import join

    return await runner(machine, join(argv), None, timeout)


async def probe(machine: Any, *, simulated: bool = False) -> dict[str, Any]:
    """Sonde HiveOS en lecture seule ; simulation = aucun appel."""
    if simulated:
        return {
            "detected": False,
            "simulated": True,
            "reason": "simulation : aucune sonde HiveOS exécutée",
            "rig": {"watchdog": False, "maintenance": False, "keys": [], "values": {}},
            "miner": "",
            "processes": [],
            "gpu_total": 0,
        }
    runner = RUNNER or default_runner
    result = await runner(machine, probe_command(), None, PROBE_TIMEOUT)
    if not result.get("ok"):
        detail = (result.get("stderr") or "").strip()[:200] or f"code {result.get('status')}"
        raise HiveosError(f"sonde HiveOS impossible : {detail}")
    parsed = H.parse_probe(result.get("stdout") or "")
    if not parsed["detected"]:
        raise HiveosError(
            "HiveOS non détecté : ni /hive ni /hive-config/wallet.conf sur cette machine"
        )
    parsed["simulated"] = False
    return parsed


async def _execute(
    machine: Any, steps: list[H.Step], *, simulated: bool
) -> list[dict[str, Any]]:
    if simulated:
        return [
            {"title": step.title, "command": step.command(), "status": None, "stdout": ""}
            for step in steps
        ]
    runner = RUNNER or default_runner
    executed: list[dict[str, Any]] = []
    for step in steps:
        result = await _run(runner, machine, step.argv, step.timeout)
        executed.append(
            {
                "title": step.title,
                "command": step.command(),
                "status": result.get("status"),
                "stdout": (result.get("stdout") or "")[:2000],
                "stderr": (result.get("stderr") or "")[:500],
            }
        )
        if not result.get("ok") and not step.allow_fail:
            detail = (result.get("stderr") or "").strip()[:200] or f"code {result.get('status')}"
            raise HiveosError(f"{step.title} : {detail}")
    return executed


def _ps_step_output(executed: list[dict[str, Any]]) -> str:
    for entry in executed:
        if entry["command"].startswith("ps -eo"):
            return entry.get("stdout") or ""
    return ""


async def _read_hashrate(machine: Any) -> tuple[float | None, str]:
    """Relit ``last_stat.json`` par la sonde (lecture seule)."""
    data = await probe(machine)
    return data.get("hashrate_hs"), data.get("hashrate_source") or ""


async def stop(
    machine: Any,
    probe_data: dict[str, Any],
    *,
    ack_watchdog: bool = False,
    simulated: bool = False,
    wait_s: int = HASHRATE_WAIT_S,
    interval_s: int = HASHRATE_INTERVAL_S,
) -> dict[str, Any]:
    """``miner stop`` (+ ``wd stop`` si validé), vérifié par ``ps`` et hashrate."""
    steps = H.plan_stop(probe_data, ack_watchdog)  # refuse avant tout appel
    commands = [step.command() for step in steps]
    if simulated:
        return {
            "simulated": True,
            "commands": commands,
            "executed": await _execute(machine, steps, simulated=True),
            "verified": True,
            "before_hashrate_hs": probe_data.get("hashrate_hs"),
            "after_hashrate_hs": None,
            "hashrate_dropped": None,
            "note": "simulation : aucune commande exécutée",
        }
    executed = await _execute(machine, steps, simulated=False)
    processes = H.parse_processes(_ps_step_output(executed))
    remaining = [row for row in processes if "/hive/miners/" in row["args"]]
    # Un mineur réel met une à deux secondes à mourir après SIGTERM : on
    # relit `ps` un court instant avant de conclure (borné, jamais infini).
    runner = RUNNER or default_runner
    waited_exit = 0
    while remaining and waited_exit < EXIT_GRACE_S:
        await asyncio.sleep(2)
        waited_exit += 2
        again = await _run(runner, machine, ("ps", "-eo", "pid,args"), STEP_TIMEOUT)
        remaining = [
            row for row in H.parse_processes(again.get("stdout") or "")
            if "/hive/miners/" in row["args"]
        ]
    before = probe_data.get("hashrate_hs")
    after: float | None = None
    dropped: bool | None = None
    waited = 0
    while True:
        after, source = await _read_hashrate(machine)
        dropped = after is None or not before or after <= max(float(before) * 0.1, 0.0)
        if dropped or waited >= max(wait_s, 0):
            break
        await asyncio.sleep(max(interval_s, 1))
        waited += max(interval_s, 1)
    note = ""
    if not dropped:
        note = (
            f"hashrate encore lu ({after} H/s) : last_stat.json n'a pas encore été réécrit "
            f"par HiveOS après {waited} s"
        )
    return {
        "simulated": False,
        "commands": commands,
        "executed": executed,
        "verified": not remaining,
        "processes_remaining": remaining,
        "before_hashrate_hs": before,
        "after_hashrate_hs": after,
        "hashrate_dropped": dropped,
        "note": note,
    }


async def resume(
    machine: Any,
    probe_data: dict[str, Any],
    *,
    ack_watchdog: bool = False,
    simulated: bool = False,
    wait_s: int = HASHRATE_WAIT_S,
    interval_s: int = HASHRATE_INTERVAL_S,
) -> dict[str, Any]:
    """``miner start`` et retour du hashrate vérifié (comme en 3b)."""
    steps = H.plan_start(probe_data, ack_watchdog)
    commands = [step.command() for step in steps]
    if simulated:
        return {
            "simulated": True,
            "commands": commands,
            "executed": await _execute(machine, steps, simulated=True),
            "verified": True,
            "hashrate_hs": None,
            "source": "simulation",
            "note": "simulation : reprise par HiveOS (miner start) non exécutée",
        }
    executed = await _execute(machine, steps, simulated=False)
    hashrate: float | None = None
    source = ""
    waited = 0
    while True:
        hashrate, source = await _read_hashrate(machine)
        if hashrate and hashrate > 0:
            break
        if waited >= max(wait_s, 0):
            break
        await asyncio.sleep(max(interval_s, 1))
        waited += max(interval_s, 1)
    verified = bool(hashrate and hashrate > 0)
    return {
        "simulated": False,
        "commands": commands,
        "executed": executed,
        "verified": verified,
        "hashrate_hs": hashrate,
        "source": source or "last_stat.json",
        "note": "" if verified else f"hashrate non revenu après {waited} s",
    }
