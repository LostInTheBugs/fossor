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
import base64
import hashlib
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import pve_vm as P


class PveRemoteError(RuntimeError):
    """Action Proxmox refusée ou échouée — message sûr pour l'opérateur."""


Runner = Callable[..., Awaitable[dict[str, Any]]]
RUNNER: Runner | None = None

log = logging.getLogger("fossor.pve")

# Trois essais, attente croissante : le SSH d'une VM fraîche tombe quand
# cloud-init redémarre les services (constat n°1 du 5e essai).
SSH_ATTEMPTS = 3
SSH_RETRY_DELAYS = (2.0, 5.0)
RETRY_JOURNAL: list[dict[str, Any]] = []

_TRANSIENT_RE = re.compile(
    r"connection (?:lost|refused|closed|reset|timed out)|closed by remote host|"
    r"kex_exchange_identification|broken pipe|no route to host|"
    r"network is unreachable|connection attempt failed|\btimeout\b",
    re.I,
)


def is_transient(text: str) -> bool:
    """Incident de transport (VM en cours de démarrage), pas un refus définitif."""
    return bool(_TRANSIENT_RE.search(text or ""))


def drain_retries() -> list[dict[str, Any]]:
    """Vide le journal des essais : le lanceur de campagne les écrit au journal."""
    items = list(RETRY_JOURNAL)
    RETRY_JOURNAL.clear()
    return items


def cloud_init_step(ip: str) -> P.VmStep:
    """Attend la fin de cloud-init **avant** de lancer le mineur (borne 300 s)."""
    script = (
        "if command -v cloud-init >/dev/null 2>&1; then "
        "timeout 300 cloud-init status --wait >/dev/null 2>&1; "
        "cloud-init status 2>/dev/null | head -2; "
        "else echo CLOUD-INIT:absent; fi; exit 0"
    )
    return P.vm_step("fin de cloud-init dans la VM", ip, ["sh", "-c", script],
                     allow_fail=True, timeout=330)


async def _default_runner(host: Any, command: str, stdin: str | None, timeout: int) -> dict[str, Any]:
    from app.miner_remote import default_runner

    return await default_runner(host, command, stdin=stdin, timeout=timeout)


def _guard(step: P.VmStep) -> None:
    if step.argv and step.argv[0] == "pct":
        raise PveRemoteError("commande « pct » refusée : un conteneur n'est jamais touché par Fossor.")


async def _run(
    host: Any,
    step: P.VmStep,
    *,
    attempts: int = 1,
    delays: tuple[float, ...] | None = None,
) -> dict[str, Any]:
    """Exécute un pas ; ``attempts > 1`` rejoue les incidents de transport.

    Un refus définitif (mot de passe, hôte inconnu, commande refusée) n'est
    jamais rejoué : seuls « Connection lost/refused », « timeout » et
    compagnie sont transitoires, et chaque nouvel essai est journalisé.
    """
    _guard(step)
    runner = RUNNER or _default_runner
    delays = SSH_RETRY_DELAYS if delays is None else delays
    attempt = 0
    while True:
        attempt += 1
        result = await runner(host, step.command(), stdin=step.stdin, timeout=step.timeout)
        status = int(result.get("status", 0) or 0)
        detail = str(result.get("stderr") or result.get("stdout") or "").strip()
        broken = not result.get("ok", True) or (status == 255 and is_transient(detail))
        if broken and attempt < max(1, attempts) and is_transient(detail or "connection refused"):
            RETRY_JOURNAL.append(
                {"title": step.title, "attempt": attempt, "attempts": int(attempts),
                 "error": detail[:200], "command": step.command()[:200]}
            )
            log.warning("SSH instable (%s) : essai %s/%s — %s", step.title, attempt,
                        attempts, detail[:200])
            await asyncio.sleep(delays[min(attempt - 1, len(delays) - 1)])
            continue
        if not result.get("ok", True):
            raise PveRemoteError(
                f"{step.title} : connexion impossible — {result.get('stderr') or 'erreur SSH'}"
            )
        break
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


def _is_guest(step: P.VmStep) -> bool:
    """Pas exécuté **dans** la VM : SSH depuis l'hôte, donc sujet aux coupures."""
    return bool(step.argv) and step.argv[0] == "ssh"


async def _run_guest(host: Any, step: P.VmStep) -> dict[str, Any]:
    """Pas dans la VM : trois essais si c'est un SSH (plans idempotents)."""
    return await _run(host, step, attempts=SSH_ATTEMPTS if _is_guest(step) else 1)


def _simulated(plan: list[P.VmStep], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "simulated": True,
        "commands": [step.command() for step in plan],
        "note": "Simulation : aucune commande n'est exécutée.",
    }
    if extra:
        payload.update(extra)
    return payload


PUSH_CHUNK = 3 * 1024 * 1024


def local_image_path(model: dict[str, Any]) -> Path:
    """Cache local de l'image, côté Fossor (jamais dans un dossier partagé)."""
    base = Path(os.environ.get("FOSSOR_DATA_DIR") or "/data") / "cache" / "pve-images"
    return base / P.image_path(model).rsplit("/", 1)[-1]


def _download_blocking(model: dict[str, Any], dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(model["image_url"], timeout=1800) as response, part.open("wb") as fh:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            fh.write(block)
    digest = hashlib.new(model["checksum_algo"])
    with part.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest().lower() != model["checksum"].lower():
        part.unlink(missing_ok=True)
        raise PveRemoteError(
            f"image {model['image_url']} : somme {model['checksum_algo']} locale différente "
            f"({digest.hexdigest()})"
        )
    part.replace(dest)
    return str(dest)


async def download_image(model: dict[str, Any]) -> str:
    """Récupère l'image **côté Fossor**, somme vérifiée localement."""
    return await asyncio.to_thread(_download_blocking, model, local_image_path(model))


async def push_image(host: Any, model: dict[str, Any], path: str) -> dict[str, Any]:
    """Copie l'image vers l'hôte par le canal SSH, somme vérifiée des deux côtés.

    Utilisé seulement quand l'hôte ne résout pas le nom de l'image : le contenu
    est encodé en base64 fragment par fragment, puis l'hôte recalcule la somme
    demandée par le modèle avant tout ``qm set``.
    """
    dest = P.image_path(model)
    algo = model["checksum_algo"]
    await _run(
        host,
        P.VmStep(
            "copie de l'image : fichier cible vidé",
            ("sh", "-c",
             'p="$1"; install -d -m 0700 "$(dirname "$p")" && rm -f "$p" && echo "PUSH:init $p"',
             "fossor", dest),
            timeout=60,
        ),
    )
    sent = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(PUSH_CHUNK)
            if not chunk:
                break
            await _run(
                host,
                P.VmStep(
                    "copie de l'image (fragment)",
                    ("sh", "-c", 'p="$1"; base64 -d >> "$p" && echo "PUSH:chunk"', "fossor", dest),
                    stdin=base64.b64encode(chunk).decode("ascii"),
                    timeout=600,
                ),
            )
            sent += len(chunk)
    check = await _run(
        host,
        P.VmStep(
            "somme de contrôle de l'image reçue",
            ("sh", "-c", 'p="$1"; s="$2"; a="$3"; echo "$s  $p" | "${a}sum" -c -',
             "fossor", dest, model["checksum"], algo),
            allow_fail=True,
            timeout=600,
        ),
    )
    if "OK" not in (check["stdout"] or ""):
        raise PveRemoteError(f"copie de l'image : somme {algo} non confirmée par l'hôte")
    return {"path": dest, "bytes": sent, "checksum": model["checksum"], "algo": algo,
            "verified": True}


def install_key_plan(private_key: str) -> list[P.VmStep]:
    """Clé privée de rebond dans :data:`P.PVE_TMP_DIR` (0400) — jamais dans un argv.

    Le dossier est créé explicitement : la création d'une VM l'installe avant
    les pas d'image, et une reprise après incident doit retrouver 0700.
    """
    if not (private_key or "").strip():
        raise PveRemoteError("clé SSH des VM absente : générez-la avant de créer une VM")
    return [
        P.VmStep("dossier de travail du pilote (0700)",
                 ("install", "-d", "-m", "0700", P.PVE_TMP_DIR, P.IMAGE_DIR)),
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
        problem = P.ci_storage_problem(storages, model.get("ci_storage") or "")
        if problem:
            raise PveRemoteError(problem)
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
    image_source = "téléchargée par l'hôte"
    note = ""
    download_index = len(key_steps) + 1
    for index, step in enumerate(key_steps + plan):
        try:
            await _run(host, step)
        except PveRemoteError as exc:
            if index != download_index or not P.image_error_is_dns(str(exc)):
                raise
            local = await download_image(model)
            pushed = await push_image(host, model, local)
            image_source = "envoyée par Fossor"
            note = (
                f"l'hôte ne résout pas {P.image_url_host(model['image_url'])} : "
                f"image envoyée par Fossor ({pushed['bytes']} octets, "
                f"{pushed['algo']} vérifiée sur l'hôte)"
            )
        commands.append(step.command())
    cache_kept = bool(model.get("keep_image_cache"))
    if not cache_kept:
        # Cache désactivé : l'image ne reste pas sur l'hôte après la création.
        # Seul l'image part — la clé de rebond sert au provisionnement et au
        # démarrage du mineur, elle ne disparaît qu'à la dernière VM.
        await _run(host, P.image_cleanup_plan(model)[0])
        commands.append(P.image_cleanup_plan(model)[0].command())
    return {
        "simulated": False,
        "name": name,
        "vmid": int(vmid),
        "state": "running",
        "image": P.image_path(model),
        "image_source": image_source,
        "cache_kept": cache_kept,
        "note": note,
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
    host: Any,
    vmid: int,
    *,
    simulated: bool,
    attempts: int = 24,
    pause: float = 5.0,
    bridge: str = P.DEFAULT_BRIDGE,
) -> dict[str, Any]:
    """Agent invité, ARP IPv4, puis IPv6 lien-local → ``ip -4 addr`` par SSH.

    Tout est en lecture seule sur l'hôte ; le SSH vers la VM ne lit que ses
    adresses. Une VM fraîche n'a souvent que son adresse lien-local dans la
    table de voisins, l'ARP IPv4 étant vide faute d'échange.
    """
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
        if mac and bridge:
            neighbours = await _run(host, plan[2])
            link_local = P.parse_ipv6_neigh(neighbours["stdout"], mac)
            if link_local:
                scan, addr = P.link_local_addr_step(link_local, bridge)
                await _run(host, scan)
                ip = P.parse_addr_ipv4((await _run(host, addr))["stdout"])
                if ip:
                    return {
                        "simulated": False,
                        "ip": ip,
                        "mac": mac,
                        "link_local": link_local,
                        "source": "SSH sur adresse lien-local",
                    }
        await asyncio.sleep(pause)
    raise PveRemoteError(
        f"La VM {vmid} n'a pas obtenu d'adresse IP (agent invité muet, MAC "
        f"{mac or 'inconnue'} absente de l'ARP IPv4 et des voisins IPv6)."
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
    # La VM emporte son snippet cloud-init ``fossor-user-<vmid>.yaml`` : le
    # laisser dans ``snippets/`` est un résidu du test réel (voir RAPPORT).
    keep_cache = bool(model.get("keep_image_cache"))
    plan = P.destroy_plan(vmid) + P.snippet_remove_plan(model, vmid)
    if simulated:
        return _simulated(plan + P.last_vm_cleanup_plan([], keep_cache),
                          {"destroyed": False})
    commands: list[str] = []
    for step in plan:
        await _run(host, step)
        commands.append(step.command())
    # Dernière VM Fossor détruite : la clé de rebond et son known_hosts
    # quittent l'hôte (constat n°1 du 5e essai). Il en reste une ? On garde.
    remaining = await fossor_guest_names(host)
    for step in P.last_vm_cleanup_plan(remaining, keep_cache):
        await _run(host, step)
        commands.append(step.command())
    return {"simulated": False, "destroyed": True, "last_vm": not remaining,
            "remaining": remaining, "cache_kept": keep_cache, "commands": commands}


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


async def fossor_guest_names(host: Any) -> list[str]:
    """Noms des VM Fossor encore présentes (``qm list``), lecture seule."""
    try:
        listing = await list_guests(host, simulated=False)
    except Exception as exc:  # hôte injoignable : on ne conclut pas « dernière »
        log.warning("qm list indisponible après destruction : %s", exc)
        return []
    return [item["name"] for item in listing.get("vms") or []
            if P.is_fossor_name(str(item.get("name") or ""))]


async def cleanup(host: Any, *, simulated: bool) -> dict[str, Any]:
    plan = P.cleanup_plan()
    if simulated:
        return _simulated(plan, {"cleaned": False})
    for step in plan:
        await _run(host, step)
    return {"simulated": False, "cleaned": True, "commands": [step.command() for step in plan]}


# ── Actions **dans** la VM (mêmes plans que libvirt, SSH de l'hôte racine) ──
def _vm_ip(ip: str) -> str:
    import re

    target = (ip or "").strip()
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", target):
        raise PveRemoteError("adresse de la VM invalide")
    return target


def _host_key_line(stdout: str, ip: str) -> str:
    for raw in (stdout or "").splitlines():
        text = raw.strip()
        if text.startswith("#") or not text:
            continue
        parts = text.split()
        if len(parts) >= 3 and parts[1].startswith(("ssh-", "ecdsa-")):
            return f"{ip} {parts[1]} {parts[2]}"
    return ""


async def pin_host_key(host: Any, ip: str, *, simulated: bool) -> dict[str, Any]:
    """``ssh-keyscan`` **depuis l'hôte**, puis épinglage (aucun ``sudo``)."""
    target = ip if simulated else _vm_ip(ip)
    plan = [
        P.VmStep(
            "clé d'hôte de la VM (ssh-keyscan depuis l'hôte)",
            ("ssh-keyscan", "-T", "10", "-t", "ed25519", target),
            allow_fail=True,
            timeout=60,
        )
    ]
    if simulated:
        return _simulated(plan, {"host_key": ""})
    result = await _run(host, plan[0], attempts=SSH_ATTEMPTS)
    line = _host_key_line(result["stdout"], target)
    if not line:
        raise PveRemoteError("Clé d'hôte de la VM introuvable (ssh-keyscan vide).")
    pin = P.VmStep("épinglage de la clé d'hôte de la VM", ("tee", "-a", P.KNOWN_HOSTS),
                   stdin=line + "\n")
    await _run(host, pin)
    return {"simulated": False, "host_key": line, "commands": [plan[0].command(), pin.command()]}


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
    """Nœud, wallet et mineur dans la VM — plan de :mod:`app.spec_vm`, SSH Proxmox."""
    from app import spec_vm as V

    pushed = False
    if str(profile.get("binary_source") or "url") == "fossor":
        if not simulated:
            await push_guest_binary(host, ip, profile, bridge=str(model.get("bridge") or ""))
        pushed = True
    plan = V.provision_plan(model, profile, ip, coin=coin, step_maker=P.vm_step,
                            binary_pushed=pushed)
    if simulated:
        return _simulated(plan, {"address": expected_address, "verified": False})
    from app.vm_remote import _address_from_stdout

    commands: list[str] = []
    address = expected_address
    for step in plan:
        result = await _run_guest(host, step)
        commands.append(step.command())
        if step.title.startswith("création du wallet"):
            found = _address_from_stdout(result["stdout"])
            if found and expected_address and found != expected_address:
                raise PveRemoteError(
                    "Le wallet créé dans la VM ne correspond pas à l'adresse du profil — "
                    "campagne refusée."
                )
            if found:
                address = found
    return {"simulated": False, "address": address, "verified": bool(address),
            "commands": commands}


async def start_miner(host: Any, profile: dict[str, Any], argv: list[str], ip: str, *,
                      simulated: bool) -> dict[str, Any]:
    from app import spec_vm as V

    plan = V.vm_miner_start_plan(profile, argv, ip, step_maker=P.vm_step)
    if simulated:
        return _simulated(plan)
    # 1. cloud-init doit avoir fini : sinon SSH tombe pendant les redémarrages
    #    de services et le mineur n'est jamais lancé (constat n°1).
    wait = cloud_init_step(ip)
    await _run_guest(host, wait)
    # 2. Lancement, trois essais sur incident de transport.
    result = await _run_guest(host, plan[0])
    return {"simulated": False, "pid": (result["stdout"] or "").strip()[:12],
            "commands": [wait.command(), plan[0].command()]}


async def stop_miner(host: Any, profile: dict[str, Any], ip: str, *,
                     simulated: bool) -> dict[str, Any]:
    from app import spec_vm as V

    plan = V.vm_miner_stop_plan(profile, ip, step_maker=P.vm_step)
    if simulated:
        return _simulated(plan)
    for step in plan:
        await _run_guest(host, step)
    return {"simulated": False, "stopped": True, "commands": [s.command() for s in plan]}


async def check_binary(host: Any, ip: str, profile: dict[str, Any], *,
                       simulated: bool, bridge: str = "") -> dict[str, Any]:
    from app import spec_vm as V

    plan = V.binary_probe_plan(ip, profile, step_maker=P.vm_step)
    if simulated:
        return _simulated(plan, {"ok": True, "message": ""})
    result = await _run_guest(host, plan[0])
    verdict = V.parse_binary_probe(result["stdout"] + "\n" + result["stderr"])
    verdict["commands"] = [plan[0].command()]
    return verdict


async def check_alive(host: Any, ip: str, profile: dict[str, Any], *,
                      simulated: bool, bridge: str = "") -> dict[str, Any]:
    from app import spec_vm as V

    plan = V.alive_plan(ip, profile, step_maker=P.vm_step)
    if simulated:
        return _simulated(plan, {"alive": True, "processes": []})
    result = await _run(host, plan[0])
    verdict = V.parse_alive(result["stdout"], profile)
    verdict["commands"] = [plan[0].command()]
    return verdict


async def miner_log_tail(host: Any, ip: str, profile: dict[str, Any], *,
                         simulated: bool, bridge: str = "") -> dict[str, Any]:
    from app import spec_vm as V

    plan = V.miner_log_tail_plan(ip, profile, step_maker=P.vm_step)
    if simulated:
        return _simulated(plan, {"tail": ""})
    result = await _run(host, plan[0])
    return {"tail": result["stdout"][-4000:], "commands": [plan[0].command()]}


async def collect(host: Any, vmid: int, ip: str, profile: dict[str, Any] | None, *,
                  simulated: bool) -> dict[str, Any]:
    """``qm status`` sur l'hôte, puis ``ps``/API **dans** la VM."""
    from app import spec_vm as V

    plan = P.status_plan(vmid)
    if ip:
        plan += V.guest_collect_plan(ip, profile, step_maker=P.vm_step)
    if simulated:
        return _simulated(plan, {"state": "", "hashrate_hs": None, "processes": []})
    from app.miner_remote import parse_hashrate
    from app.vm_remote import parse_ps

    state = ""
    hashrate: float | None = None
    processes: list[dict[str, str]] = []
    for step in plan:
        result = await _run_guest(host, step)
        if step.title == "état de la VM":
            state = P.parse_qm_status(result["stdout"])
        elif step.title == "processus du mineur dans la VM":
            processes = parse_ps(result["stdout"])
        elif step.title == "hashrate du mineur dans la VM":
            from app.spec_vm import parse_vm_hashrate

            hashrate = parse_vm_hashrate(result["stdout"])
    source = ""
    if hashrate is not None:
        source = "API du mineur (VM)" if (profile or {}).get("api_port") else "journal du mineur (VM)"
    return {"simulated": False, "state": state, "hashrate_hs": hashrate,
            "hashrate_source": source, "processes": processes,
            "commands": [s.command() for s in plan]}


async def check_backup(host: Any, ip: str, path: str, *, now_epoch: int,
                       max_age_days: int, simulated: bool,
                       wallet_mode: str = "file") -> dict[str, Any]:
    """§10.11 : taille et date seulement, jamais le contenu du wallet."""
    import posixpath

    from app import spec_vm as V

    if wallet_mode == "pool_address":
        plan = V.pool_wallet_check_plan(ip, posixpath.dirname(path) or V.DATA_DIR,
                                        step_maker=P.vm_step)
    else:
        plan = V.backup_check_plan(ip, path, step_maker=P.vm_step)
    if simulated:
        return _simulated(plan, {"verified": False, "reason": "simulation"})
    result = await _run_guest(host, plan[0])
    if wallet_mode == "pool_address":
        verdict = V.evaluate_pool_wallet(V.parse_dir_listing(result["stdout"]), now_epoch)
    else:
        stat = V.parse_backup_stat(result["stdout"])
        verdict = V.evaluate_backup(stat, now_epoch, max_age_days)
    verdict["commands"] = [plan[0].command()]
    return verdict


async def cache_info(host: Any, model: dict[str, Any], *, simulated: bool) -> dict[str, Any]:
    """Taille et contenu du dossier de travail du pilote (lecture seule)."""
    script = (
        'p="$1"; if [ -d "$p" ]; then '
        'echo "SIZE:$(du -sk "$p" 2>/dev/null | cut -f1)"; '
        'find "$p" -type f | while read -r f; do echo "FILE:$f:$(stat -c %s "$f")"; done; '
        'else echo SIZE:0; fi'
    )
    plan = [P.VmStep("cache du pilote Proxmox", ("sh", "-c", script, "fossor", P.PVE_TMP_DIR),
                     allow_fail=True)]
    if simulated:
        return {"simulated": True, "path": P.PVE_TMP_DIR, "bytes": 0, "files": [],
                "keep_image_cache": bool(model.get("keep_image_cache"))}
    result = await _run(host, plan[0])
    size_kb = 0
    files: list[dict[str, Any]] = []
    for raw in (result["stdout"] or "").splitlines():
        line = raw.strip()
        if line.startswith("SIZE:") and line[5:].isdigit():
            size_kb = int(line[5:])
        elif line.startswith("FILE:"):
            _, path, size = line.split(":", 2)
            files.append({"name": path.rsplit("/", 1)[-1],
                          "size": int(size) if size.isdigit() else 0})
    return {"simulated": False, "path": P.PVE_TMP_DIR, "bytes": size_kb * 1024,
            "files": files, "keep_image_cache": bool(model.get("keep_image_cache")),
            "commands": [plan[0].command()]}


async def clear_cache(host: Any, *, simulated: bool) -> dict[str, Any]:
    plan = P.cleanup_plan()
    if simulated:
        return _simulated(plan, {"cleared": False})
    for step in plan:
        await _run(host, step)
    return {"simulated": False, "cleared": True, "path": P.PVE_TMP_DIR,
            "commands": [step.command() for step in plan]}


async def push_guest_binary(host: Any, ip: str, profile: dict[str, Any], *,
                            bridge: str = "") -> dict[str, Any]:
    """Archive du mineur poussée dans la VM — même canal que l'image.

    La somme est vérifiée côté Fossor **avant** l'envoi, puis recalculée dans la
    VM : sans ces deux verdicts, le provisionnement refuse de continuer.
    """
    from app import binaries as B
    from app import spec_vm as V

    path, digest = B.verified_path(profile)
    kind = str(profile.get("binary_kind") or "tar.gz").strip().lower()
    dest = f"{V.INSTALL_DIR}/miner.{V.BINARY_SUFFIXES.get(kind, 'tar.gz')}"
    await _run(host, P.vm_step(
        "copie du binaire : cible vidée", ip,
        ["sh", "-c", 'p="$1"; install -d -m 0700 "$(dirname "$p")" && rm -f "$p"', "fossor", dest],
        bridge=bridge))
    sent = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(PUSH_CHUNK)
            if not chunk:
                break
            await _run(host, P.vm_step(
                "copie du binaire (fragment)", ip,
                ["sh", "-c", 'p="$1"; base64 -d >> "$p"', "fossor", dest],
                bridge=bridge, stdin=base64.b64encode(chunk).decode("ascii"), timeout=600))
            sent += len(chunk)
    check = await _run(host, P.vm_step(
        "somme du binaire reçu dans la VM", ip,
        ["sh", "-c", 'echo "$2  $1" | sha256sum -c -', "fossor", dest, digest],
        bridge=bridge, allow_fail=True, timeout=600))
    if "OK" not in (check["stdout"] or ""):
        raise PveRemoteError("copie du binaire : SHA256 non confirmé dans la VM — exécution refusée")
    return {"verified": True, "sha256": digest, "bytes": sent, "path": dest}


def backup_verdict(stat: dict[str, Any], now_epoch: int | None = None,
                   max_age_days: int = P.DEFAULT_BACKUP_MAX_AGE_DAYS) -> dict[str, Any]:
    """Verdict §10.11 sur un ``stat`` déjà relevé (aucune lecture du fichier)."""
    return P.evaluate_backup(stat, int(time.time()) if now_epoch is None else now_epoch, max_age_days)
