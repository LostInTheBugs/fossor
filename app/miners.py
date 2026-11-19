"""Miner profiles (§4.5) — strict validation, argument rendering, systemd unit.

Everything in this module is pure: no SSH, no database, no clock. The
operator writes a *template*; the values that fill it come from records
(wallet, pool) or from the machine (worker name, GPU indices, threads).

Safety model (§5.5)
-------------------
* the template is split with :func:`shlex.split` **before** any substitution,
  so a value can never add an argument, a shell operator or a newline;
* every substituted value is checked against its own pattern;
* the result is an **argv list**, never a string handed to a shell;
* the first token must be the exact installed binary path, so a profile can
  only ever execute what Fossor itself downloaded and hashed.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Iterable

INSTALL_ROOT = "/opt/fossor/miners"
RUN_DIR = "/opt/fossor/run"
LOG_DIR = "/opt/fossor/logs"
UNIT_DIR = "/etc/systemd/system"

SOFTWARES = ("srbminer", "lolminer", "bzminer", "xmrig", "custom")
# §4.6: trois modes depuis 3c — ``host``, ``vm`` et ``hybrid``.
EXECUTION_MODES = ("host", "vm", "hybrid")
LATER_MODES: tuple[str, ...] = ()

TEMPLATE_VARS = ("wallet", "pool", "worker", "gpus", "threads", "api_port", "vm_node")

PATTERNS: dict[str, str] = {
    "wallet": r"^[A-Za-z0-9:._-]{8,128}$",
    "pool": r"^(solo|stratum(\+tcp|\+ssl)?://[A-Za-z0-9.-]+:\d{2,5})$",
    "worker": r"^[A-Za-z0-9_-]{1,32}$",
    "gpus": r"^\d+(,\d+)*$",
    "threads": r"^[1-9]\d{0,3}$",
    "api_port": r"^\d{2,5}$",
    # §4.6 hybride : adresse du nœud de la VM, ``ip:port`` (port 1024-65535).
    "vm_node": r"^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d):(?:102[4-9]|10[3-9]\d|1[1-9]\d{2}|[2-9]\d{3}|[1-5]\d{4}|6[0-4]\d{3}|65[0-4]\d{2}|655[0-2]\d|6553[0-5])$",
}

_RE_URL = re.compile(r"^https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+$")
_RE_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_RE_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_RE_PATH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_RE_COIN = re.compile(r"^[A-Za-z0-9]{2,20}$")
_RE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_RE_VAR = re.compile(r"\{([a-z_]+)\}")
# Characters a template token may never contain: they only make sense for a
# shell, and this template is never given to one.
_FORBIDDEN_IN_TEMPLATE = (";", "|", "&", "`", "$", "\n", "\r", "<", ">", "\\")


class ProfileError(ValueError):
    """Invalid miner profile — message is safe to show to the operator."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ProfileError(message)


def install_dir(software: str, version: str) -> str:
    return f"{INSTALL_ROOT}/{software}/{version}"


def binary_path(profile: dict[str, Any]) -> str:
    return f"{install_dir(profile['software'], profile['version'])}/{profile['binary_path']}"


def slug(name: str) -> str:
    """Systemd-safe identifier for a profile name."""
    raw = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    raw = raw[:40].strip("-")
    return raw or "profil"


def unit_name(profile: dict[str, Any]) -> str:
    return f"fossor-miner-{slug(profile['name'])}.service"


def pid_file(profile: dict[str, Any]) -> str:
    return f"{RUN_DIR}/{slug(profile['name'])}.pid"


def log_file(profile: dict[str, Any]) -> str:
    return f"{LOG_DIR}/{slug(profile['name'])}.log"


# ── Validation ────────────────────────────────────────────────────────
def validate_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised, validated profile dict or raise :class:`ProfileError`."""
    name = str(data.get("name") or "").strip()
    _require(1 <= len(name) <= 60, "nom du profil : 1 à 60 caractères")

    coin = str(data.get("coin") or "").strip().upper()
    _require(bool(_RE_COIN.match(coin)), "coin : 2 à 20 caractères alphanumériques")

    wallet = str(data.get("wallet") or "").strip()
    _require(
        bool(re.fullmatch(PATTERNS["wallet"], wallet)),
        "wallet : ^[A-Za-z0-9:._-]{8,128}$ (aucun espace, aucun métacaractère)",
    )

    pool = str(data.get("pool") or "").strip()
    _require(
        bool(re.fullmatch(PATTERNS["pool"], pool)),
        "pool : « solo » ou stratum://hote:port (stratum+tcp / stratum+ssl)",
    )

    software = str(data.get("software") or "").strip().lower()
    _require(software in SOFTWARES, f"logiciel : {', '.join(SOFTWARES)}")

    binary_source = str(data.get("binary_source") or "url").strip().lower()
    _require(binary_source in ("url", "fossor"),
             "source du binaire : url (release publique) ou fossor (archive déposée)")

    binary_url = str(data.get("binary_url") or "").strip()
    if binary_source == "fossor":
        _require(not binary_url or bool(_RE_URL.match(binary_url)),
                 "binaire Fossor : URL vide ou http(s) ; l'archive est déposée dans Fossor")
    else:
        _require(bool(_RE_URL.match(binary_url)), "binaire : URL http(s) de l'archive, sans espace")

    sha256 = str(data.get("sha256") or "").strip().lower()
    _require(bool(_RE_SHA256.match(sha256)), "SHA256 obligatoire : 64 caractères hexadécimaux")

    version = str(data.get("version") or "").strip()
    _require(bool(_RE_VERSION.match(version)), "version : lettres, chiffres, point, tiret, souligné")

    rel = str(data.get("binary_path") or "").strip().lstrip("./")
    _require(bool(_RE_PATH.match(rel)), "chemin dans l'archive : caractères [A-Za-z0-9._/-]")
    _require(".." not in rel.split("/"), "chemin dans l'archive : « .. » interdit")
    _require(not rel.startswith("/"), "chemin dans l'archive : relatif, pas absolu")

    cmdline = str(data.get("cmdline") or "").strip()
    _require(bool(cmdline), "gabarit de ligne de commande obligatoire")
    tokens = _split_template(cmdline)

    expected = f"{install_dir(software, version)}/{rel}"
    _require(
        tokens[0] == expected,
        f"le premier mot du gabarit doit être le binaire installé : {expected}",
    )

    mode = str(data.get("execution_mode") or "host").strip().lower()
    _require(mode in EXECUTION_MODES, "mode d'exécution : host, vm ou hybrid")
    uses_vm_node = "vm_node" in template_vars(cmdline)
    if mode == "hybrid":
        _require(
            uses_vm_node,
            "mode hybride : le gabarit doit se connecter au nœud de la VM avec {vm_node}",
        )
    else:
        _require(
            not uses_vm_node,
            f"{{vm_node}} n'a de sens qu'en mode hybride (mode demandé : {mode})",
        )

    api_port = data.get("api_port", 0)
    try:
        api_port = int(api_port or 0)
    except (TypeError, ValueError):
        raise ProfileError("port d'API : nombre entier")
    _require(api_port == 0 or 1024 <= api_port <= 65535, "port d'API : 0 (aucun) ou 1024-65535")

    api_kind = str(data.get("api_kind") or "").strip().lower()
    _require(api_kind in ("", "cpuminer", "srbminer", "xmrig", "generic"),
             "type d'API : cpuminer, srbminer, xmrig, generic ou aucun")

    wallet_mode = str(data.get("wallet_mode") or "file").strip().lower()
    _require(wallet_mode in ("file", "pool_address"),
             "wallet : file (fichier local) ou pool_address (adresse de pool seule)")

    return {
        "name": name,
        "coin": coin,
        "wallet": wallet,
        "pool": pool,
        "software": software,
        "binary_url": binary_url,
        "sha256": sha256,
        "binary_path": rel,
        "version": version,
        "cmdline": cmdline,
        "api_port": api_port,
        "api_kind": api_kind,
        "binary_source": binary_source,
        "wallet_mode": wallet_mode,
        "execution_mode": mode,
        "threads": int(_validated("threads", str(data.get("threads")))) if data.get("threads") else 0,
    }


def _validated(var: str, value: str) -> str:
    pattern = PATTERNS[var]
    # fullmatch, not match: ``$`` accepts a trailing newline, a value never should.
    if not re.fullmatch(pattern, value):
        raise ProfileError(f"valeur invalide pour {{{var}}} : {value!r}")
    return value


def _split_template(cmdline: str) -> list[str]:
    try:
        tokens = shlex.split(cmdline)
    except ValueError as exc:
        raise ProfileError(f"gabarit illisible (guillemets non fermés ?) : {exc}")
    _require(bool(tokens), "gabarit vide")
    for token in tokens:
        for char in _FORBIDDEN_IN_TEMPLATE:
            _require(
                char not in token,
                f"gabarit : caractère interdit {char!r} — les valeurs passent par les variables",
            )
        for var in _RE_VAR.findall(token):
            _require(var in TEMPLATE_VARS, f"variable inconnue {{{var}}}")
        stripped = _RE_VAR.sub("", token)
        _require("{" not in stripped and "}" not in stripped, "gabarit : accolade orpheline")
    return tokens


def template_vars(cmdline: str) -> set[str]:
    """Variables actually used by a template."""
    return set(_RE_VAR.findall(cmdline))


# ── Rendering ─────────────────────────────────────────────────────────
def render_argv(profile: dict[str, Any], values: dict[str, str]) -> list[str]:
    """Split the template, then substitute — an argv list, never a shell string."""
    tokens = _split_template(profile["cmdline"])
    out: list[str] = []
    for token in tokens:
        def _sub(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in values:
                raise ProfileError(f"valeur manquante pour {{{var}}}")
            return _validated(var, str(values[var]))

        rendered = _RE_VAR.sub(_sub, token)
        for char in _FORBIDDEN_IN_TEMPLATE:
            _require(
                char not in rendered,
                f"valeur substituée refusée : caractère {char!r} dans {rendered!r}",
            )
        out.append(rendered)
    _require(bool(out), "commande vide")
    return out


def build_values(
    profile: dict[str, Any],
    *,
    worker: str,
    gpus: Iterable[int] = (),
    threads: int | None = None,
    api_port: int | None = None,
    vm_node: str | None = None,
) -> dict[str, str]:
    """Values for the variables a template uses, each one already validated."""
    used = template_vars(profile["cmdline"])
    gpu_list = [str(g) for g in gpus]
    values: dict[str, str] = {
        "wallet": profile["wallet"],
        "pool": profile["pool"],
        "worker": worker,
        "gpus": ",".join(gpu_list),
        "threads": str(threads or profile.get("threads") or 0),
        "api_port": str(api_port if api_port is not None else profile.get("api_port") or 0),
        "vm_node": str(vm_node or ""),
    }
    values = {k: str(v) for k, v in values.items()}
    for var in sorted(used):
        if var == "gpus" and not gpu_list:
            raise ProfileError("gabarit : {gpus} mais aucun GPU coché sur cette machine")
        if var == "vm_node" and not values[var]:
            raise ProfileError("gabarit : {vm_node} requis mais aucun nœud de VM disponible")
        if var in ("threads", "api_port") and values[var] in ("", "0"):
            raise ProfileError(f"gabarit : {{{var}}} requis mais aucune valeur réglée")
        _validated(var, values[var])
    return values


def worker_name(machine_name: str) -> str:
    """Worker/RIG name derived from the machine name, safe by construction."""
    raw = re.sub(r"[^A-Za-z0-9_-]+", "-", machine_name or "").strip("-")[:32]
    return raw or "fossor"


# ── systemd ───────────────────────────────────────────────────────────
def systemd_quote(arg: str) -> str:
    """Quote one argv element for ``ExecStart=`` (systemd double-quote rules)."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def exec_start(argv: list[str]) -> str:
    return " ".join(systemd_quote(a) for a in argv)


def render_unit(
    profile: dict[str, Any],
    argv: list[str],
    *,
    user: str,
) -> str:
    """Frozen systemd unit text for one miner profile."""
    return "\n".join(
        [
            "[Unit]",
            f"Description=Fossor miner {slug(profile['name'])} ({profile['coin']})",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={user}",
            f"WorkingDirectory={install_dir(profile['software'], profile['version'])}",
            f"ExecStart={exec_start(argv)}",
            "Restart=on-failure",
            "RestartSec=10",
            "KillSignal=SIGTERM",
            "TimeoutStopSec=30",
            "StandardOutput=journal",
            "StandardError=journal",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
