"""Exécution des actions Proxmox sur l'hôte — simulation par défaut (§4.6).

Les plans viennent de :mod:`app.pve_vm` (pur). Une seule porte vers l'extérieur,
``RUNNER``, et un retour **avant** tout appel quand la simulation est active : le
test unitaire installe un runner qui lève pour le prouver.

L'hôte est joint en SSH avec le compte racine du préréglage ``os_kind=proxmox``
(§4.1 ter) : les commandes ``qm``/``pvesh``/``pvesm`` partent donc **sans**
``sudo``. ``pct`` est refusé ici, en plus de ne figurer dans aucun plan.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from app import pve_vm as P


class PveRemoteError(RuntimeError):
    """Action Proxmox refusée ou échouée — message sûr pour l'opérateur."""


Runner = Callable[..., Awaitable[dict[str, Any]]]
RUNNER: Runner | None = None


async def _default_runner(host: Any, command: str, stdin: str | None, timeout: int) -> dict[str, Any]:
    from app.miner_remote import default_runner

    return await default_runner(host, command, stdin=stdin, timeout=timeout)


def _guard(step: P.VmStep) -> None:
    if step.argv and step.argv[0] == "pct":
        raise PveRemoteError("commande « pct » refusée : un conteneur n'est jamais touché par Fossor.")


async def _run(host: Any, step: P.VmStep) -> dict[str, Any]:
    _guard(step)
    runner = RUNNER or _default_runner
    result = await runner(host, step.command(), stdin=step.stdin, timeout=step.timeout)
    if not result.get("ok", True):
        raise PveRemoteError(
            f"{step.title} : connexion impossible — {result.get('stderr') or 'erreur SSH'}"
        )
    status = int(result.get("status", 0) or 0)
    stdout = result.get("stdout") or ""
    if status != 0 and not step.allow_fail:
        detail = (result.get("stderr") or stdout or "").strip()[:300]
        raise PveRemoteError(f"{step.title} : code {status} — {detail}")
    if step.expect and status == 0 and step.expect not in stdout:
        raise PveRemoteError(f"{step.title} : sortie inattendue — {stdout.strip()[:200]}")
    return {
        "status": status,
        "stdout": stdout,
        "stderr": result.get("stderr") or "",
        "command": step.command(),
    }


def _simulated(plan: list[P.VmStep], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "simulated": True,
        "commands": [step.command() for step in plan],
        "note": "Simulation : aucune commande n'est exécutée.",
    }
    if extra:
        payload.update(extra)
    return payload


def install_key_plan(private_key: str) -> list[P.VmStep]:
    """Clé privée dédiée dans ``/root/.ssh`` de l'hôte (0400) — jamais dans un argv."""
    if not (private_key or "").strip():
        raise PveRemoteError("clé SSH des VM absente : générez-la avant de créer une VM")
    return [
        P.VmStep("clé SSH dédiée (privée, 0400)", ("tee", P.KEY_PATH),
                 stdin=private_key if private_key.endswith("\n") else private_key + "\n"),
        P.VmStep("droits de la clé", ("chmod", "0400", P.KEY_PATH)),
        P.VmStep("known_hosts dédié", ("touch", P.KNOWN_HOSTS), allow_fail=True),
    ]


async def inspect(host: Any, *, simulated: bool, model: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lecture seule : nœud, stockages ``images``, VMID pris et premier libre."""
    plan = P.inspect_plan()
    if simulated:
        return _simulated(plan, {"node": "", "storages": [], "taken": [], "free_vmid": None})
    results = {}
    for step in plan:
        results[step.title] = await _run(host, step)
    statuses = P.parse_pvesm_status(results["stockages et espace libre"]["stdout"])
    storages = P.parse_pve_storages(results["stockages et contenus acceptés"]["stdout"])
    taken = P.parse_cluster_resources(results["VM prises sur le cluster"]["stdout"])
    listed = P.parse_qm_list(results["VM prises sur le nœud"]["stdout"])
    taken |= set(listed)
    detect = P.parse_detect(results["version de Proxmox"]["stdout"]
                            + results["version de Proxmox"]["stderr"])
    payload: dict[str, Any] = {
        "simulated": False,
        "version": detect["version"],
        "storages": statuses,
        "storage_content": storages,
        "taken": sorted(taken),
        "commands": [step.command() for step in plan],
    }
    if model:
        need = int(model.get("disk_gb") or 0)
        eligible = P.eligible_storages(statuses, storages, need)
        payload["eligible"] = eligible
        try:
            payload["free_vmid"] = P.choose_vmid(
                int(model["vmid_min"]), int(model["vmid_max"]), taken
            )
        except P.VmError as exc:
            payload["free_vmid"] = None
            payload["free_vmid_error"] = str(exc)
    return payload


async def detect(host: Any, *, simulated: bool) -> dict[str, Any]:
    """Détection ``vm_host`` : ``pveversion`` et ``/dev/kvm``."""
    plan = P.detect_plan()
    if simulated:
        return _simulated(plan, {"hypervisor": "", "version": "", "kvm": None, "vm_host": None})
    text = ""
    for step in plan:
        result = await _run(host, step)
        text += (result["stdout"] or "") + (result["stderr"] or "") + "\n"
    verdict = P.parse_detect(text)
    verdict.update({"simulated": False, "commands": [step.command() for step in plan]})
    return verdict


async def create(
    host: Any,
    model: dict[str, Any],
    name: str,
    vmid: int,
    *,
    ssh_public_key: str,
    private_key: str = "",
    simulated: bool,
    enforce: bool = True,
) -> dict[str, Any]:
    """Création d'une VM jetable. ``enforce`` relit la plage avant d'agir."""
    if enforce and not P.in_range(int(vmid), model["vmid_min"], model["vmid_max"]):
        raise PveRemoteError(
            f"VMID {vmid} hors de la plage réservée {model['vmid_min']}-{model['vmid_max']}."
        )
    plan = P.create_plan(model, name, vmid, ssh_public_key)
    key_steps = install_key_plan(private_key) if private_key else []
    if simulated:
        return _simulated(key_steps + plan, {"name": name, "vmid": int(vmid), "state": "creating"})
    commands: list[str] = []
    for step in key_steps + plan:
        await _run(host, step)
        commands.append(step.command())
    return {
        "simulated": False,
        "name": name,
        "vmid": int(vmid),
        "state": "running",
        "image": P.image_path(model),
        "commands": commands,
    }


async def config(host: Any, vmid: int, *, simulated: bool) -> dict[str, Any]:
    plan = P.config_plan(vmid)
    if simulated:
        return _simulated(plan, {"name": "", "tags": [], "description": "", "mac": ""})
    result = await _run(host, plan[0])
    parsed = P.parse_qm_config(result["stdout"])
    parsed.update({"simulated": False, "commands": [plan[0].command()]})
    return parsed


async def wait_address(
    host: Any, vmid: int, *, simulated: bool, attempts: int = 24, pause: float = 5.0
) -> dict[str, Any]:
    """Agent invité, puis MAC → table ARP de l'hôte (lecture seule)."""
    plan = P.ip_plan(vmid)
    if simulated:
        return _simulated(plan, {"ip": "", "mac": "", "source": ""})
    cfg = await config(host, vmid, simulated=False)
    mac = cfg.get("mac") or ""
    for _ in range(max(1, attempts)):
        agent = await _run(host, plan[0])
        ip = P.parse_guest_ip(agent["stdout"])
        if ip:
            return {"simulated": False, "ip": ip, "mac": mac, "source": "agent invité"}
        neigh = await _run(host, plan[1])
        ip = P.parse_ip_neigh(neigh["stdout"], mac)
        if ip:
            return {"simulated": False, "ip": ip, "mac": mac, "source": "table ARP de l'hôte"}
        await asyncio.sleep(pause)
    raise PveRemoteError(
        f"La VM {vmid} n'a pas obtenu d'adresse IP (agent invité muet et MAC {mac or 'inconnue'} "
        "absente de la table ARP)."
    )


async def shutdown(
    host: Any, vmid: int, *, simulated: bool, timeout_s: int = 120
) -> dict[str, Any]:
    plan = P.shutdown_plan(vmid, timeout_s)
    if simulated:
        return _simulated(plan, {"state": "shut off"})
    for step in plan:
        await _run(host, step)
    state = P.parse_qm_status((await _run(host, plan[-1]))["stdout"])
    return {"simulated": False, "state": state, "commands": [step.command() for step in plan]}


async def stop(host: Any, vmid: int, *, simulated: bool) -> dict[str, Any]:
    plan = P.stop_plan(vmid)
    if simulated:
        return _simulated(plan, {"state": "stopped"})
    for step in plan:
        await _run(host, step)
    return {"simulated": False, "state": "stopped", "commands": [step.command() for step in plan]}


async def destroy(
    host: Any,
    model: dict[str, Any],
    vmid: int,
    *,
    backup: dict[str, Any] | None,
    now_epoch: int,
    max_age_days: int,
    simulated: bool,
) -> dict[str, Any]:
    """Sauvegarde vérifiée **avant toute commande**, puis propriété relue sur l'hôte."""
    P.ensure_backup(backup, now_epoch, max_age_days)
    cfg = await config(host, vmid, simulated=simulated)
    tags = cfg.get("tags") or []
    description = cfg.get("description") or ""
    if simulated:
        # La simulation ne relit rien : elle n'affirme aucune propriété.
        tags = tags or [P.PVE_TAG]
        description = description or P.OWNER_MARKER
    P.ensure_destroyable(
        int(vmid), model["vmid_min"], model["vmid_max"], tags, description,
        backup, now_epoch, max_age_days,
    )
    plan = P.destroy_plan(vmid)
    if simulated:
        return _simulated(plan, {"destroyed": False})
    commands: list[str] = []
    for step in plan:
        await _run(host, step)
        commands.append(step.command())
    return {"simulated": False, "destroyed": True, "commands": commands}


async def list_guests(host: Any, *, simulated: bool) -> dict[str, Any]:
    """``qm list`` en lecture seule — jamais ``pct``."""
    plan = [P.VmStep("VM présentes sur le nœud", ("qm", "list"), allow_fail=True)]
    if simulated:
        return _simulated(plan, {"vms": []})
    result = await _run(host, plan[0])
    guests = P.parse_qm_list(result["stdout"])
    return {
        "simulated": False,
        "vms": [{"vmid": key, **value} for key, value in sorted(guests.items())],
        "commands": [plan[0].command()],
    }


async def cleanup(host: Any, *, simulated: bool) -> dict[str, Any]:
    plan = P.cleanup_plan()
    if simulated:
        return _simulated(plan, {"cleaned": False})
    for step in plan:
        await _run(host, step)
    return {"simulated": False, "cleaned": True, "commands": [step.command() for step in plan]}


def backup_verdict(stat: dict[str, Any], now_epoch: int | None = None,
                   max_age_days: int = P.DEFAULT_BACKUP_MAX_AGE_DAYS) -> dict[str, Any]:
    """Verdict §10.11 sur un ``stat`` déjà relevé (aucune lecture du fichier)."""
    return P.evaluate_backup(stat, int(time.time()) if now_epoch is None else now_epoch, max_age_days)
