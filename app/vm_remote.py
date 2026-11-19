"""Exécution des actions de VM sur l'hôte libvirt — simulation par défaut (§4.6).

Les plans viennent de :mod:`app.spec_vm` (pur). Ici, une seule porte vers
l'extérieur, ``RUNNER``, et un retour **avant** tout appel quand la simulation
est active : le test unitaire installe un runner qui lève pour le prouver.

Toutes les commandes passent par SSH vers la machine hôte, avec ``sudo -n``
(le compte dédié de Fossor est dans le sudoers depuis la préparation). Le compte
``fossor`` n'a **jamais** besoin d'être root sur la VM : les commandes dans la
VM passent par la clé dédiée, écrites sur l'hôte en 0400.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app import spec_vm as V


class VmRemoteError(RuntimeError):
    """Action de VM refusée ou échouée — message sûr pour l'opérateur."""


Runner = Callable[..., Awaitable[dict[str, Any]]]
RUNNER: Runner | None = None


async def _default_runner(host: Any, command: str, stdin: str | None, timeout: int) -> dict[str, Any]:
    from app.miner_remote import default_runner

    return await default_runner(host, command, stdin=stdin, timeout=timeout)


async def _run(host: Any, step: V.VmStep) -> dict[str, Any]:
    runner = RUNNER or _default_runner
    result = await runner(host, step.command(), stdin=step.stdin, timeout=step.timeout)
    if not result.get("ok", True):
        raise VmRemoteError(
            f"{step.title} : connexion impossible — {result.get('stderr') or 'erreur SSH'}"
        )
    status = int(result.get("status", 0) or 0)
    stdout = result.get("stdout") or ""
    if status != 0 and not step.allow_fail:
        detail = (result.get("stderr") or stdout or "").strip()[:300]
        raise VmRemoteError(f"{step.title} : code {status} — {detail}")
    if step.expect and status == 0 and step.expect not in stdout:
        raise VmRemoteError(f"{step.title} : sortie inattendue — {stdout.strip()[:200]}")
    return {"status": status, "stdout": stdout, "stderr": result.get("stderr") or "",
            "command": step.command()}


def _simulated(plan: list[V.VmStep], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "simulated": True,
        "commands": [step.command() for step in plan],
        "note": "Simulation : aucune commande n'est exécutée.",
    }
    if extra:
        payload.update(extra)
    return payload


def key_plan(private_key: str) -> list[V.VmStep]:
    """Pose la clé privée dédiée sur l'hôte (0400, root) — jamais dans un argv."""
    if not (private_key or "").strip():
        raise VmRemoteError("clé SSH des VM absente : générez-la avant de créer une VM")
    return [
        V.VmStep("clé SSH dédiée (privée, 0400)", ("sudo", "-n", "tee", V.KEY_PATH),
                 stdin=private_key if private_key.endswith("\n") else private_key + "\n"),
        V.VmStep("droits de la clé", ("sudo", "-n", "chmod", "0400", V.KEY_PATH)),
        V.VmStep("known_hosts dédié", ("sudo", "-n", "touch", V.KNOWN_HOSTS), allow_fail=True),
    ]


def create_plan(model: dict[str, Any], name: str, ssh_public_key: str, private_key: str) -> list[V.VmStep]:
    return key_plan(private_key) + V.create_plan(model, name, ssh_public_key)


KVM_PROBE_SCRIPT = (
    "test -e /dev/kvm && echo KVM=1; "
    "command -v virsh >/dev/null 2>&1 && echo VIRSH=1; "
    "command -v pveversion >/dev/null 2>&1 && echo PVERSION=1; "
    "echo \"KVM_DOMAIN=$(virsh --connect qemu:///system capabilities 2>/dev/null | "
    "grep -c 'domain type=.kvm.')\"; exit 0"
)
KVM_MISSING_MESSAGE = (
    "libvirt sans KVM (émulation seule) : modes vm/hybrid indisponibles sur cet hôte"
)


async def probe_kvm(host: Any, *, simulated: bool) -> dict[str, Any]:
    """``/dev/kvm`` **et** ``domain type='kvm'`` dans ``virsh capabilities``.

    Lecture seule. Sans les deux, ``virt-install --cpu max`` échoue à la
    dernière étape : le refus arrive donc avant toute écriture.
    """
    if simulated:
        return {
            "simulated": True, "kvm": None, "kvm_device": None, "kvm_domain": None,
            "message": "simulation : contrôle KVM non exécuté",
        }
    result = await _run(
        host, V.VmStep("contrôle KVM", ("bash", "-lc", KVM_PROBE_SCRIPT), allow_fail=True)
    )
    from app.hw_parser import parse_virt

    flags = parse_virt(result["stdout"])
    capable = bool(flags.get("vm_capable"))
    return {
        "simulated": False,
        "kvm": capable,
        "kvm_device": bool(flags.get("kvm")),
        "kvm_domain": int(flags.get("kvm_domain") or 0),
        "pveversion": bool(flags.get("pveversion")),
        "message": "" if capable else KVM_MISSING_MESSAGE,
        "stdout": result["stdout"].strip()[:400],
    }


async def create(
    host: Any,
    model: dict[str, Any],
    name: str,
    *,
    ssh_public_key: str,
    private_key: str,
    simulated: bool,
) -> dict[str, Any]:
    plan = create_plan(model, name, ssh_public_key, private_key)
    if simulated:
        return _simulated(plan, {"name": name, "state": "creating"})
    probe = await probe_kvm(host, simulated=False)
    if not probe["kvm"]:
        raise VmRemoteError(probe["message"] or KVM_MISSING_MESSAGE)
    for step in plan:
        await _run(host, step)
    if not model.get("keep_image_cache"):
        # Cache désactivé : l'image de base ne reste pas sur l'hôte libvirt.
        await _run(host, V.VmStep("cache d'image libvirt vidé",
                                  ("rm", "-rf", f"{V.IMAGE_ROOT}/images"), allow_fail=True))
    return {"simulated": False, "name": name, "state": "running",
            "cache_kept": bool(model.get("keep_image_cache")),
            "commands": [s.command() for s in plan]}


async def wait_address(host: Any, name: str, *, simulated: bool, attempts: int = 12,
                       pause: float = 5.0) -> dict[str, Any]:
    import asyncio

    plan = V.ip_plan(name)
    if simulated:
        return _simulated(plan, {"ip": "", "state": "running"})
    last = ""
    for _ in range(max(1, attempts)):
        for step in plan:
            result = await _run(host, step)
            last = result["stdout"] or last
        ip = V.parse_domifaddr(last)
        if ip:
            return {"simulated": False, "ip": ip, "state": "running"}
        await asyncio.sleep(pause)
    raise VmRemoteError(f"La VM {name} n'a pas obtenu d'adresse IP (bail ARP puis lease vides).")


async def pin_host_key(host: Any, ip: str, *, simulated: bool) -> dict[str, Any]:
    plan = V.host_key_plan(ip)
    if simulated:
        return _simulated(plan, {"host_key": ""})
    result = await _run(host, plan[0])
    line = ""
    for raw in (result["stdout"] or "").splitlines():
        if raw.strip().startswith(("ed25519", "ssh-")) or " ssh-ed25519 " in raw:
            parts = raw.split()
            if len(parts) >= 2:
                line = f"{ip} ssh-ed25519 {parts[-1]}"
                break
    if not line:
        raise VmRemoteError("Clé d'hôte de la VM introuvable (ssh-keyscan vide).")
    for step in V.trust_plan(ip, line):
        await _run(host, step)
    return {"simulated": False, "host_key": line}


async def provision(
    host: Any,
    model: dict[str, Any],
    profile: dict[str, Any],
    ip: str,
    *,
    coin: str,
    simulated: bool,
    expected_address: str = "",
) -> dict[str, Any]:
    pushed = False
    if str(profile.get("binary_source") or "url") == "fossor":
        if not simulated:
            await push_guest_binary(host, profile)
        pushed = True
    plan = V.provision_plan(model, profile, ip, coin=coin, binary_pushed=pushed)
    if simulated:
        return _simulated(plan, {"address": expected_address, "verified": False})
    commands: list[str] = []
    address = expected_address
    for step in plan:
        result = await _run(host, step)
        commands.append(step.command())
        if step.title.startswith("création du wallet"):
            found = _address_from_stdout(result["stdout"])
            if found and expected_address and found != expected_address:
                raise VmRemoteError(
                    "Le wallet créé dans la VM ne correspond pas à l'adresse du profil — "
                    "campagne refusée."
                )
            if found:
                address = found
    return {"simulated": False, "address": address, "verified": bool(address), "commands": commands}


async def push_guest_binary(host: Any, profile: dict[str, Any]) -> dict[str, Any]:
    """Archive Fossor poussée dans la VM libvirt, SHA256 vérifié des deux côtés."""
    import base64

    from app import binaries as B

    path, digest = B.verified_path(profile)
    kind = str(profile.get("binary_kind") or "tar.gz").strip().lower()
    dest = f"{V.INSTALL_DIR}/miner.{V.BINARY_SUFFIXES.get(kind, 'tar.gz')}"
    await _run(host, V.VmStep(
        "copie du binaire : cible vidée",
        ("sh", "-c", 'p="$1"; install -d -m 0700 "$(dirname "$p")" && rm -f "$p"', "fossor", dest)))
    sent = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(256 * 1024)
            if not chunk:
                break
            await _run(host, V.VmStep(
                "copie du binaire (fragment)",
                ("sh", "-c", 'p="$1"; base64 -d >> "$p"', "fossor", dest),
                stdin=base64.b64encode(chunk).decode("ascii"), timeout=600))
            sent += len(chunk)
    check = await _run(host, V.VmStep(
        "somme du binaire reçu dans la VM",
        ("sh", "-c", 'echo "$2  $1" | sha256sum -c -', "fossor", dest, digest),
        allow_fail=True, timeout=600))
    if "OK" not in (check["stdout"] or ""):
        raise VmRemoteError("copie du binaire : SHA256 non confirmé dans la VM — exécution refusée")
    return {"verified": True, "sha256": digest, "bytes": sent, "path": dest}


def _address_from_stdout(stdout: str) -> str:
    import re

    for raw in reversed((stdout or "").splitlines()):
        line = raw.strip()
        if re.fullmatch(r"[A-Za-z0-9:._-]{8,128}", line):
            return line
    return ""


async def start_miner(host: Any, profile: dict[str, Any], argv: list[str], ip: str, *,
                      simulated: bool) -> dict[str, Any]:
    plan = V.vm_miner_start_plan(profile, argv, ip)
    if simulated:
        return _simulated(plan)
    result = await _run(host, plan[0])
    return {"simulated": False, "pid": (result["stdout"] or "").strip()[:12],
            "commands": [plan[0].command()]}


async def stop_miner(host: Any, profile: dict[str, Any], ip: str, *, simulated: bool) -> dict[str, Any]:
    plan = V.vm_miner_stop_plan(profile, ip)
    if simulated:
        return _simulated(plan)
    for step in plan:
        await _run(host, step)
    return {"simulated": False, "stopped": True, "commands": [s.command() for s in plan]}


async def check_binary(host: Any, ip: str, profile: dict[str, Any], *,
                       simulated: bool) -> dict[str, Any]:
    """Lance ``--version`` dans la VM **avant** d'écrire campaign-started."""
    plan = V.binary_probe_plan(ip, profile)
    if simulated:
        return _simulated(plan, {"ok": True, "message": ""})
    result = await _run(host, plan[0])
    verdict = V.parse_binary_probe(result["stdout"] + "\n" + result["stderr"])
    verdict["commands"] = [plan[0].command()]
    return verdict


async def check_alive(host: Any, ip: str, profile: dict[str, Any], *,
                      simulated: bool) -> dict[str, Any]:
    plan = V.alive_plan(ip, profile)
    if simulated:
        return _simulated(plan, {"alive": True, "processes": []})
    result = await _run(host, plan[0])
    verdict = V.parse_alive(result["stdout"], profile)
    verdict["commands"] = [plan[0].command()]
    return verdict


async def miner_log_tail(host: Any, ip: str, profile: dict[str, Any], *,
                         simulated: bool) -> dict[str, Any]:
    plan = V.miner_log_tail_plan(ip, profile)
    if simulated:
        return _simulated(plan, {"tail": ""})
    result = await _run(host, plan[0])
    return {"tail": result["stdout"][-4000:], "commands": [plan[0].command()]}


async def shutdown(host: Any, name: str, *, simulated: bool) -> dict[str, Any]:
    plan = V.shutdown_plan(name)
    if simulated:
        return _simulated(plan, {"state": "shut off"})
    for step in plan:
        await _run(host, step)
    return {"simulated": False, "state": "shut off", "commands": [s.command() for s in plan]}


async def destroy(
    host: Any,
    name: str,
    description: str,
    *,
    backup: dict[str, Any] | None,
    now_epoch: int,
    max_age_days: int,
    simulated: bool,
    keep_cache: bool = True,
) -> dict[str, Any]:
    """Destruction : propriété puis sauvegarde vérifiée, **avant** toute commande."""
    V.ensure_destroyable(name, description, backup, now_epoch, max_age_days)
    plan = V.destroy_plan(name)
    if not keep_cache:
        plan = plan + [V.VmStep("cache d'image libvirt vidé",
                                ("rm", "-rf", f"{V.IMAGE_ROOT}/images"), allow_fail=True)]
    if simulated:
        return _simulated(plan, {"destroyed": False})
    commands = []
    for step in plan:
        await _run(host, step)
        commands.append(step.command())
    return {"simulated": False, "destroyed": True, "commands": commands}


async def list_vms(host: Any, *, simulated: bool) -> dict[str, Any]:
    plan = V.list_plan()
    if simulated:
        return _simulated(plan, {"vms": []})
    result = await _run(host, plan[0])
    infos = V.parse_vm_list(result["stdout"])
    return {
        "simulated": False,
        "vms": [
            {"name": item.name, "description": item.description, "owned": item.owned}
            for item in infos
        ],
        "commands": [plan[0].command()],
    }


async def hypervisor(host: Any, *, simulated: bool) -> dict[str, Any]:
    """Détection seulement : libvirt utilisable, ou Proxmox « pilote à venir »."""
    plan = V.hypervisor_plan()
    if simulated:
        return _simulated(plan, {"driver": "none", "message": ""})
    libvirt_result = await _run(host, plan[0])
    proxmox_result = await _run(host, plan[1])
    lib = V.parse_libvirt_version(libvirt_result["stdout"] + libvirt_result["stderr"])
    pve = V.parse_pveversion(proxmox_result["stdout"] + proxmox_result["stderr"])
    if lib["driver"] == "libvirt":
        return {"simulated": False, "driver": "libvirt", "version": lib["version"],
                "message": "libvirt utilisable : pilote VM actif."}
    if pve["driver"] == "proxmox":
        return {"simulated": False, "driver": "proxmox", "version": pve["version"],
                "message": "Proxmox : pilote à venir."}
    return {"simulated": False, "driver": "none", "version": "",
            "message": "Ni libvirt ni Proxmox détectés sur cette machine."}


async def check_backup(host: Any, ip: str, path: str, *, now_epoch: int,
                       max_age_days: int, simulated: bool,
                       wallet_mode: str = "file") -> dict[str, Any]:
    import posixpath

    if wallet_mode == "pool_address":
        plan = V.pool_wallet_check_plan(ip, posixpath.dirname(path) or V.DATA_DIR)
    else:
        plan = V.backup_check_plan(ip, path)
    if simulated:
        return _simulated(plan, {"verified": False, "reason": "simulation"})
    result = await _run(host, plan[0])
    if wallet_mode == "pool_address":
        verdict = V.evaluate_pool_wallet(V.parse_dir_listing(result["stdout"]), now_epoch)
    else:
        stat = V.parse_backup_stat(result["stdout"])
        verdict = V.evaluate_backup(stat, now_epoch, max_age_days)
    verdict["commands"] = [plan[0].command()]
    return verdict


async def collect(host: Any, name: str, ip: str, profile: dict[str, Any] | None, *,
                  simulated: bool) -> dict[str, Any]:
    plan = V.collect_plan(name, ip, profile)
    if simulated:
        return _simulated(plan, {"state": "", "hashrate_hs": None})
    state = ""
    info: dict[str, Any] = {}
    hashrate: float | None = None
    processes: list[dict[str, str]] = []
    for step in plan:
        result = await _run(host, step)
        if step.title == "état de la VM":
            state = V.parse_domstate(result["stdout"])
        elif step.title == "ressources de la VM":
            info = parse_dominfo(result["stdout"])
        elif step.title == "processus du mineur dans la VM":
            processes = parse_ps(result["stdout"])
        elif step.title == "hashrate du mineur dans la VM":
            hashrate = V.parse_vm_hashrate(result["stdout"])
    source = ""
    if hashrate is not None:
        source = "API du mineur (VM)" if (profile or {}).get("api_port") else "journal du mineur (VM)"
    return {
        "simulated": False,
        "state": state,
        "hashrate_hs": hashrate,
        "hashrate_source": source,
        "processes": processes,
        "vcpus": info.get("vcpus"),
        "memory_kb": info.get("memory_kb"),
        "cpu_time_s": info.get("cpu_time_s"),
        "commands": [s.command() for s in plan],
    }


def parse_dominfo(text: str) -> dict[str, Any]:
    """``virsh dominfo`` → vCPU, mémoire maximale, temps CPU."""
    out: dict[str, Any] = {}
    for raw in (text or "").splitlines():
        key, _, value = raw.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in ("cpu(s)", "vcpus") and value.isdigit():
            out["vcpus"] = int(value)
        elif key.startswith("max memory") and value.split():
            if value.split()[0].isdigit():
                out["memory_kb"] = int(value.split()[0])
        elif key == "cpu time" and value:
            import re as _re

            match = _re.match(r"([0-9]+(?:\.[0-9]+)?)", value)
            if match:
                out["cpu_time_s"] = float(match.group(1))
    return out


def parse_ps(text: str) -> list[dict[str, str]]:
    """``ps -eo pid=,args=`` → processus retenus (chemin du mineur)."""
    found: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        pid, _, args = line.partition(" ")
        if pid.isdigit() and args.strip():
            found.append({"pid": pid, "args": args.strip()[:200]})
    return found
