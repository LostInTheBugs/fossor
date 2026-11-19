"""Pilote Proxmox des modes ``vm`` et ``hybrid`` (§4.6) — modèle et commandes.

Ce module est **pur** : aucune connexion, aucune horloge implicite (``now`` est
toujours passé en argument). Il produit des plans de commandes figées, exécutés
par :mod:`app.pve_remote`.

Choix d'accès
-------------
L'hôte Proxmox est joint en SSH avec le compte du préréglage ``os_kind=proxmox``
(racine par défaut, §4.1 ter) : les commandes ``qm``, ``pvesh`` et ``pvesm`` sont
donc lancées **directement**, sans ``sudo``. Aucun jeton d'API n'est utilisé à ce
stade.

Règles de sûreté
----------------
* le VMID est pris dans une plage réservée (défaut ``9900-9999``) et doit être
  libre sur le nœud **et** dans le cluster ;
* la propriété d'une VM est triple : VMID dans la plage, tag ``fossor``, et
  métadonnée ``fossor_vm=1`` dans la description — relus sur l'hôte ;
* la destruction exige en plus une sauvegarde de wallet vérifiée (§10.11) ;
* ``pct`` n'apparaît dans aucun plan : un conteneur n'est jamais touché ;
* aucun secret de wallet n'est manipulé ici.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from app.spec_vm import (
    DEFAULT_BACKUP_MAX_AGE_DAYS,
    VmError,
    VmStep,
    evaluate_backup,
    parse_backup_stat,
    parse_checksum_file,
    validate_vm_node,
)

FOSSOR_PREFIX = "fossor-"
PVE_TAG = "fossor"
OWNER_MARKER = "fossor_vm=1"
VM_USER = "specminer"
DEFAULT_VMID_MIN = 9900
DEFAULT_VMID_MAX = 9999
DEFAULT_BRIDGE = "vmbr0"
DEFAULT_DISK_STORAGE = "local-lvm"
DEFAULT_CI_STORAGE = "local"
PVE_TMP_DIR = "/var/tmp/fossor-pve"
IMAGE_DIR = f"{PVE_TMP_DIR}/images"
PUBKEY_PATH = f"{PVE_TMP_DIR}/fossor.pub"
KEY_PATH = "/root/.ssh/fossor-vm"
KNOWN_HOSTS = "/root/.ssh/known_hosts.fossor-vm"
CHECKSUM_ALGOS = ("sha256", "sha512")
GUEST_AGENT_PACKAGE = "qemu-guest-agent"
LOCAL_SNIPPET_DIR = "/var/lib/vz/snippets"
SNIPPET_PREFIX = "fossor-user"

_RE_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_RE_STORAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_RE_BRIDGE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,15}$")
_RE_URL = re.compile(r"^https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+$")
_RE_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(qcow2|img|raw)$")
_RE_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_RE_SHA512 = re.compile(r"^[a-f0-9]{128}$")
_RE_ABS_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
_RE_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_RE_IPV6_LL = re.compile(r"^[fF][eE]80:[0-9A-Fa-f:]{2,39}$")
_RE_IPV4 = re.compile(
    r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"
)
_RE_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]{20,}( [^\n]{0,80})?$")


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise VmError(message)


def _int(data: dict[str, Any], field: str, low: int, high: int, default: int) -> int:
    raw = data.get(field)
    try:
        value = int(default if raw is None or raw == "" else raw)
    except (TypeError, ValueError):
        raise VmError(f"{field} : nombre entier attendu")
    _require(low <= value <= high, f"{field} : {low} à {high}")
    return value


# ── Modèle de VM Proxmox ──────────────────────────────────────────────
def validate_pve_model(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise et valide le modèle Proxmox (Configuration → Spec mining)."""
    data = dict(data or {})
    try:
        host_id = int(data.get("host_id") or 0)
    except (TypeError, ValueError):
        raise VmError("machine hôte : identifiant numérique attendu")
    _require(host_id > 0, "machine hôte : obligatoire (une machine Proxmox marquée vm_host)")

    node = str(data.get("node") or "").strip()
    _require(bool(_RE_NODE.match(node)), "nœud Proxmox : nom du nœud attendu (pve)")

    disk_storage = str(data.get("disk_storage") or DEFAULT_DISK_STORAGE).strip()
    ci_storage = str(data.get("ci_storage") or DEFAULT_CI_STORAGE).strip()
    for label, value in (("stockage disque", disk_storage), ("stockage cloud-init", ci_storage)):
        _require(bool(_RE_STORAGE.match(value)), f"{label} : nom de stockage attendu")

    bridge = str(data.get("bridge") or DEFAULT_BRIDGE).strip()
    _require(bool(_RE_BRIDGE.match(bridge)), "pont réseau : nom de pont attendu (vmbr0)")

    vmid_min = _int(data, "vmid_min", 100, 999_999_999, DEFAULT_VMID_MIN)
    vmid_max = _int(data, "vmid_max", 100, 999_999_999, DEFAULT_VMID_MAX)
    _require(vmid_min < vmid_max, "plage de VMID : le minimum doit être inférieur au maximum")
    _require(vmid_max - vmid_min <= 1000, "plage de VMID : 1000 valeurs au plus")

    url = str(data.get("image_url") or "").strip()
    _require(bool(_RE_URL.match(url)), "image cloud : URL http(s) sans espace")
    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    _require(bool(_RE_IMAGE_NAME.match(name)), "image cloud : nom .qcow2/.img/.raw attendu")

    algo = str(data.get("checksum_algo") or "sha256").strip().lower()
    _require(algo in CHECKSUM_ALGOS, "somme de contrôle : sha256 ou sha512")
    checksum = str(data.get("checksum") or data.get("base_image_sha256") or "").strip().lower()
    checksum_url = str(data.get(
        "checksum_url") or data.get("checksum_file_url") or "").strip()
    if checksum_url:
        _require(bool(_RE_URL.match(checksum_url)),
                 "fichier de sommes : URL http(s) sans espace")
        # Un fichier SHA256SUMS Ubuntu ne porte que des SHA256.
        algo = "sha256"
    else:
        pattern = _RE_SHA512 if algo == "sha512" else _RE_SHA256
        expected_len = 128 if algo == "sha512" else 64
        _require(
            bool(pattern.match(checksum)),
            f"image cloud : somme {algo.upper()} obligatoire ({expected_len} hexadécimaux)",
        )

    node_port = _int(data, "node_port", 1024, 65535, 19333)
    backup = str(data.get("wallet_backup_path") or "/home/specminer/data/wallet.dat").strip()
    _require(bool(_RE_ABS_PATH.match(backup)), "sauvegarde du wallet : chemin absolu")
    _require(".." not in backup.split("/"), "sauvegarde du wallet : « .. » interdit")

    node_url = str(data.get("node_url") or "").strip()
    node_sha = str(data.get("node_sha256") or "").strip().lower()
    if node_url or node_sha:
        _require(bool(_RE_URL.match(node_url)), "nœud : URL http(s) de l'archive")
        _require(bool(_RE_SHA256.match(node_sha)), "nœud : SHA256 obligatoire")
    node_binary = str(data.get("node_binary") or "").strip().lstrip("./")
    if node_binary:
        _require(bool(_RE_ABS_PATH.match("/" + node_binary)), "nœud : chemin relatif simple")
        _require(".." not in node_binary.split("/"), "nœud : « .. » interdit")

    keep_cache = data.get("keep_image_cache")
    if isinstance(keep_cache, str):
        keep_cache = keep_cache.strip().lower() in ("1", "true", "yes", "on")
    keep_cache = bool(keep_cache)

    from app import spec_vm as SV

    return {
        "host_id": host_id,
        "keep_image_cache": keep_cache,
        "node": node,
        "disk_storage": disk_storage,
        "ci_storage": ci_storage,
        "bridge": bridge,
        "vmid_min": vmid_min,
        "vmid_max": vmid_max,
        "vcpus": _int(data, "vcpus", 1, 32, 2),
        "memory_mb": _int(data, "memory_mb", 256, 65536, 2048),
        "disk_gb": _int(data, "disk_gb", 2, 2048, 20),
        "image_url": url,
        "image_name": name,
        "checksum": checksum,
        "checksum_algo": algo,
        "checksum_url": checksum_url,
        "node_port": node_port,
        "wallet_backup_path": backup,
        "node_url": node_url,
        "node_sha256": node_sha,
        "node_binary": node_binary,
        "node_start_cmd": SV._validate_template(data.get("node_start_cmd"),
                                               "démarrage du nœud", ("datadir", "port", "coin")),
        "wallet_create_cmd": SV._validate_template(data.get("wallet_create_cmd"),
                                                   "création du wallet", ("datadir", "coin")),
    }


def image_path(model: dict[str, Any]) -> str:
    return f"{IMAGE_DIR}/{model['image_name']}"


def is_fossor_name(name: str) -> bool:
    return bool(name) and re.match(r"^fossor-[a-z0-9][a-z0-9-]{0,52}$", name or "") is not None


def validate_name(name: str) -> str:
    _require(is_fossor_name(name), f"nom de VM : préfixe « {FOSSOR_PREFIX} » attendu")
    return name


def description_text(name: str) -> str:
    """Description portée par la VM : la métadonnée de propriété, jamais un secret."""
    return f"{OWNER_MARKER} {name}"


# ── Analyse des sorties Proxmox ───────────────────────────────────────
def parse_pvesm_status(text: str) -> list[dict[str, Any]]:
    """``pvesm status`` → stockages avec leur espace (KiB)."""
    out: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        parts = raw.split()
        if len(parts) < 6 or parts[0] == "Name":
            continue
        if not parts[3].isdigit() or not parts[4].isdigit() or not parts[5].isdigit():
            continue
        total, used, avail = int(parts[3]), int(parts[4]), int(parts[5])
        out.append(
            {
                "name": parts[0],
                "type": parts[1],
                "status": parts[2],
                "total_kb": total,
                "used_kb": used,
                "avail_kb": avail,
                "avail_gb": round(avail / 1024 / 1024, 2),
                "used_pct": round(used * 100 / total, 2) if total else 0.0,
            }
        )
    return out


def parse_pve_storages(text: str) -> list[dict[str, Any]]:
    """``pvesh get /storage --output-format json`` → stockages et contenus acceptés."""
    try:
        payload = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("storage") or "").strip()
        if not name:
            continue
        content = [part.strip() for part in str(item.get("content") or "").split(",") if part.strip()]
        out.append(
            {
                "name": name,
                "type": str(item.get("type") or ""),
                "content": content,
                "images": "images" in content,
                "snippets": "snippets" in content,
            }
        )
    return out


def ci_storage_problem(
    storages: list[dict[str, Any]], name: str
) -> str:
    """Refus net quand le stockage cloud-init n'accepte pas ``snippets``.

    2026.11.008 : le snippet user-data est écrit dans ``<stockage>:snippets/``.
    Un stockage ``dir``/``lvm`` sans ``snippets`` dans ses contenus ne peut pas
    le recevoir : le modèle est refusé dès l'inspection, sans repli silencieux
    vers un autre dossier.  Un stockage **inconnu** de l'hôte ne déclenche rien
    (l'inspection ne l'a pas listé, ce n'est pas une preuve d'absence).
    """
    target = str(name or "").strip()
    if not target:
        return ""
    for item in storages or []:
        if item.get("name") != target:
            continue
        if item.get("snippets") or "snippets" in (item.get("content") or []):
            return ""
        accepted = ", ".join(item.get("content") or []) or "aucun"
        return (
            f"stockage cloud-init « {target} » sans snippets (contenus acceptés : "
            f"{accepted}) — choisissez un stockage qui accepte « snippets » : "
            "modèle refusé, aucun repli"
        )
    return ""


def eligible_storages(
    statuses: list[dict[str, Any]], storages: list[dict[str, Any]], need_gb: int
) -> list[dict[str, Any]]:
    """Stockages qui acceptent ``images``, sont actifs et ont la place demandée."""
    contents = {item["name"]: item for item in storages}
    out: list[dict[str, Any]] = []
    for status in statuses:
        meta = contents.get(status["name"])
        if meta is None or not meta.get("images"):
            continue
        if status.get("status") != "active":
            continue
        if float(status.get("avail_gb") or 0) < float(need_gb):
            continue
        out.append({**status, "content": meta.get("content") or [], "images": True})
    return out


def parse_qm_list(text: str) -> dict[int, dict[str, str]]:
    """``qm list`` → VMID → {name, status}."""
    out: dict[int, dict[str, str]] = {}
    for raw in (text or "").splitlines():
        parts = raw.split()
        if len(parts) < 3 or parts[0] == "VMID" or not parts[0].isdigit():
            continue
        out[int(parts[0])] = {"name": parts[1], "status": parts[2]}
    return out


def parse_cluster_resources(text: str) -> set[int]:
    """``pvesh get /cluster/resources --type vm`` → VMID déjà pris (tous types)."""
    try:
        payload = json.loads(text or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(payload, list):
        return set()
    taken: set[int] = set()
    for item in payload:
        if isinstance(item, dict) and str(item.get("vmid") or "").isdigit():
            taken.add(int(item["vmid"]))
    return taken


def choose_vmid(low: int, high: int, taken: set[int] | dict[int, Any]) -> int:
    """Premier VMID libre de la plage — refus explicite si la plage est pleine."""
    _require(int(low) < int(high), "plage de VMID : bornes incohérentes")
    used = {int(key) for key in taken}
    for candidate in range(int(low), int(high) + 1):
        if candidate not in used:
            return candidate
    raise VmError(f"plage de VMID {low}-{high} pleine : aucune VM libre")


def parse_qm_status(text: str) -> str:
    """``qm status`` → ``running`` / ``stopped`` / ``unknown``."""
    raw = (text or "").strip().splitlines()
    if not raw:
        return "unknown"
    match = re.search(r"status:\s*(\S+)", raw[0])
    return (match.group(1) if match else raw[0].split()[-1]).strip().lower()


def parse_qm_config(text: str) -> dict[str, Any]:
    """``qm config`` → champs utiles : nom, tags, description, MAC du net0."""
    out: dict[str, Any] = {"name": "", "tags": [], "description": "", "mac": "", "scsi0": ""}
    key = ""
    for raw in (text or "").splitlines():
        if key == "description" and raw.startswith((" ", "\t")):
            out["description"] += "\n" + raw.strip()
            continue
        field, sep, value = raw.partition(":")
        if not sep:
            continue
        field = field.strip().lower()
        value = value.strip()
        key = field
        if field == "name":
            out["name"] = value
        elif field == "tags":
            out["tags"] = [part.strip().lower() for part in value.split(";") if part.strip()]
        elif field == "description":
            out["description"] = value
        elif field == "scsi0":
            out["scsi0"] = value
        elif field in ("net0", "net1"):
            match = re.search(r"(?:virtio|e1000|vmxnet3)=([0-9A-Fa-f:]{17})", value)
            if match and not out["mac"]:
                out["mac"] = match.group(1).lower()
    return out


def parse_guest_ip(text: str) -> str:
    """``qm guest cmd <id> network-get-interfaces`` → première IPv4 utile."""
    try:
        payload = json.loads(text or "[]")
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, list):
        return ""
    for iface in payload:
        if not isinstance(iface, dict):
            continue
        if str(iface.get("name") or "") == "lo":
            continue
        for addr in iface.get("ip-addresses") or []:
            if not isinstance(addr, dict):
                continue
            if str(addr.get("ip-address-type") or "") != "ipv4":
                continue
            candidate = str(addr.get("ip-address") or "").strip()
            if not _RE_IPV4.match(candidate):
                continue
            if candidate.startswith(("127.", "169.254.")):
                continue
            return candidate
    return ""


def parse_ip_neigh(text: str, mac: str) -> str:
    """Table ARP de l'hôte (lecture seule) : MAC → IPv4."""
    wanted = (mac or "").strip().lower()
    if not _RE_MAC.match(wanted):
        return ""
    for raw in (text or "").splitlines():
        parts = raw.split()
        if not parts or not _RE_IPV4.match(parts[0]):
            continue
        lower = [part.lower() for part in parts]
        if "lladdr" in lower and lower.index("lladdr") + 1 < len(lower):
            if lower[lower.index("lladdr") + 1] == wanted:
                return parts[0]
    return ""


def parse_ipv6_neigh(text: str, mac: str) -> str:
    """Table de voisins **IPv6** de l'hôte : MAC → adresse lien-local.

    Relevé sur un hôte Proxmox juste après ``qm start`` (anonymisé) ::

        fe80::be24:11ff:fe00:0001 dev vmbr0 lladdr bc:24:11:00:00:01 router STALE

    C'est la seule trace d'une VM fraîche : aucun échange IPv4 n'a eu lieu,
    donc la table ARP est vide.
    """
    wanted = (mac or "").strip().lower()
    if not _RE_MAC.match(wanted):
        return ""
    for raw in (text or "").splitlines():
        parts = raw.split()
        if not parts or not _RE_IPV6_LL.match(parts[0]):
            continue
        lower = [part.lower() for part in parts]
        if "lladdr" in lower and lower.index("lladdr") + 1 < len(lower):
            if lower[lower.index("lladdr") + 1] == wanted:
                return parts[0].lower()
    return ""


def parse_addr_ipv4(text: str) -> str:
    """``ip -4 -o addr show scope global`` → première IPv4 de la VM.

    Relevé réel (anonymisé) ::

        2: ens18    inet 192.0.2.31/24 brd 192.0.2.255 scope global dynamic ens18\\       valid_lft 86390sec preferred_lft 86390sec
    """
    for match in re.finditer(r"\binet\s+([0-9]{1,3}(?:\.[0-9]{1,3}){3})/\d+", text or ""):
        candidate = match.group(1)
        if not _RE_IPV4.match(candidate):
            continue
        if candidate.startswith(("127.", "169.254.")):
            continue
        return candidate
    return ""


# ── Propriété et destruction ──────────────────────────────────────────
def in_range(vmid: int, low: int, high: int) -> bool:
    try:
        value = int(vmid)
    except (TypeError, ValueError):
        return False
    return int(low) <= value <= int(high)


def is_owned(vmid: int, low: int, high: int, tags: list[str] | str, description: str) -> bool:
    """Propriété = VMID dans la plage **et** tag ``fossor`` **et** métadonnée."""
    tag_list = tags if isinstance(tags, list) else [part.strip() for part in str(tags or "").split(";")]
    lowered = {str(tag).strip().lower() for tag in tag_list if str(tag).strip()}
    return (
        in_range(vmid, low, high)
        and PVE_TAG in lowered
        and OWNER_MARKER in (description or "")
    )


def ensure_owned(
    vmid: int, low: int, high: int, tags: list[str] | str, description: str
) -> None:
    """Refus explicite, nommant ce qui manque — avant toute commande."""
    if not in_range(vmid, low, high):
        raise VmError(
            f"VM {vmid} refusée : hors de la plage réservée {low}-{high} — "
            "Fossor ne touche aucune autre VM ni aucun conteneur."
        )
    tag_list = tags if isinstance(tags, list) else [part.strip() for part in str(tags or "").split(";")]
    lowered = {str(tag).strip().lower() for tag in tag_list if str(tag).strip()}
    if PVE_TAG not in lowered:
        raise VmError(
            f"VM {vmid} refusée : tag « {PVE_TAG} » absent — elle n'appartient pas à Fossor."
        )
    if OWNER_MARKER not in (description or ""):
        raise VmError(
            f"VM {vmid} refusée : métadonnée « {OWNER_MARKER} » absente de la description — "
            "Fossor ne touche jamais une VM qu'il n'a pas créée."
        )


def ensure_backup(
    backup: dict[str, Any] | None,
    now_epoch: int,
    max_age_days: int = DEFAULT_BACKUP_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Refus si la sauvegarde n'est pas vérifiée ou si le contrôle est périmé."""
    if not backup or not backup.get("verified"):
        reason = (backup or {}).get("reason") or "sauvegarde du wallet jamais vérifiée"
        raise VmError(
            f"Destruction refusée : {reason}. Lancez le contrôle de sauvegarde "
            "(§10.11) avant de détruire la VM."
        )
    checked = int(backup.get("checked_at_epoch") or 0)
    if checked and max(0, int(now_epoch) - checked) > max_age_days * 86400:
        raise VmError(
            "Destruction refusée : le dernier contrôle de sauvegarde est trop ancien — relancez-le."
        )
    return backup


def ensure_destroyable(
    vmid: int,
    low: int,
    high: int,
    tags: list[str] | str,
    description: str,
    backup: dict[str, Any] | None,
    now_epoch: int,
    max_age_days: int = DEFAULT_BACKUP_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Propriété puis sauvegarde vérifiée : deux refus, dans cet ordre."""
    ensure_owned(vmid, low, high, tags, description)
    return ensure_backup(backup, now_epoch, max_age_days)


# ── Plans de commandes ────────────────────────────────────────────────
def _sh(script: str, *args: str) -> tuple[str, ...]:
    return ("sh", "-c", script, "fossor", *args)


def userdata_yaml(name: str, ssh_public_key: str) -> str:
    """cloud-config de la VM : compte, clé Fossor et **agent invité**.

    ``--cicustom user=…`` remplace l'user-data que Proxmox génère : le compte,
    sa clé et l'installation de ``qemu-guest-agent`` doivent donc être écrits
    ici, sans quoi la VM n'a ni accès SSH ni agent (et son IP reste
    introuvable).
    """
    validate_name(name)
    key = (ssh_public_key or "").strip()
    _require(bool(_RE_KEY.match(key)) and "\n" not in key,
             "clé SSH de Fossor : ligne OpenSSH attendue")
    return "\n".join(
        [
            "#cloud-config",
            f"hostname: {name}",
            "manage_etc_hosts: true",
            "users:",
            f"  - name: {VM_USER}",
            "    shell: /bin/bash",
            "    sudo: ALL=(ALL) NOPASSWD:ALL",
            "    lock_passwd: true",
            "    ssh_authorized_keys:",
            f"      - {key}",
            "package_update: true",
            "packages:",
            f"  - {GUEST_AGENT_PACKAGE}",
            "runcmd:",
            f"  - systemctl enable --now {GUEST_AGENT_PACKAGE}",
            "",
        ]
    )


def snippet_name(vmid: int) -> str:
    return f"{SNIPPET_PREFIX}-{int(vmid)}.yaml"


def snippet_remove_plan(model: dict[str, Any], vmid: int) -> list[VmStep]:
    """Supprime le snippet cloud-init de la VM, même stockage que l'écriture.

    Le dossier est résolu par ``pvesh`` (champ ``path``) : jamais ``/var/lib/vz``
    en dur. Un fichier déjà absent n'échoue pas (``allow_fail``) : la
    destruction de la VM, elle, est déjà faite.
    """
    script = (
        's="$1"; f="$2"; b=""; '
        'b=$(pvesh get "/storage/$s" --output-format json 2>/dev/null '
        "| sed -n 's/.*\"path\":\"\\([^\"]*\\)\".*/\\1/p'); "
        '[ -n "$b" ] || b="' + LOCAL_SNIPPET_DIR.rsplit("/snippets", 1)[0] + '"; '
        'rm -f -- "$b/snippets/$f" && echo "SNIPPET_REMOVED:$b/snippets/$f"'
    )
    return [
        VmStep(
            "suppression du snippet cloud-init",
            _sh(script, model["ci_storage"], snippet_name(vmid)),
            allow_fail=True,
            timeout=60,
        )
    ]


def cicustom_value(model: dict[str, Any], vmid: int) -> str:
    """Référence ``<stockage>:snippets/<fichier>`` passée à ``qm set --cicustom``."""
    return f"{model['ci_storage']}:snippets/{snippet_name(vmid)}"


def userdata_plan(
    model: dict[str, Any], name: str, vmid: int, ssh_public_key: str
) -> list[VmStep]:
    """Écrit le snippet cloud-init dans ``snippets/`` du stockage choisi.

    Le dossier est résolu par ``pvesh`` (champ ``path`` du stockage) : un
    stockage ``dir`` autre que ``local`` n'a pas ses snippets sous
    ``/var/lib/vz``. Repli sur ``/var/lib/vz`` si ``pvesh`` ne répond pas.
    """
    script = (
        's="$1"; f="$2"; b=""; '
        'b=$(pvesh get "/storage/$s" --output-format json 2>/dev/null '
        "| sed -n 's/.*\"path\":\"\\([^\"]*\\)\".*/\\1/p'); "
        '[ -n "$b" ] || b="' + LOCAL_SNIPPET_DIR.rsplit("/snippets", 1)[0] + '"; '
        '[ -d "$b" ] || { echo "SNIPPET:stockage introuvable $s" >&2; exit 6; }; '
        'install -d -m 0755 "$b/snippets" || exit 6; '
        'cat > "$b/snippets/$f" && echo "SNIPPET:$b/snippets/$f"'
    )
    return [
        VmStep(
            "snippet cloud-init (agent invité, compte et clé Fossor)",
            _sh(script, model["ci_storage"], snippet_name(vmid)),
            stdin=userdata_yaml(name, ssh_public_key),
            timeout=60,
        )
    ]


_DNS_FAILURES = (
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "no address associated with hostname",
)


def image_error_is_dns(text: str) -> bool:
    """Vrai quand l'échec de téléchargement vient de la **résolution** sur l'hôte."""
    lower = (text or "").lower()
    return any(marker in lower for marker in _DNS_FAILURES)


def image_url_host(url: str) -> str:
    match = re.match(r"^https?://([^/:]+)", url or "")
    return match.group(1) if match else ""


def image_plan(model: dict[str, Any]) -> list[VmStep]:
    """Téléchargement de l'image cloud sur l'hôte, somme de contrôle vérifiée."""
    path = image_path(model)
    algo = model["checksum_algo"]
    script = (
        'p="$1"; u="$2"; s="$3"; a="$4"; su="$5"; n="$6"; '
        'if [ -s "$p" ] && [ -n "$s" ] && echo "$s  $p" | "${a}sum" -c - >/dev/null 2>&1; then '
        'echo "IMAGE:present $s"; exit 0; fi; '
        'if [ -z "$s" ]; then '
        'rm -f "$p.sums"; '
        'if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 300 -o "$p.sums" "$su"; '
        'elif command -v wget >/dev/null 2>&1; then wget -q -O "$p.sums" "$su"; '
        'else echo "SUMS:no-downloader" >&2; exit 3; fi || exit 4; '
        's=$(grep -E "[[:space:]]\\*?${n}$" "$p.sums" | head -1 | cut -d" " -f1); '
        'rm -f "$p.sums"; '
        '[ -n "$s" ] || { echo "SUMS:absent $n" >&2; exit 6; }; fi; '
        'rm -f "$p.part"; '
        'if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 1800 -o "$p.part" "$u"; '
        'elif command -v wget >/dev/null 2>&1; then wget -q -O "$p.part" "$u"; '
        'elif command -v python3 >/dev/null 2>&1; then '
        'python3 -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" '
        '"$u" "$p.part"; else echo "IMAGE:no-downloader" >&2; exit 3; fi || exit 4; '
        'echo "$s  $p.part" | "${a}sum" -c - || { rm -f "$p.part"; exit 5; }; '
        'mv "$p.part" "$p" && echo "IMAGE:downloaded $s"'
    )
    return [
        VmStep("répertoire temporaire du pilote Proxmox", ("install", "-d", "-m", "0700", PVE_TMP_DIR,
                                                          IMAGE_DIR)),
        VmStep(
            "image cloud (téléchargement et somme de contrôle vérifiée)",
            _sh(script, path, model["image_url"], model["checksum"], algo,
                model.get("checksum_url") or "", model["image_name"]),
            timeout=1800,
        ),
        VmStep("image cloud présente", ("test", "-s", path)),
    ]


def create_plan(
    model: dict[str, Any], name: str, vmid: int, ssh_public_key: str
) -> list[VmStep]:
    """Création complète : import du disque, cloud-init, démarrage."""
    validate_name(name)
    key = (ssh_public_key or "").strip()
    _require(bool(_RE_KEY.match(key)) and "\n" not in key, "clé SSH de Fossor : ligne OpenSSH attendue")
    _require(in_range(vmid, model["vmid_min"], model["vmid_max"]),
             f"VMID {vmid} hors de la plage {model['vmid_min']}-{model['vmid_max']}")
    steps = image_plan(model)
    steps += [
        VmStep("clé publique de la VM (cloud-init)", ("tee", PUBKEY_PATH), stdin=key + "\n"),
        *userdata_plan(model, name, vmid, key),
        VmStep(
            "création de la VM (qm create)",
            (
                "qm", "create", str(int(vmid)),
                "--name", name,
                "--memory", str(model["memory_mb"]),
                "--cores", str(model["vcpus"]),
                "--sockets", "1",
                "--scsihw", "virtio-scsi-single",
                "--ostype", "l26",
                "--agent", "enabled=1",
                "--tags", PVE_TAG,
                "--description", description_text(name),
                "--net0", f"virtio,bridge={model['bridge']}",
                "--serial0", "socket",
                "--vga", "serial0",
                "--onboot", "0",
            ),
            timeout=300,
        ),
        VmStep(
            "import du disque cloud dans le stockage choisi",
            (
                "qm", "set", str(int(vmid)),
                "--scsi0", f"{model['disk_storage']}:0,import-from={image_path(model)}",
                "--boot", "order=scsi0",
            ),
            timeout=900,
        ),
        VmStep(
            "redimensionnement du disque",
            ("qm", "resize", str(int(vmid)), "scsi0", f"{model['disk_gb']}G"),
            allow_fail=True,
            timeout=300,
        ),
        VmStep(
            "cloud-init (utilisateur specminer, clé Fossor, DHCP)",
            (
                "qm", "set", str(int(vmid)),
                "--ide2", f"{model['ci_storage']}:cloudinit",
                "--ciuser", VM_USER,
                "--sshkeys", PUBKEY_PATH,
                "--ipconfig0", "ip=dhcp",
                "--cicustom", f"user={cicustom_value(model, vmid)}",
            ),
            timeout=300,
        ),
        VmStep("démarrage de la VM", ("qm", "start", str(int(vmid))), timeout=300),
        VmStep("état de la VM", ("qm", "status", str(int(vmid))), allow_fail=True, expect="running"),
    ]
    return steps


PREVIEW_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPreviewKeyFossorPreviewKey0000000000 apercu@fossor"
)


def plan_preview(model: dict[str, Any], name: str, vmid: int) -> list[VmStep]:
    """Plan complet d'une VM de campagne : création, extinction, destruction.

    Aucune commande n'est exécutée : la simulation de campagne liste ce plan.
    """
    return (
        image_plan(model)
        + create_plan(model, name, vmid, PREVIEW_KEY)
        + shutdown_plan(vmid)
        + destroy_plan(vmid)
    )


def config_plan(vmid: int) -> list[VmStep]:
    return [VmStep("configuration de la VM (tags, description, MAC)", ("qm", "config", str(int(vmid))),
                   allow_fail=True)]


def status_plan(vmid: int) -> list[VmStep]:
    return [VmStep("état de la VM", ("qm", "status", str(int(vmid))), allow_fail=True)]


def ip_plan(vmid: int) -> list[VmStep]:
    """Agent invité d'abord ; la table ARP de l'hôte (lecture seule) en repli."""
    return [
        VmStep(
            "adresse de la VM (agent invité)",
            ("qm", "guest", "cmd", str(int(vmid)), "network-get-interfaces"),
            allow_fail=True,
            timeout=30,
        ),
        VmStep("adresse de la VM (table ARP de l'hôte)", ("ip", "neigh", "show"), allow_fail=True,
               timeout=30),
        VmStep("adresse de la VM (voisins IPv6 de l'hôte)", ("ip", "-6", "neigh", "show"),
               allow_fail=True, timeout=30),
    ]


def link_local_addr_step(link_local: str, bridge: str) -> list[VmStep]:
    """Clé d'hôte scannée depuis l'hôte, puis IPv4 relevée par SSH (lecture seule).

    Une adresse ``fe80::`` n'est joignable qu'avec son scope ``%<bridge>``. La
    clé d'hôte est épinglée par un scan lancé **depuis l'hôte Proxmox** avant
    tout envoi de secret, puis le SSH exige ``StrictHostKeyChecking=yes``.
    """
    _require(bool(_RE_IPV6_LL.match(link_local or "")), "adresse lien-local invalide")
    _require(bool(_RE_BRIDGE.match(bridge or "")), "interface de l'hôte (bridge) requise")
    scan = (
        'f="$1"; h="$2"; '
        'touch "$f" && chmod 600 "$f"; '
        'ssh-keyscan -T 5 -t ed25519 "$h" >>"$f" 2>/dev/null '
        '&& echo "HOSTKEY:ok $h"'
    )
    return [
        VmStep("clé d'hôte de la VM (scan depuis l'hôte)",
               _sh(scan, KNOWN_HOSTS, f"{link_local}%{bridge}"), allow_fail=True, timeout=60),
        VmStep("IPv4 de la VM par SSH sur son adresse lien-local",
               ssh_vm_argv(link_local, ("ip", "-4", "-o", "addr", "show", "scope", "global"),
                           bridge),
               allow_fail=True, timeout=60),
    ]


def shutdown_plan(vmid: int, timeout_s: int = 120) -> list[VmStep]:
    """Arrêt propre demandé au système invité, puis état."""
    return [
        VmStep(
            "arrêt de la VM (qm shutdown, conservée)",
            ("qm", "shutdown", str(int(vmid)), "--timeout", str(int(timeout_s))),
            allow_fail=True,
            timeout=int(timeout_s) + 60,
        ),
        VmStep("état après arrêt", ("qm", "status", str(int(vmid))), allow_fail=True),
    ]


def stop_plan(vmid: int) -> list[VmStep]:
    return [VmStep("arrêt forcé", ("qm", "stop", str(int(vmid))), allow_fail=True, timeout=120)]


def destroy_plan(vmid: int) -> list[VmStep]:
    """Destruction : le VMID a déjà été vérifié (plage, tag, métadonnée, sauvegarde)."""
    return [
        *stop_plan(vmid),
        VmStep(
            "destruction de la VM et de ses disques non référencés",
            ("qm", "destroy", str(int(vmid)), "--purge", "--destroy-unreferenced-disks", "1"),
            timeout=300,
        ),
    ]


def inspect_plan() -> list[VmStep]:
    """Lecture seule : version, stockages, VM prises (nœud et cluster)."""
    return [
        VmStep("version de Proxmox", ("pveversion",), allow_fail=True),
        VmStep("stockages et espace libre", ("pvesm", "status"), allow_fail=True),
        VmStep(
            "stockages et contenus acceptés",
            ("pvesh", "get", "/storage", "--output-format", "json"),
            allow_fail=True,
        ),
        VmStep("VM prises sur le cluster",
               ("pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"),
               allow_fail=True),
        VmStep("VM prises sur le nœud", ("qm", "list"), allow_fail=True),
    ]


def detect_plan() -> list[VmStep]:
    """Détection ``vm_host`` : ``pveversion`` **et** ``/dev/kvm``."""
    return [
        VmStep("version de Proxmox", ("pveversion",), allow_fail=True),
        VmStep("périphérique KVM",
               _sh('test -e /dev/kvm && echo KVM=1 || echo KVM=0; '
                   'command -v qm >/dev/null 2>&1 && echo QM=1; exit 0'),
               allow_fail=True),
    ]


def cleanup_plan(model: dict[str, Any] | None = None) -> list[VmStep]:
    """Ménage : image téléchargée et dossier temporaire — chemins fixes, jamais saisis."""
    return [VmStep("suppression du dossier temporaire du pilote", ("rm", "-rf", PVE_TMP_DIR))]


def parse_detect(text: str) -> dict[str, Any]:
    """Sortie de :func:`detect_plan` → version et capacité."""
    version = ""
    match = re.search(r"pve-manager/([0-9][^\s/]*)", text or "")
    if match:
        version = match.group(1)
    return {
        "hypervisor": "proxmox" if version else "",
        "version": version,
        "kvm": "KVM=1" in (text or ""),
        "qm": "QM=1" in (text or ""),
        "vm_host": bool(version and "KVM=1" in (text or "")),
    }


def ssh_vm_argv(
    ip: str, argv: list[str] | tuple[str, ...], bridge: str = ""
) -> tuple[str, ...]:
    """Commande passée à l'hôte : SSH vers la VM, clé Fossor, hôte épinglé.

    Une adresse IPv6 lien-local (``fe80::…``) exige ``-6`` et le scope
    ``%<bridge>`` : elle n'est joignable que par l'interface qui la porte.
    """
    target = (ip or "").strip()
    scope = ""
    if _RE_IPV6_LL.match(target):
        _require(bool(_RE_BRIDGE.match(bridge or "")),
                 "adresse lien-local : interface de l'hôte (bridge) requise")
        scope = f"%{bridge}"
    else:
        _require(bool(_RE_IPV4.match(target)), "adresse de la VM invalide")
    _require(bool(argv), "commande vide")
    return (
        "ssh", *(("-6",) if scope else ()), "-i", KEY_PATH,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=10",
        f"{VM_USER}@{target}{scope}",
        # Même réencodage que côté libvirt : ssh recolle ses arguments par une
        # espace, le shell distant les redécoupe. Sans ``shlex.join``, un
        # ``sh -c 'for t in a b; …'`` perd son argv (constat n°1).
        shlex.join(list(argv)),
    )


def vm_step(
    title: str, ip: str, argv: list[str] | tuple[str, ...], **kwargs: Any
) -> VmStep:
    bridge = kwargs.pop("bridge", "")
    return VmStep(title, ssh_vm_argv(ip, argv, bridge), **kwargs)


__all__ = [
    "plan_preview",
    "CHECKSUM_ALGOS", "DEFAULT_BRIDGE", "DEFAULT_CI_STORAGE", "DEFAULT_DISK_STORAGE",
    "DEFAULT_VMID_MAX", "DEFAULT_VMID_MIN", "IMAGE_DIR", "KEY_PATH", "KNOWN_HOSTS",
    "OWNER_MARKER", "PUBKEY_PATH", "PVE_TAG", "PVE_TMP_DIR", "VM_USER",
    "choose_vmid", "cleanup_plan", "config_plan", "create_plan", "description_text",
    "destroy_plan", "detect_plan", "eligible_storages", "ensure_destroyable",
    "ensure_owned", "image_path", "image_plan", "in_range", "inspect_plan", "ip_plan",
    "is_fossor_name", "is_owned", "parse_cluster_resources", "parse_detect",
    "parse_guest_ip", "parse_ip_neigh", "parse_pve_storages", "parse_pvesm_status",
    "parse_ipv6_neigh", "parse_addr_ipv4", "userdata_yaml", "userdata_plan",
    "cicustom_value", "snippet_name", "image_error_is_dns", "image_url_host",
    "link_local_addr_step", "GUEST_AGENT_PACKAGE", "LOCAL_SNIPPET_DIR",
    "parse_qm_config", "parse_qm_list", "parse_qm_status", "shutdown_plan",
    "status_plan", "stop_plan", "validate_name", "validate_pve_model", "vm_step",
    "evaluate_backup", "parse_backup_stat", "validate_vm_node", "VmStep", "VmError",
]
