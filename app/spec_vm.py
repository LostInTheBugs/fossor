"""Modes ``vm`` et ``hybrid`` (§4.6) — modèle de VM jetable et commandes libvirt.

Ce module est **pur** : aucune connexion, aucun accès disque, aucune horloge
implicite (``now`` est toujours passé en argument). Il produit des plans de
commandes figées, exécutés par :mod:`app.vm_remote`.

Règles de sûreté
----------------
* tout ce qui vient de l'opérateur est validé ici (URL, SHA256 **obligatoire**,
  nom de VM, port du nœud, gabarits sans métacaractère de shell) ;
* une VM n'est touchée que si son nom porte le préfixe ``fossor-`` **et** si sa
  description porte la métadonnée ``fossor_vm=1`` — les specbox de Fred, qui
  n'ont ni l'un ni l'autre, sont donc hors d'atteinte ;
* la destruction exige une sauvegarde de wallet vérifiée (§10.11) : fichier
  présent, non vide, récent — jamais lu, seulement ``stat`` ;
* aucun secret de wallet (seed, mot de passe) n'apparaît ici : seule l'adresse
  publique est manipulée.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

FOSSOR_PREFIX = "fossor-"
OWNER_MARKER = "fossor_vm=1"
VM_USER = "specminer"
VM_HOME = f"/home/{VM_USER}"
IMAGE_ROOT = "/var/lib/libvirt/images/fossor"
KEY_PATH = "/root/.ssh/fossor-vm"
KNOWN_HOSTS = "/root/.ssh/known_hosts.fossor-vm"
HYPERVISOR = "qemu:///system"

SPEC_MODES = ("vm", "hybrid", "host")
DEFAULT_SPEC_MODE = "vm"
MODE_LABELS = {
    "vm": "VM jetable (nœud, wallet et mineur dans la VM)",
    "hybrid": "Hybride (nœud et wallet dans la VM, mineur sur l'hôte)",
    "host": "Hôte (mineur directement sur la machine)",
}
DEFAULT_NODE_PORT = 19333
DEFAULT_BACKUP_MAX_AGE_DAYS = 7

INSTALL_DIR = f"{VM_HOME}/opt"
LOG_DIR = f"{VM_HOME}/logs"
RUN_DIR = f"{VM_HOME}/run"
DATA_DIR = f"{VM_HOME}/data"

_RE_URL = re.compile(r"^https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+$")
_RE_SHA = re.compile(r"^[a-f0-9]{64}$")
_RE_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(qcow2|img|raw)$")
_RE_TOKEN = re.compile(r"^[a-f0-9]{4,12}$")
_RE_NET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_RE_ABS_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
_RE_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]{20,}( [^\n]{0,80})?$")
_RE_COIN = re.compile(r"^[A-Za-z0-9]{2,20}$")
_RE_VAR = re.compile(r"\{([a-z_]+)\}")
_RE_IPV4 = re.compile(
    r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"
)
_FORBIDDEN = (";", "|", "&", "`", "$", "\n", "\r", "<", ">", "\\", "$(")


class VmError(ValueError):
    """Modèle ou action de VM refusés — message sûr à afficher."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise VmError(message)


@dataclass(frozen=True)
class VmStep:
    """Une commande du plan, en argv (jamais une chaîne donnée à un shell)."""

    title: str
    argv: tuple[str, ...]
    stdin: str | None = None
    allow_fail: bool = False
    timeout: int = 180
    expect: str | None = None

    def command(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True)
class VmInfo:
    """Ce qu'on sait d'une VM listée sur l'hôte."""

    name: str
    description: str
    owned: bool


# ── Modèle de VM ──────────────────────────────────────────────────────
def validate_vm_model(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise et valide le modèle de VM jetable (Configuration)."""
    data = dict(data or {})
    try:
        host_id = int(data.get("host_id") or 0)
    except (TypeError, ValueError):
        raise VmError("machine hôte : identifiant numérique attendu")
    _require(host_id > 0, "machine hôte : obligatoire (une machine marquée vm_host)")

    url = str(data.get("base_image_url") or "").strip()
    _require(bool(_RE_URL.match(url)), "image de base : URL http(s) sans espace")
    sha = str(data.get("base_image_sha256") or "").strip().lower()
    _require(bool(_RE_SHA.match(sha)), "image de base : SHA256 obligatoire (64 hexadécimaux)")

    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    _require(bool(_RE_IMAGE_NAME.match(name)), "image de base : nom de fichier .qcow2/.img/.raw attendu")

    def _int(field: str, low: int, high: int, default: int) -> int:
        raw = data.get(field)
        try:
            value = int(default if raw is None else raw)
        except (TypeError, ValueError):
            raise VmError(f"{field} : nombre entier attendu")
        _require(low <= value <= high, f"{field} : {low} à {high}")
        return value

    network = str(data.get("network") or "default").strip()
    _require(bool(_RE_NET.match(network)), "réseau : nom libvirt (lettres, chiffres, tiret)")

    backup = str(data.get("wallet_backup_path") or f"{DATA_DIR}/wallet.dat").strip()
    _require(bool(_RE_ABS_PATH.match(backup)), "sauvegarde du wallet : chemin absolu")
    _require(".." not in backup.split("/"), "sauvegarde du wallet : « .. » interdit")

    node_url = str(data.get("node_url") or "").strip()
    node_sha = str(data.get("node_sha256") or "").strip().lower()
    if node_url or node_sha:
        _require(bool(_RE_URL.match(node_url)), "nœud : URL http(s) de l'archive")
        _require(bool(_RE_SHA.match(node_sha)), "nœud : SHA256 obligatoire")
    node_binary = str(data.get("node_binary") or "").strip().lstrip("./")
    if node_binary:
        _require(bool(_RE_ABS_PATH.match("/" + node_binary)), "nœud : chemin relatif simple")
        _require(".." not in node_binary.split("/"), "nœud : « .. » interdit")

    return {
        "host_id": host_id,
        "base_image_url": url,
        "base_image_sha256": sha,
        "base_image_name": name,
        "vcpus": _int("vcpus", 1, 32, 2),
        "memory_mb": _int("memory_mb", 256, 65536, 2048),
        "disk_gb": _int("disk_gb", 2, 2048, 20),
        "network": network,
        "node_port": _int("node_port", 1024, 65535, DEFAULT_NODE_PORT),
        "wallet_backup_path": backup,
        "node_url": node_url,
        "node_sha256": node_sha,
        "node_binary": node_binary,
        "node_start_cmd": _validate_template(data.get("node_start_cmd"), "démarrage du nœud",
                                            ("datadir", "port", "coin")),
        "wallet_create_cmd": _validate_template(data.get("wallet_create_cmd"),
                                               "création du wallet", ("datadir", "coin")),
    }


def _validate_template(raw: Any, label: str, allowed: tuple[str, ...]) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for char in _FORBIDDEN:
        _require(char not in text, f"{label} : caractère interdit {char!r}")
    for var in _RE_VAR.findall(text):
        _require(var in allowed, f"{label} : variable inconnue {{{var}}}")
    stripped = _RE_VAR.sub("", text)
    _require("{" not in stripped and "}" not in stripped, f"{label} : accolade orpheline")
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise VmError(f"{label} : gabarit illisible ({exc})")
    _require(bool(tokens), f"{label} : gabarit vide")
    return text


def spec_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in SPEC_MODES else DEFAULT_SPEC_MODE


def normalize_mode(value: Any, default: str = DEFAULT_SPEC_MODE) -> str:
    """Mode demandé (campagne) ou repli explicite ; refuse tout le reste."""
    mode = str(value or "").strip().lower()
    if not mode:
        return spec_mode(default)
    _require(mode in SPEC_MODES, "mode : vm, hybrid ou host")
    return mode


def render_template(text: str, values: dict[str, str]) -> list[str]:
    """Rend un gabarit validé en argv — mêmes garde-fous que les profils."""
    tokens = shlex.split(text)
    out: list[str] = []
    for token in tokens:
        def _sub(match: re.Match[str]) -> str:
            var = match.group(1)
            _require(var in values, f"valeur manquante pour {{{var}}}")
            value = str(values[var])
            _require(not _FORBIDDEN_MATCH(value), f"valeur refusée pour {{{var}}}")
            return value

        out.append(_RE_VAR.sub(_sub, token))
    return out


def _FORBIDDEN_MATCH(value: str) -> bool:
    return any(char in value for char in _FORBIDDEN)


# ── Nom, chemins, propriété ───────────────────────────────────────────
def vm_name(campaign_id: int, coin: str, token: str) -> str:
    """``fossor-c<id>-<coin>-<jeton>`` : préfixe Fossor, unique, sûr pour libvirt."""
    try:
        cid = int(campaign_id)
    except (TypeError, ValueError):
        raise VmError("nom de VM : campagne numérique attendue")
    _require(cid > 0, "nom de VM : campagne obligatoire")
    ticker = str(coin or "").strip().lower()
    _require(bool(_RE_COIN.match(ticker)), "nom de VM : ticker invalide")
    suffix = str(token or "").strip().lower()
    _require(bool(_RE_TOKEN.match(suffix)), "nom de VM : jeton hexadécimal attendu")
    name = f"{FOSSOR_PREFIX}c{cid}-{ticker}-{suffix}"
    _require(len(name) <= 60, "nom de VM trop long")
    return name


def vm_dir(name: str) -> str:
    _require(is_fossor_name(name), "nom de VM hors préfixe Fossor")
    return f"{IMAGE_ROOT}/vms/{name}"


def overlay_path(name: str) -> str:
    return f"{vm_dir(name)}/disk.qcow2"


def seed_path(name: str) -> str:
    return f"{vm_dir(name)}/seed.iso"


def user_data_path(name: str) -> str:
    return f"{vm_dir(name)}/user-data"


def meta_data_path(name: str) -> str:
    return f"{vm_dir(name)}/meta-data"


def base_image_path(model: dict[str, Any]) -> str:
    return f"{IMAGE_ROOT}/images/{model['base_image_name']}"


def is_fossor_name(name: str) -> bool:
    return bool(name) and name.startswith(FOSSOR_PREFIX) and re.match(
        r"^fossor-[a-z0-9][a-z0-9-]{0,52}$", name
    ) is not None


def is_owned(name: str, description: str) -> bool:
    """Propriété = préfixe **et** métadonnée. Les deux sont exigés."""
    return is_fossor_name(name) and OWNER_MARKER in (description or "")


def ensure_owned(name: str, description: str) -> None:
    if is_owned(name, description):
        return
    if not is_fossor_name(name):
        raise VmError(
            f"VM « {name} » refusée : son nom ne porte pas le préfixe Fossor "
            f"« {FOSSOR_PREFIX} » — elle n'appartient pas à Fossor."
        )
    raise VmError(
        f"VM « {name} » refusée : métadonnée « {OWNER_MARKER} » absente — "
        "Fossor ne touche jamais une VM qu'il n'a pas créée."
    )


# ── cloud-init ────────────────────────────────────────────────────────
def cloud_init_user_data(name: str, ssh_public_key: str) -> str:
    key = (ssh_public_key or "").strip()
    _require(bool(_RE_KEY.match(key)), "clé SSH de Fossor : ligne OpenSSH attendue")
    _require("\n" not in key, "clé SSH : une seule ligne")
    host = name[:63]
    return "\n".join(
        [
            "#cloud-config",
            f"hostname: {host}",
            "preserve_hostname: false",
            "users:",
            f"  - name: {VM_USER}",
            "    shell: /bin/bash",
            "    lock_passwd: true",
            "    sudo: ALL=(ALL) NOPASSWD:ALL",
            "    ssh_authorized_keys:",
            f"      - {key}",
            "ssh_pwauth: false",
            "disable_root: true",
            "package_update: false",
            "runcmd:",
            f"  - [ install, -d, -m, '0755', -o, {VM_USER}, -g, {VM_USER}, "
            f"'{INSTALL_DIR}', '{LOG_DIR}', '{RUN_DIR}', '{DATA_DIR}' ]",
            "final_message: \"fossor ready\"",
            "",
        ]
    )


def cloud_init_meta_data(name: str) -> str:
    _require(is_fossor_name(name), "méta-données : nom Fossor attendu")
    return "\n".join(
        [f"instance-id: {name}", f"local-hostname: {name[:63]}", ""]
    )


# ── Découverte : IP, état, liste ──────────────────────────────────────
def parse_domifaddr(text: str) -> str | None:
    """Première IPv4 d'une sortie ``virsh domifaddr`` (arp puis lease)."""
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        for part in parts:
            candidate = part.split("/", 1)[0].strip()
            if not _RE_IPV4.match(candidate):
                continue
            if candidate.startswith("127.") or candidate.startswith("169.254."):
                continue
            return candidate
    return None


def parse_domstate(text: str) -> str:
    value = (text or "").strip().splitlines()
    return value[0].strip().lower() if value else ""


def parse_vm_list(text: str) -> list[VmInfo]:
    """Liste ``VM:<nom>`` + description (bloc ``virsh desc``) → objets purs."""
    items: list[VmInfo] = []
    name = ""
    buffer: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("VM:"):
            if name:
                items.append(VmInfo(name, " ".join(buffer).strip(), is_owned(name, " ".join(buffer))))
            name = line[3:].strip()
            buffer = []
        elif name:
            buffer.append(line.strip())
    if name:
        description = " ".join(buffer).strip()
        items.append(VmInfo(name, description, is_owned(name, description)))
    return items


def parse_pveversion(text: str) -> dict[str, Any]:
    """Détection Proxmox sur l'hôte : pilote **à venir**, mais reconnu."""
    raw = (text or "").strip()
    if not raw or "command not found" in raw:
        return {"hypervisor": "", "version": "", "driver": "none"}
    match = re.search(r"pve-manager/([0-9][^\s]*)", raw)
    if match or raw.lower().startswith("proxmox"):
        return {"hypervisor": "proxmox", "version": match.group(1) if match else "", "driver": "proxmox"}
    return {"hypervisor": "", "version": "", "driver": "none"}


def parse_libvirt_version(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw or "command not found" in raw:
        return {"hypervisor": "", "version": "", "driver": "none"}
    match = re.search(r"([0-9]+\.[0-9]+\.[0-9]+)", raw)
    return {"hypervisor": "libvirt", "version": match.group(1) if match else "", "driver": "libvirt"}


# ── ``{vm_node}`` ─────────────────────────────────────────────────────
def validate_vm_node(value: str) -> str:
    """``ip:port`` du nœud de la VM — la seule forme acceptée pour ``{vm_node}``."""
    text = str(value or "")
    host, sep, port = text.rpartition(":")
    _require(bool(sep), "{vm_node} : forme ip:port attendue")
    _require(bool(_RE_IPV4.match(host)), "{vm_node} : adresse IPv4 attendue")
    _require(port.isdigit() and 1024 <= int(port) <= 65535, "{vm_node} : port 1024-65535")
    return f"{host}:{int(port)}"


def node_address(ip: str, port: int) -> str:
    _require(bool(_RE_IPV4.match(ip or "")), "adresse de la VM invalide")
    return validate_vm_node(f"{ip}:{int(port)}")


# ── Plans de commandes ────────────────────────────────────────────────
def _root(*argv: str) -> tuple[str, ...]:
    return ("sudo", "-n", *argv)


def _sh(script: str, *args: str) -> tuple[str, ...]:
    return ("sh", "-c", script, "fossor", *args)


def image_plan(model: dict[str, Any]) -> list[VmStep]:
    path = base_image_path(model)
    script = (
        'p="$1"; u="$2"; s="$3"; '
        'if [ -f "$p" ] && echo "$s  $p" | sha256sum -c - >/dev/null 2>&1; then '
        'echo "IMAGE:present $s"; exit 0; fi; '
        'rm -f "$p" "$p.part"; '
        'if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 600 -o "$p.part" "$u"; '
        'elif command -v wget >/dev/null 2>&1; then wget -q -O "$p.part" "$u"; '
        'elif command -v python3 >/dev/null 2>&1; then '
        'python3 -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" '
        '"$u" "$p.part"; else echo "IMAGE:no-downloader" >&2; exit 3; fi || exit 4; '
        'echo "$s  $p.part" | sha256sum -c - || { rm -f "$p.part"; exit 5; }; '
        'mv "$p.part" "$p" && echo "IMAGE:downloaded $s"'
    )
    return [
        VmStep(
            "répertoires des images et des VM",
            _root("install", "-d", "-m", "0775", f"{IMAGE_ROOT}/images", f"{IMAGE_ROOT}/vms"),
        ),
        VmStep(
            "image de base (téléchargement et SHA256 vérifié)",
            _root(*_sh(script, path, model["base_image_url"], model["base_image_sha256"])),
            timeout=600,
        ),
        VmStep("image de base présente", _root("test", "-s", path)),
    ]


def create_plan(model: dict[str, Any], name: str, ssh_public_key: str) -> list[VmStep]:
    """Création complète : surcouche qcow2, seed cloud-init, ``virt-install --import``."""
    overlay = overlay_path(name)
    seed = seed_path(name)
    steps = image_plan(model)
    steps += [
        VmStep("répertoire de la VM", _root("install", "-d", "-m", "0775", vm_dir(name))),
        VmStep(
            "disque en surcouche qcow2 sur l'image de base",
            _root(
                "qemu-img", "create", "-f", "qcow2",
                "-b", base_image_path(model), "-F", "qcow2",
                overlay, f"{model['disk_gb']}G",
            ),
            timeout=300,
        ),
        VmStep(
            "user-data cloud-init (utilisateur specminer, clé Fossor)",
            _root("tee", user_data_path(name)),
            stdin=cloud_init_user_data(name, ssh_public_key),
        ),
        VmStep(
            "meta-data cloud-init",
            _root("tee", meta_data_path(name)),
            stdin=cloud_init_meta_data(name),
        ),
        VmStep(
            "image d'amorçage cloud-init",
            _root("cloud-localds", seed, user_data_path(name), meta_data_path(name)),
            timeout=300,
        ),
        VmStep(
            "création et démarrage de la VM (virt-install --import)",
            _root(
                "virt-install",
                "--connect", HYPERVISOR,
                "--name", name,
                "--memory", str(model["memory_mb"]),
                "--vcpus", str(model["vcpus"]),
                "--cpu", "max",
                "--disk", f"path={overlay},format=qcow2,bus=virtio",
                "--disk", f"path={seed},device=cdrom",
                "--network", f"network={model['network']},model=virtio",
                "--os-variant", "generic",
                "--import",
                "--noautoconsole",
                "--graphics", "none",
                "--metadata", f"description={OWNER_MARKER},title=Fossor",
            ),
            timeout=600,
        ),
        VmStep("état de la VM", _root("virsh", "--connect", HYPERVISOR, "domstate", name),
               allow_fail=True, expect="running"),
    ]
    return steps


def ip_plan(name: str) -> list[VmStep]:
    return [
        VmStep(
            "adresse de la VM (bail ARP)",
            _root("virsh", "--connect", HYPERVISOR, "domifaddr", name, "--source", "arp"),
            allow_fail=True,
        ),
        VmStep(
            "adresse de la VM (bail du réseau)",
            _root("virsh", "--connect", HYPERVISOR, "domifaddr", name, "--source", "lease"),
            allow_fail=True,
        ),
    ]


def host_key_plan(ip: str) -> list[VmStep]:
    return [
        VmStep(
            "clé d'hôte de la VM (épinglée ensuite)",
            _root("ssh-keyscan", "-T", "10", "-t", "ed25519", ip),
            allow_fail=True,
            timeout=60,
        )
    ]


def trust_plan(ip: str, host_key_line: str) -> list[VmStep]:
    """Écrit la clé d'hôte dans le known_hosts dédié de Fossor sur l'hôte."""
    _require(bool(_RE_IPV4.match(ip or "")), "adresse de la VM invalide")
    _require(bool((host_key_line or "").strip()), "clé d'hôte vide")
    return [
        VmStep("épinglage de la clé d'hôte de la VM", _root("tee", "-a", KNOWN_HOSTS),
               stdin=host_key_line.strip() + "\n"),
    ]


def ssh_vm_argv(ip: str, argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Commande passée à l'hôte : SSH vers la VM, clé Fossor, hôte épinglé."""
    _require(bool(_RE_IPV4.match(ip or "")), "adresse de la VM invalide")
    _require(bool(argv), "commande vide")
    return _root(
        "ssh",
        "-i", KEY_PATH,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=10",
        f"{VM_USER}@{ip}",
        *argv,
    )


def vm_step(title: str, ip: str, argv: list[str] | tuple[str, ...], **kwargs: Any) -> VmStep:
    return VmStep(title, ssh_vm_argv(ip, argv), **kwargs)


def provision_plan(
    model: dict[str, Any],
    profile: dict[str, Any],
    ip: str,
    *,
    coin: str,
) -> list[VmStep]:
    """Installation dans la VM : nœud, mineur, wallet — SHA256 vérifié **dans la VM**."""
    from app import miners as M

    download = (
        'if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 600 -o "$1" "$2"; '
        'elif command -v wget >/dev/null 2>&1; then wget -q -O "$1" "$2"; '
        'elif command -v python3 >/dev/null 2>&1; then '
        'python3 -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" '
        '"$2" "$1"; else echo "no downloader" >&2; exit 3; fi'
    )
    steps = [
        vm_step("outils de la VM", ip, ["sh", "-c",
                "for t in curl wget tar sha256sum ps; do command -v \"$t\" >/dev/null 2>&1 "
                "|| echo \"MISSING:$t\"; done; echo OK"]),
        vm_step("répertoires de travail",
                ip, ["install", "-d", "-m", "0755", INSTALL_DIR, DATA_DIR]),
        # Le mineur vit sous /opt/fossor (mêmes chemins que sur un hôte) :
        # specminer a sudo sans mot de passe dans la VM (cloud-init).
        vm_step("répertoires du mineur dans la VM",
                ip, ["sudo", "-n", "install", "-d", "-m", "0775", "-o", VM_USER, "-g", VM_USER,
                     M.RUN_DIR, M.LOG_DIR]),
        vm_step("racine des binaires du mineur",
                ip, ["sudo", "-n", "install", "-d", "-m", "0755", M.INSTALL_ROOT]),
    ]
    if model.get("node_url"):
        node_dir = f"{INSTALL_DIR}/node"
        archive = f"{INSTALL_DIR}/node.tar.gz"
        steps += [
            vm_step("répertoire du nœud", ip, ["install", "-d", "-m", "0755", node_dir]),
            vm_step(
                "téléchargement du nœud dans la VM",
                ip,
                ["sh", "-c", download, "fossor", archive, model["node_url"]],
                timeout=600,
            ),
            vm_step(
                "SHA256 du nœud vérifié dans la VM",
                ip,
                ["sh", "-c", 'echo "$2  $1" | sha256sum -c -', "fossor", archive,
                 model["node_sha256"]],
            ),
            vm_step("extraction du nœud", ip, ["tar", "-xzf", archive, "-C", node_dir], timeout=600),
        ]
    binary_dir = M.install_dir(profile["software"], profile["version"])
    miner_archive = f"{INSTALL_DIR}/miner.tar.gz"
    steps += [
        vm_step("répertoire du mineur", ip, ["sudo", "-n", "install", "-d", "-m", "0755", binary_dir]),
        vm_step(
            "téléchargement du mineur dans la VM",
            ip,
            ["sh", "-c", download, "fossor", miner_archive, profile["binary_url"]],
            timeout=600,
        ),
        vm_step(
            "SHA256 du mineur vérifié dans la VM",
            ip,
            ["sh", "-c", 'echo "$2  $1" | sha256sum -c -', "fossor", miner_archive, profile["sha256"]],
        ),
        vm_step("extraction du mineur", ip, ["sudo", "-n", "tar", "-xzf", miner_archive,
                                            "-C", binary_dir], timeout=600),
        vm_step("binaire du mineur exécutable",
                ip, ["sudo", "-n", "chmod", "0755", M.binary_path(profile)]),
    ]
    if model.get("wallet_create_cmd"):
        values = {"datadir": DATA_DIR, "coin": coin}
        argv = render_template(model["wallet_create_cmd"], values)
        steps.append(
            vm_step("création du wallet dans la VM (adresse seule remonte)", ip, argv, timeout=300)
        )
    if model.get("node_start_cmd"):
        values = {"datadir": DATA_DIR, "port": str(model["node_port"]), "coin": coin}
        argv = render_template(model["node_start_cmd"], values)
        steps.append(vm_step("démarrage du nœud dans la VM (solo)", ip, argv, timeout=300))
    return steps


def vm_miner_start_plan(profile: dict[str, Any], argv: list[str], ip: str) -> list[VmStep]:
    """Démarrage du mineur **dans** la VM (mode ``vm``), sans systemd."""
    from app import miners as M

    script = (
        'log="$1"; pid="$2"; shift 2; '
        'setsid nohup "$@" >> "$log" 2>&1 < /dev/null & echo $! > "$pid"; cat "$pid"'
    )
    return [
        vm_step(
            "démarrage du mineur dans la VM",
            ip,
            ["sh", "-c", script, "fossor", M.log_file(profile), M.pid_file(profile), *argv],
            timeout=60,
        )
    ]


def vm_miner_stop_plan(profile: dict[str, Any], ip: str) -> list[VmStep]:
    from app import miners as M

    script = (
        'pid=$(cat "$1" 2>/dev/null) || exit 0; test -n "$pid" || exit 0; '
        'grep -q /home/specminer/opt /proc/$pid/cmdline 2>/dev/null || exit 2; '
        'kill -TERM "$pid"; for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done; '
        'rm -f "$1"'
    )
    return [
        vm_step("arrêt du mineur dans la VM", ip, ["sh", "-c", script, "fossor", M.pid_file(profile)],
                timeout=60, allow_fail=True)
    ]


def collect_plan(name: str, ip: str, profile: dict[str, Any] | None = None) -> list[VmStep]:
    """État de la VM et du mineur : ``virsh`` sur l'hôte, ``ps``/API dans la VM."""
    from app import miners as M

    steps = [
        VmStep("état de la VM", _root("virsh", "--connect", HYPERVISOR, "domstate", name),
               allow_fail=True),
        VmStep("ressources de la VM", _root("virsh", "--connect", HYPERVISOR, "dominfo", name),
               allow_fail=True),
    ]
    if ip:
        steps.append(
            vm_step(
                "processus du mineur dans la VM",
                ip,
                ["sh", "-c", 'ps -eo pid=,args= | grep -F "$1" | grep -v grep || true',
                 "fossor", M.INSTALL_ROOT],
                allow_fail=True,
            )
        )
        if profile and profile.get("api_port"):
            steps.append(
                vm_step(
                    "hashrate du mineur dans la VM",
                    ip,
                    ["curl", "-s", "--max-time", "3", f"http://127.0.0.1:{profile['api_port']}/"],
                    allow_fail=True,
                )
            )
    return steps


def state_plan(name: str) -> list[VmStep]:
    return [
        VmStep("état de la VM", _root("virsh", "--connect", HYPERVISOR, "domstate", name)),
    ]


def shutdown_plan(name: str) -> list[VmStep]:
    """Fin de campagne : la VM est **éteinte et conservée** (le wallet y est)."""
    return [
        VmStep(
            "extinction de la VM (conservée)",
            _root("virsh", "--connect", HYPERVISOR, "shutdown", name),
            allow_fail=True,
            timeout=120,
        ),
        VmStep("état après extinction", _root("virsh", "--connect", HYPERVISOR, "domstate", name),
               allow_fail=True),
    ]


def destroy_plan(name: str) -> list[VmStep]:
    """Destruction explicite : arrêt, indéfinition, suppression de la surcouche.

    L'appelant a déjà vérifié la propriété (préfixe + métadonnée) **et** la
    sauvegarde du wallet.
    """
    return [
        VmStep("arrêt forcé", _root("virsh", "--connect", HYPERVISOR, "destroy", name),
               allow_fail=True, timeout=120),
        VmStep("indéfinition de la VM", _root("virsh", "--connect", HYPERVISOR, "undefine", name,
                                             "--nvram"), allow_fail=True),
        VmStep("suppression de la surcouche", _root("rm", "-rf", vm_dir(name))),
    ]


def list_plan() -> list[VmStep]:
    script = (
        'c="virsh --connect ' + HYPERVISOR + '"; '
        'for n in $($c list --all --name 2>/dev/null); do '
        '[ -n "$n" ] || continue; echo "VM:$n"; '
        '$c desc "$n" --config 2>/dev/null | sed "s/^ */  /"; done'
    )
    return [VmStep("VM présentes sur l'hôte", _root(*_sh(script)), allow_fail=True)]


def hypervisor_plan() -> list[VmStep]:
    return [
        VmStep("version de libvirt", _root("virsh", "--connect", HYPERVISOR, "version"),
               allow_fail=True),
        VmStep("version de Proxmox", _root("pveversion"), allow_fail=True),
    ]


# ── Sauvegarde du wallet (§10.11) ────────────────────────────────────
def backup_stat_argv(path: str) -> list[str]:
    _require(bool(_RE_ABS_PATH.match(path or "")), "chemin de sauvegarde invalide")
    return ["sh", "-c", 'stat -c "%s %Y" -- "$1" 2>/dev/null || echo MISSING', "fossor", path]


def backup_check_plan(ip: str, path: str) -> list[VmStep]:
    return [vm_step("contrôle de la sauvegarde du wallet (stat, sans lecture)", ip,
                    backup_stat_argv(path), allow_fail=True)]


def parse_backup_stat(text: str) -> dict[str, Any]:
    """``stat`` seulement : taille et date. Le contenu n'est jamais lu."""
    raw = (text or "").strip().splitlines()
    line = raw[-1].strip() if raw else ""
    if not line or line == "MISSING":
        return {"present": False, "size": 0, "mtime": 0}
    parts = line.split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return {"present": False, "size": 0, "mtime": 0}
    return {"present": True, "size": int(parts[0]), "mtime": int(parts[1])}


def evaluate_backup(
    stat: dict[str, Any], now_epoch: int, max_age_days: int = DEFAULT_BACKUP_MAX_AGE_DAYS
) -> dict[str, Any]:
    """Verdict §10.11 : présent, non vide, récent — sans jamais lire le fichier."""
    reason = ""
    if not stat.get("present"):
        reason = "fichier de sauvegarde absent"
    elif int(stat.get("size") or 0) <= 0:
        reason = "fichier de sauvegarde vide"
    else:
        age_s = max(0, int(now_epoch) - int(stat.get("mtime") or 0))
        if age_s > max_age_days * 86400:
            reason = (
                f"sauvegarde trop ancienne ({age_s // 86400} j, limite {max_age_days} j)"
            )
    verified = not reason
    return {
        "verified": verified,
        "reason": reason or "sauvegarde présente, non vide et récente",
        "size": int(stat.get("size") or 0),
        "mtime": int(stat.get("mtime") or 0),
        "max_age_days": int(max_age_days),
        "checked_at_epoch": int(now_epoch),
    }


def ensure_destroyable(
    name: str, description: str, backup: dict[str, Any] | None, now_epoch: int,
    max_age_days: int = DEFAULT_BACKUP_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Deux refus, dans cet ordre : propriété, puis sauvegarde vérifiée."""
    ensure_owned(name, description)
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
