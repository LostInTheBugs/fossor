"""Adoption des mineurs lancés hors Fossor (§6.7).

Un mineur « lancé hors Fossor » (nœud solo, mineur de pool sous ``screen`` ou
script ``@reboot``) reste visible mais non pilotable.  Ce module construit un
**plan d'adoption** en lecture seule, puis la séquence d'adoption et son retour
arrière.  Aucune commande n'est exécutée à la construction du plan.

Règles de sécurité
------------------
* le lanceur est **prouvé**, jamais deviné : unité systemd lue dans
  ``/proc/<pid>/cgroup`` (celle du processus et de ses parents), cron croisé
  avec le script lu sur l'hôte ; une chaîne incertaine bloque le mineur ;
* une unité ``fossor-miner-*`` n'est jamais attribuée à un processus qu'elle ne
  lance pas ; tout plan qui arrêterait, désactiverait ou modifierait une unité
  existante est refusé ;
* chemins **absolus** : binaire par ``readlink /proc/<pid>/exe``, répertoire par
  ``/proc/<pid>/cwd``, CLI du même répertoire vérifié par ``test -x`` ; l'unité
  n'utilise que des chemins absolus ;
* l'unité garde le **compte d'origine** du processus (root compris) ; le compte
  ``fossor`` ne sert qu'à piloter l'unité par ``sudo systemctl`` ;
* un lanceur de surveillance (cron à la minute, boucle de script) est neutralisé
  **avant** l'arrêt du mineur et restauré au retour arrière ;
* les secrets et les wallets ne sortent jamais : ``mask_command`` masque les
  mots de passe et tronque les wallets, y compris dans les argv proposés ;
* un **nœud solo** n'est arrêté que par ``<coin>-cli -datadir=… stop`` ; jamais
  de ``kill -9``, jamais de signal direct au démon qui porte le wallet ;
* le ``wallet.dat`` est **copié** (droits 600) et vérifié (taille + sha256)
  avant l'arrêt ; son contenu n'est ni lu, ni affiché, ni déplacé.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from app.external_miners import _datadir, mask_command, truncate_wallet
from app.miners import slug

UNIT_PREFIX = "fossor-miner-"
INSTALL_ROOT = "/opt/fossor/miners"
VERIFY_MINUTES = 5
CRON_BACKUP_TAG = ".fossor-backup-"
WALLET_BACKUP_TAG = ".fossor-backup-"
SOLO_TIMEOUT = 120
POOL_STOP_TIMEOUT = 30

#: Répertoires balayés en lecture seule pour trouver un lanceur (HiveOS, rc.local).
SEARCH_DIRS = ("/hive", "/hive-config", "/etc/rc.local", "/etc/systemd/system")
#: Processus système jamais présentés comme lanceur d'un mineur.
SYSTEM_UNITS = ("cron.service", "crond.service", "atd.service", "systemd-user.service")


class AdoptionError(Exception):
    """Le plan ne peut pas être exécuté en l'état."""


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def _sq(value: str) -> str:
    return shlex.quote(str(value))


def _sudo(inner: str) -> str:
    return f"sudo -n sh -c {_sq(inner)}"


def _launcher(
    mode: str,
    account: str,
    detail: str,
    *,
    script: str = "",
    unit: str = "",
    verified: bool,
    reason: str,
    schedule: str = "",
) -> dict[str, Any]:
    return {
        "mode": mode,
        "account": account,
        "detail": detail,
        "schedule": schedule,
        "script": script,
        "unit": unit,
        "verified": bool(verified),
        "reason": reason,
        "backup": None,
    }


# --------------------------------------------------------------------------- #
# Lecture de l'hôte (sorties brutes, jamais d'exécution ici)
# --------------------------------------------------------------------------- #
def parse_crontab(text: str) -> list[dict[str, Any]]:
    """Lignes utiles d'une crontab : ``schedule``, ``command``, ``reboot``."""
    entries: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            schedule, command = parts[0], parts[1]
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            schedule, command = " ".join(parts[:5]), parts[5]
        entries.append(
            {
                "line": line,
                "schedule": schedule,
                "command": command,
                "reboot": schedule == "@reboot",
            }
        )
    return entries


def parse_screens(text: str) -> list[str]:
    return [
        line.split("\t")[-1].strip()
        for line in (text or "").splitlines()
        if re.match(r"^\s*\d+\.\S+", line)
    ]


def _script_path(args: str) -> str:
    tokens = shlex.split(args or "")
    if len(tokens) >= 2 and tokens[0].split("/")[-1] in ("sh", "bash", "dash"):
        return tokens[1] if tokens[1].endswith(".sh") else ""
    return tokens[0] if tokens and tokens[0].endswith(".sh") else ""


def _clean_path(value: str) -> str:
    """Un chemin venu d'une sortie d'hôte : caractères sûrs, sinon vide."""
    text = str(value or "").strip()
    if not text.startswith("/"):
        return ""
    if not re.match(r"^[A-Za-z0-9_./@:+-]+$", text):
        return ""
    return text


def parse_ps_tree(text: str) -> list[dict[str, Any]]:
    """Rows of ``ps -eo user,pid,ppid,pcpu,etimes,args``."""
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("user "):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid, ppid = int(parts[1]), int(parts[2])
            cpu = float(str(parts[3]).replace(",", "."))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "user": parts[0],
                "pid": pid,
                "ppid": ppid,
                "cpu_pct": cpu,
                "elapsed_s": int(parts[4]) if str(parts[4]).isdigit() else None,
                "args": parts[5],
            }
        )
    return rows


def parse_proc_info(text: str) -> dict[str, dict[str, str]]:
    """Sortie de la sonde ``/proc`` : ``PID n`` puis EXE, CWD, UNIT, PPID, CMD."""
    facts: dict[str, dict[str, str]] = {}
    current = ""
    for raw in (text or "").splitlines():
        line = raw.rstrip("\n")
        if line.startswith("PID "):
            current = line.split(None, 1)[1].strip()
            facts[current] = {"pid": current, "exe": "", "cwd": "", "unit": "", "ppid": "", "cmd": ""}
            continue
        if not current:
            continue
        for key, prefix in (("exe", "EXE "), ("cwd", "CWD "), ("unit", "UNIT "),
                            ("ppid", "PPID "), ("cmd", "CMD ")):
            if line.startswith(prefix):
                facts[current][key] = line[len(prefix):].strip()
                break
    return facts


def parse_cli_ok(text: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[0] in ("X", "N"):
            out[parts[1].strip()] = parts[0] == "X"
    return out


def parse_wallet_info(text: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) == 3 and parts[0] == "W":
            try:
                size = int(parts[2])
            except ValueError:
                continue
            out[parts[1].strip()] = {"present": size >= 0, "size": max(size, 0)}
    return out


def parse_files(text: str) -> dict[str, str]:
    """Sortie ``FILE <chemin>`` … ``EOFILE`` : contenu des scripts lus."""
    files: dict[str, str] = {}
    path = ""
    body: list[str] = []
    for raw in (text or "").splitlines():
        if raw.startswith("FILE "):
            path, body = raw[5:].strip(), []
            continue
        if raw.strip() == "EOFILE":
            if path:
                files[path] = "\n".join(body)
            path, body = "", []
            continue
        if path:
            body.append(raw)
    return files


def ancestors(pid: str, tree: Sequence[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    """Chaîne des parents d'un PID, du plus proche au plus ancien."""
    by_pid = {str(row.get("pid")): row for row in tree or []}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = str(pid or "")
    for _ in range(limit):
        row = by_pid.get(current)
        if not row:
            break
        parent = str(row.get("ppid") or "")
        if not parent or parent in ("0", "1") or parent in seen:
            break
        seen.add(parent)
        nxt = by_pid.get(parent)
        if not nxt:
            break
        out.append(nxt)
        current = parent
    return out


def unit_from_cgroup(text: str) -> str:
    """Unité systemd réelle d'un processus, lue dans ``/proc/<pid>/cgroup``."""
    for line in (text or "").splitlines():
        path = line.split(":", 2)[-1].strip()
        match = re.search(r"/([^/]+\.service)$", path)
        if match:
            return match.group(1)
    return ""


def launcher_of(
    args: str,
    *,
    account: str,
    crontabs: dict[str, str] | None = None,
    screens: Sequence[str] = (),
    units: Sequence[str] = (),
    hints: Sequence[str] = (),
    proc: dict[str, Any] | None = None,
    tree: Sequence[dict[str, Any]] = (),
    scripts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Lanceur réel du processus, prouvé par sa chaîne (cgroup, parents, cron).

    Une unité ``fossor-miner-*`` n'est retenue que si le processus (ou l'un de
    ses parents) y est rattaché : la simple présence de l'unité sur l'hôte ne
    prouve rien.  À défaut de preuve, ``verified`` est faux et le mineur sera
    bloqué.
    """
    proc = dict(proc or {})
    scripts = dict(scripts or {})
    tokens = shlex.split(str(args or ""))
    declared = tokens[0] if tokens else ""
    exe = str(proc.get("exe") or "")
    binary_path = exe if exe.startswith("/") else (declared if declared.startswith("/") else "")
    binary = binary_path.split("/")[-1] or declared.split("/")[-1]
    names = [n for n in (binary, *hints) if n]
    pid = str(proc.get("pid") or "")
    chain = ancestors(pid, tree) if pid else []
    chain_args = " ".join(str(row.get("args") or "") for row in chain)

    # 1. L'unité systemd réelle du processus — jamais une unité voisine.
    own_unit = str(proc.get("unit") or "")
    if own_unit.startswith(UNIT_PREFIX):
        return _launcher(
            "service", account, own_unit, unit=own_unit, verified=True,
            reason=f"cgroup du processus {pid} = {own_unit}",
        )
    for parent in chain:
        unit = str(parent.get("unit") or "")
        if unit.startswith(UNIT_PREFIX):
            return _launcher(
                "service", account, unit, unit=unit, verified=True,
                reason=f"parent {parent.get('pid')} rattaché à {unit}",
            )

    # 2. Cron : la ligne lance le binaire, ou un script dont le contenu le lance.
    for owner, text in (crontabs or {}).items():
        for entry in parse_crontab(text):
            command = entry["command"]
            script = _script_path(command)
            how = ""
            if binary and re.search(r"(^|[\s/])" + re.escape(binary) + r"(\s|$)", command):
                how = f"binaire {binary} dans la ligne"
            elif script and script in str(args or ""):
                how = "script du processus"
            elif script and script in chain_args:
                how = f"script dans la chaîne des parents ({script})"
            elif script and binary and binary in (scripts.get(script) or ""):
                how = f"script lu sur l'hôte ({script})"
            if how:
                return _launcher(
                    "cron @reboot" if entry["reboot"] else "cron", owner, entry["line"],
                    script=script, verified=True, reason=how,
                    schedule=entry["schedule"],
                )

    # 3. Écran détaché nommé comme le binaire.
    for session in screens:
        if binary and binary in session:
            return _launcher("screen", account, session, verified=True,
                             reason=f"session screen nommée {binary}")

    # 4. Fichier de l'hôte qui lance le binaire (HiveOS, rc.local, unité tierce).
    for path, body in scripts.items():
        if not binary or binary not in (body or ""):
            continue
        if path.endswith(".service"):
            unit = path.split("/")[-1]
            if unit.startswith(UNIT_PREFIX):
                continue
            return _launcher("service hors Fossor", account, path, unit=unit,
                             verified=True, reason=f"unité de l'hôte {unit}")
        return _launcher("script", account, path, script=path, verified=True,
                         reason=f"script de l'hôte {path}")

    # 5. Le processus est lui-même ``sh script.sh``.
    script = _script_path(str(args or ""))
    if script:
        return _launcher("script", account, script, script=script, verified=True,
                         reason="script dans la ligne de commande du processus")

    # 6. Autre unité systemd (hors Fossor) : information, pas d'attribution.
    if own_unit and own_unit not in SYSTEM_UNITS:
        return _launcher("service hors Fossor", account, own_unit, unit=own_unit,
                         verified=True, reason=f"cgroup {own_unit}")

    # 7. Rien de sûr : bloqué avec la raison, jamais deviné.
    return _launcher(
        "manuel", account, f"processus {binary or names[0] if names else '?'}",
        verified=False,
        reason="aucun lanceur identifié (ni cron, ni script, ni unité systemd)",
    )


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def mask_args(args: str) -> str:
    """argv destiné à l'UI et au rapport : secrets ``•••``, wallets tronqués."""
    return mask_command(args, max_len=600)


def _wallet_path(datadir: str) -> str:
    return (datadir.rstrip("/") + "/wallet.dat") if datadir else ""


def _cli_for(binary: str) -> str:
    base = (binary or "").split("/")[-1]
    return (base[:-1] + "-cli") if base.endswith("d") else (base + "-cli")


def _cli_path(binary: str) -> str:
    """CLI du même répertoire que le démon, en chemin absolu."""
    if not binary.startswith("/"):
        return ""
    return binary.rsplit("/", 1)[0] + "/" + _cli_for(binary)


def _version_from_path(binary: str) -> str:
    """Version lisible dans le chemin (``…/srbminer/1.9.4/srbminer``)."""
    for token in str(binary or "").split("/"):
        match = re.match(r"^v?(\d+\.\d+(?:\.\d+)?)$", token)
        if match:
            return match.group(1)
    return ""


def profile_for(miner: dict[str, Any]) -> dict[str, Any]:
    """Profil Fossor proposé, au plus près de ce qui tourne déjà."""
    solo = str(miner.get("kind") or "") == "solo"
    software = str(miner.get("name") or miner.get("miner_name") or "miner").lower()
    coin = str(miner.get("coin") or "").upper()
    collected = str(miner.get("version") or "").lstrip("vV")
    source = "collecte" if re.match(r"^\d+\.\d+", collected) else ""
    version = collected if source else _version_from_path(str(miner.get("binary") or ""))
    if version and not source:
        source = "chemin"
    if not re.match(r"^\d+\.\d+", version or ""):
        version, source = "0.0.0", "inconnue"
    profile: dict[str, Any] = {
        "name": f"{coin or 'Solo'} adopté" if solo else f"{miner.get('name') or software} adopté",
        "software": software,
        "version": version,
        "version_source": source,
        "coin": coin,
        "pool": "" if solo else str(miner.get("pool") or ""),
        "algorithm": str(miner.get("algorithm") or ""),
        "threads": int(miner.get("threads") or 0) or None,
        "tty_wrapper": False,
        "solo": solo,
        "adopted": True,
    }
    if solo:
        profile["node_binary"] = str(miner.get("binary") or miner.get("daemon") or "")
        profile["datadir"] = str(miner.get("datadir") or "")
        profile["cli"] = str(miner.get("cli") or "")
    return profile


def unit_for(profile: dict[str, Any], argv: Sequence[str], *, user: str, cwd: str = "") -> dict[str, Any]:
    """Unité systemd proposée : même argv absolu, même compte que le processus."""
    name = f"{UNIT_PREFIX}{slug(str(profile['name']))}.service"
    exec_start = " ".join(_sq(token) for token in argv)
    description = (
        f"Fossor miner {slug(str(profile['name']))} ({profile.get('coin') or '?'}), adopté hors Fossor"
    )
    lines = [
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={user}",
    ]
    if cwd:
        lines.append(f"WorkingDirectory={cwd}")
    lines += [
        f"ExecStart={exec_start}",
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
    return {
        "name": name,
        "path": f"/etc/systemd/system/{name}",
        "user": user,
        "content": "\n".join(lines),
        "exec_start": exec_start,
    }


def build_item(
    miner: dict[str, Any],
    *,
    raw_args: str,
    host: dict[str, Any],
    account: str,
    launcher: dict[str, Any],
    os_kind: str = "",
    proc: dict[str, Any] | None = None,
    units: Sequence[str] = (),
    wallet: dict[str, Any] | None = None,
    cli_ok: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Une entrée du plan : processus, lancement prouvé, profil et unité."""
    solo = str(miner.get("kind") or "") == "solo"
    proc = dict(proc or {})
    cli_ok = dict(cli_ok or {})
    argv = [token for token in shlex.split(raw_args or "") if token]
    exe = str(proc.get("exe") or "")
    cwd = str(proc.get("cwd") or "")
    binary = exe if exe.startswith("/") else (argv[0] if argv and argv[0].startswith("/") else "")
    if binary and argv:
        argv[0] = binary
    datadir = str(miner.get("datadir") or "")
    wallet_path = _wallet_path(datadir) if solo else ""
    wallet_info = dict(wallet or {})
    wallet_present = bool(wallet_info.get("present")) if wallet_info else False
    wallet_size = int(wallet_info.get("size") or 0) if wallet_present else 0
    cli = _cli_path(binary) if solo else ""
    profile = profile_for({**miner, "binary": binary, "cli": cli})
    # Le compte d'origine pilote l'unité ; fossor n'intervient que par sudo.
    unit_user = account
    workdir = datadir if solo else (cwd if cwd.startswith("/") else (binary.rsplit("/", 1)[0] if "/" in binary else "/"))
    unit = unit_for(profile, argv, user=unit_user, cwd=workdir)
    # Revue : l'unité montrée à l'opérateur ne contient jamais le mot de passe,
    # mais garde le chemin absolu du binaire réel.
    review_argv = mask_args(raw_args).split()
    if review_argv and binary:
        review_argv[0] = binary
    unit_review = unit_for(profile, review_argv, user=unit_user, cwd=workdir)

    warnings: list[str] = []
    blocked = ""
    mode = str(launcher.get("mode") or "")
    if not binary:
        blocked = "binaire non résolu en chemin absolu (readlink /proc/<pid>/exe)"
    elif not argv:
        blocked = "ligne de commande du processus illisible"
    elif mode == "service":
        blocked = f"déjà lancé par l'unité {launcher.get('unit') or launcher.get('detail')} : rien à adopter"
    elif not launcher.get("verified"):
        blocked = "lanceur incertain : " + str(launcher.get("reason") or "aucun lanceur identifié")
    elif solo and not datadir:
        blocked = "datadir illisible : impossible de sauvegarder le wallet"
    elif solo and not cli_ok.get(cli):
        blocked = f"CLI {cli or '?'} introuvable ou non exécutable dans le répertoire du démon"
    elif solo and not wallet_present:
        blocked = f"wallet.dat introuvable dans {datadir} : sauvegarde impossible"
    if unit["name"] in set(units):
        blocked = (
            f"l'unité existante {unit['name']} serait arrêtée ou modifiée : adoption refusée"
        )
    if profile["version_source"] == "inconnue":
        warnings.append(
            "version non détectée : profil proposé en 0.0.0, à renseigner dans le profil avant activation."
        )
    if os_kind == "hiveos":
        warnings.append(
            "HiveOS : vérifier que l'unité reste activée après reboot "
            "(systemctl is-enabled + persistance /etc/systemd/system)."
        )
    script = str(launcher.get("script") or "")
    if script.startswith("/hive"):
        warnings.append(
            f"lanceur HiveOS ({script}) : HiveOS peut le réappliquer au reboot ; "
            "le retour arrière restaure la copie datée."
        )
    return {
        "key": f"{miner.get('pid') or '?'}:{miner.get('name') or miner.get('miner_name') or '?'}",
        "pid": miner.get("pid"),
        "process": {
            "name": miner.get("name") or miner.get("miner_name") or "",
            "binary": binary,
            "version": miner.get("version") or "",
            "kind": miner.get("kind") or "gpu",
            "cpu_pct": miner.get("cpu_pct"),
            "cwd": cwd,
        },
        "account": account,
        "args_masked": mask_args(raw_args),
        "datadir": datadir,
        "wallet": wallet_path,
        "wallet_present": wallet_present,
        "wallet_size": wallet_size,
        "pool": miner.get("pool") or "",
        "threads": miner.get("threads") or 0,
        "api_port": miner.get("api_port"),
        "log": miner.get("log") or host.get("log") or "",
        "launcher": launcher,
        "solo": solo,
        "os_kind": os_kind,
        "adoptable": not blocked,
        "blocked_reason": blocked,
        "warnings": warnings,
        "profile": profile,
        "unit": unit_review,
        "_unit": unit,
        "verify_after_minutes": VERIFY_MINUTES,
    }


def _neutralize(item: dict[str, Any], when: str) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Copie datée puis neutralisation du ou des lanceurs, avant tout arrêt."""
    launcher = item.get("launcher") or {}
    mode = str(launcher.get("mode") or "")
    account = str(launcher.get("account") or item.get("account") or "")
    binary = str((item.get("process") or {}).get("binary") or "").split("/")[-1]
    commands: list[str] = []
    restores: list[dict[str, str]] = []
    labels: list[str] = []
    if mode in ("cron", "cron @reboot"):
        backup = f"/var/backups/fossor/cron-{account}-{when}.cron"
        detail = str(launcher.get("detail") or "")
        if detail:
            expr = "s|^(" + re.escape(detail) + ")|# fossor-adoption " + when + " : \\1|"
            commands += [
                "sudo -n mkdir -p /var/backups/fossor",
                f"sudo -n -u {_sq(account)} crontab -l | sudo -n tee {_sq(backup)} >/dev/null",
                f"sudo -n chmod 600 {_sq(backup)}",
                f"sudo -n -u {_sq(account)} crontab -l | sed {_sq(expr)} | sudo -n -u {_sq(account)} crontab -",
            ]
            restores.append({"kind": "cron", "account": account, "backup": backup, "target": ""})
            labels.append("crontab")
    script = _clean_path(str(launcher.get("script") or ""))
    if script and mode != "service hors Fossor" and binary:
        backup = f"/var/backups/fossor/script-{script.split('/')[-1]}-{when}"
        expr = "/" + re.escape(binary) + "/s/^/# fossor-adoption " + when + " : /"
        commands += [
            f"sudo -n mkdir -p /var/backups/fossor",
            f"sudo -n cp -p {_sq(script)} {_sq(backup)}",
            f"sudo -n sed -i -e {_sq(expr)} {_sq(script)}",
        ]
        restores.append({"kind": "script", "account": account, "backup": backup, "target": script})
        labels.append("script")
    if mode == "service hors Fossor" and launcher.get("unit"):
        unit = str(launcher["unit"])
        commands.append(f"sudo -n systemctl disable --now {_sq(unit)}")
        restores.append({"kind": "unit", "account": account, "backup": "", "target": unit})
        labels.append("unité " + unit)
    return commands, restores, labels


def pool_steps(item: dict[str, Any], *, when: str | None = None) -> list[dict[str, Any]]:
    """Séquence d'un mineur de pool : lanceur neutralisé **avant** l'arrêt."""
    when = when or stamp()
    unit = item.get("_unit") or item["unit"]
    binary = str((item.get("process") or {}).get("binary") or "")
    dest_dir = f"{INSTALL_ROOT}/{item['profile']['software']}/{item['profile']['version']}"
    dest = f"{dest_dir}/{binary.split('/')[-1]}" if binary else ""
    pid = int(item.get("pid") or 0)
    commands, restores, labels = _neutralize(item, when)
    neutralize: dict[str, Any]
    if commands:
        neutralize = {
            "key": "neutralize",
            "label": "lanceur neutralisé avant l'arrêt (" + ", ".join(labels) + "), copie datée conservée",
            "command": " && ".join(commands),
            "backup": restores[0]["backup"] if restores else "",
            "restores": restores,
        }
    else:
        neutralize = {
            "key": "neutralize",
            "label": "aucun lanceur automatique à neutraliser",
            "kind": "skip",
            "command": "",
            "backup": "",
            "restores": [],
        }
    persist = ""
    if item.get("os_kind") == "hiveos":
        persist = " && test \"$(systemctl is-enabled " + _sq(unit["name"]) + ")\" = enabled"
    return [
        {
            "key": "profile",
            "label": "création du profil Fossor",
            "kind": "db",
            "command": "",
        },
        {
            "key": "binary",
            "label": "binaire adopté et empreinte relevée",
            "command": _sudo(
                f"test -x {_sq(binary)} && install -d -m 0755 {_sq(dest_dir)} "
                f"&& cp -p {_sq(binary)} {_sq(dest)} && sha256sum {_sq(dest)}"
            ),
        },
        neutralize,
        {
            "key": "stop",
            "label": "arrêt propre du processus actuel (SIGTERM, attente de sortie)",
            "command": (
                f"kill -TERM {pid} && "
                f"for i in $(seq 1 {POOL_STOP_TIMEOUT}); do kill -0 {pid} 2>/dev/null || exit 0; sleep 1; done; exit 1"
            ),
        },
        {
            "key": "unit",
            "label": f"installation de l'unité sous le compte d'origine ({item.get('account')}), activation",
            "command": _sudo(
                f"cat > {_sq(unit['path'])} <<'FOSSOR_UNIT'\n{unit['content']}FOSSOR_UNIT\n"
                f"systemctl daemon-reload && systemctl enable --now {_sq(unit['name'])}"
                + persist
            ),
        },
        {
            "key": "verify",
            "label": f"vérification du hashrate dans {VERIFY_MINUTES} minutes",
            "kind": "deferred",
            "command": f"systemctl is-active {_sq(unit['name'])}",
            "after_minutes": VERIFY_MINUTES,
        },
    ]


def solo_steps(item: dict[str, Any], *, when: str | None = None) -> list[dict[str, Any]]:
    """Séquence d'un nœud solo : wallet, lanceur neutralisé, arrêt par CLI."""
    when = when or stamp()
    unit = item.get("_unit") or item["unit"]
    datadir = str(item["datadir"]).rstrip("/")
    wallet = item["wallet"]
    backup = f"{wallet}{WALLET_BACKUP_TAG}{when}"
    cli = item["profile"].get("cli") or ""
    pid = int(item.get("pid") or 0)
    stop_cmd = _sq(cli) + " -datadir=" + _sq(datadir) + " stop"
    commands, restores, labels = _neutralize(item, when)
    neutralize: dict[str, Any]
    if commands:
        neutralize = {
            "key": "neutralize",
            "label": "lanceur neutralisé avant l'arrêt (" + ", ".join(labels) + "), copie datée conservée",
            "command": " && ".join(commands),
            "backup": restores[0]["backup"] if restores else "",
            "restores": restores,
        }
    else:
        neutralize = {
            "key": "neutralize",
            "label": "aucun lanceur automatique à neutraliser",
            "kind": "skip",
            "command": "",
            "backup": "",
            "restores": [],
        }
    return [
        {
            "key": "wallet",
            "label": "sauvegarde du wallet.dat (copie datée, droits 600, taille et empreinte)",
            "command": (
                f"test -s {_sq(wallet)} && cp -p {_sq(wallet)} {_sq(backup)} && chmod 600 {_sq(backup)} "
                f"&& test \"$(stat -c %s {_sq(wallet)})\" = \"$(stat -c %s {_sq(backup)})\" "
                f"&& test \"$(sha256sum < {_sq(wallet)} | cut -d' ' -f1)\" = \"$(sha256sum < {_sq(backup)} | cut -d' ' -f1)\" "
                f"&& echo WALLET_BACKUP_OK"
            ),
            "backup": backup,
        },
        neutralize,
        {
            "key": "stop",
            "label": "arrêt par le CLI absolu du nœud, attente de la fin du processus",
            "command": (
                f"{stop_cmd} && for i in $(seq 1 {SOLO_TIMEOUT}); do kill -0 {pid} 2>/dev/null || exit 0; sleep 1; done; exit 1"
            ),
            "forbids": ["kill -9"],
        },
        {
            "key": "unit",
            "label": f"unité identique à la commande d'origine, compte d'origine ({item.get('account')}), même datadir",
            "command": _sudo(
                f"cat > {_sq(unit['path'])} <<'FOSSOR_UNIT'\n{unit['content']}FOSSOR_UNIT\n"
                f"systemctl daemon-reload && systemctl enable --now {_sq(unit['name'])}"
            ),
        },
        {
            "key": "verify",
            "label": "getblockchaininfo, hauteur qui progresse, getmininginfo",
            "kind": "deferred",
            "command": f"{_sq(cli)} -datadir={_sq(datadir)} getblockchaininfo",
            "after_minutes": VERIFY_MINUTES,
        },
    ]


def steps_for(item: dict[str, Any], *, when: str | None = None) -> list[dict[str, Any]]:
    return solo_steps(item, when=when) if item.get("solo") else pool_steps(item, when=when)


def rollback_for(item: dict[str, Any], *, when: str | None = None) -> list[dict[str, Any]]:
    """Retour arrière : unité arrêtée, ancien processus et lanceurs d'origine."""
    steps = steps_for(item, when=when)
    unit = item.get("_unit") or item["unit"]
    original = item["process"]["binary"]
    raw_argv = list(item.get("_original_argv") or ([original] if original else []))
    if item.get("solo") and raw_argv and "-daemon" not in raw_argv:
        raw_argv.append("-daemon")
    relaunch = " ".join(_sq(token) for token in raw_argv) if raw_argv else _sq(original)
    account = item["account"]
    cwd = str(item.get("datadir") or (item.get("process") or {}).get("cwd") or "")
    inner = (f"cd {_sq(cwd)} && " if cwd else "") + "exec " + relaunch
    actions: list[dict[str, Any]] = [
        {
            "key": "stop_unit",
            "label": "arrêt de l'unité Fossor adoptée",
            "command": _sudo(f"systemctl disable --now {_sq(unit['name'])} || true"),
        },
        {
            "key": "relaunch",
            "label": "relance de l'ancien processus, commande absolue, même compte",
            "command": _sudo(f"runuser -u {_sq(account)} -- sh -c {_sq(inner)}"),
        },
    ]
    neutralize = next((s for s in steps if s["key"] == "neutralize"), None)
    order = {"script": 0, "unit": 1, "cron": 2}
    for restore in sorted(
        (neutralize or {}).get("restores") or [],
        key=lambda entry: order.get(entry.get("kind"), 3),
    ):
        kind = restore.get("kind")
        backup = restore.get("backup") or ""
        if kind == "script":
            actions.append(
                {
                    "key": "restore_script",
                    "label": "script d'origine restauré depuis la copie datée",
                    "command": _sudo(f"cp -p {_sq(backup)} {_sq(restore.get('target') or '')}"),
                }
            )
        elif kind == "cron":
            actions.append(
                {
                    "key": "restore_cron",
                    "label": "crontab d'origine restaurée depuis la copie datée",
                    "command": f"sudo -n -u {_sq(restore.get('account') or account)} crontab {_sq(backup)}",
                }
            )
        elif kind == "unit":
            actions.append(
                {
                    "key": "restore_unit",
                    "label": "unité d'origine réactivée",
                    "command": _sudo(f"systemctl enable --now {_sq(restore.get('target') or '')}"),
                }
            )
    return actions


def touches_existing_unit(steps: Sequence[dict[str, Any]], units: Iterable[str]) -> str:
    """Une unité ``fossor-miner-*`` existante touchée par une étape → refus."""
    existing = sorted({str(u) for u in units or [] if str(u).startswith(UNIT_PREFIX)})
    for step in steps:
        command = str(step.get("command") or "")
        for unit in existing:
            if unit in command:
                return unit
    return ""


def height_of(text: str) -> int | None:
    match = re.search(r'"blocks"\s*:\s*(\d+)', text or "")
    return int(match.group(1)) if match else None


def hashrate_seen(text: str) -> bool:
    """Une ligne de hashrate réelle (``123.4 kH/s``), pas un zéro."""
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([kKmMgGtT]?H/s)", text or ""):
        value = float(match.group(1).replace(",", "."))
        if value > 0:
            return True
    return False


def verify_solo(snapshot_before: str, snapshot_after: str) -> dict[str, Any]:
    before, after = height_of(snapshot_before), height_of(snapshot_after)
    ok = after is not None and (before is None or after >= before)
    return {"ok": ok, "height_before": before, "height_after": after}


# --------------------------------------------------------------------------- #
# Exécution (utilisée par la mission d'exécution, jamais à la construction)
# --------------------------------------------------------------------------- #
async def apply(
    runner,
    item: dict[str, Any],
    *,
    when: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Joue la séquence ; en cas d'échec, joue le retour arrière.

    ``dry_run`` reste vrai par défaut : la conception n'exécute rien.
    """
    if not item.get("adoptable"):
        raise AdoptionError(item.get("blocked_reason") or "adoption impossible")
    steps = steps_for(item, when=when)
    done: list[str] = []
    for step in steps:
        if step.get("kind") in ("db", "deferred", "skip"):
            done.append(step["key"])
            continue
        if dry_run:
            done.append(step["key"])
            continue
        status, out, err = await runner(step["command"])
        if status != 0:
            rolled = await rollback(runner, item, when=when)
            return {
                "ok": False,
                "failed_step": step["key"],
                "detail": (err or out or "")[:400],
                "done": done,
                "rollback": rolled,
            }
        done.append(step["key"])
    return {"ok": True, "done": done, "pending_verification": VERIFY_MINUTES, "dry_run": dry_run}


async def rollback(runner, item: dict[str, Any], *, when: str | None = None) -> dict[str, Any]:
    actions = rollback_for(item, when=when)
    played: list[str] = []
    for action in actions:
        status, _out, _err = await runner(action["command"])
        played.append(action["key"])
        if status != 0 and action["key"] == "relaunch":
            return {"ok": False, "played": played, "error": "relance de l'ancien processus impossible"}
    return {"ok": True, "played": played}


def finish_adoption(db, machine, item: dict[str, Any]) -> None:
    """Bascule d'occupation après succès : ``fossor_default``, minage inchangé."""
    from app.devices import mark_external_devices

    kept = [
        entry
        for entry in _machine_miners(machine)
        if str(entry.get("pid")) != str(item.get("pid")) or entry.get("name") != item["process"]["name"]
    ]
    machine.external_miners = _json(kept)
    devices = getattr(machine, "devices", []) or []
    for device in devices:
        if device.occupied_external:
            device.occupied_external = False
            device.occupied_by = "fossor_default"
    db.commit()


def _machine_miners(machine) -> list[dict[str, Any]]:
    import json

    try:
        value = json.loads(machine.external_miners or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Sondes de l'hôte et plan complet (toutes en lecture seule)
# --------------------------------------------------------------------------- #
PROBE_PS = "ps -eo user,pid,ppid,pcpu,etimes,args --no-headers"
PROBE_UNITS = "systemctl list-unit-files 'fossor-miner-*' --no-legend --no-pager 2>/dev/null || true"


def _proc_command(pids: Sequence[str]) -> str:
    lines: list[str] = []
    for pid in pids:
        p = re.sub(r"[^0-9]", "", str(pid or ""))
        if not p:
            continue
        lines += [
            "echo 'PID " + p + "'",
            'echo "EXE $(readlink /proc/' + p + '/exe 2>/dev/null)"',
            'echo "CWD $(readlink /proc/' + p + '/cwd 2>/dev/null)"',
            'echo "UNIT $(awk -F: \'{print $3}\' /proc/' + p + '/cgroup 2>/dev/null | tail -1 | sed \'s|.*/||\')"',
            'echo "PPID $(awk \'/^PPid:/{print $2}\' /proc/' + p + '/status 2>/dev/null)"',
            'echo "CMD $(tr \'\\0\' \' \' < /proc/' + p + '/cmdline 2>/dev/null)"',
        ]
    return "sh -c " + _sq("\n".join(lines))


def _read_files_command(paths: Sequence[str]) -> str:
    lines: list[str] = []
    for raw in paths:
        path = _clean_path(raw)
        if not path:
            continue
        lines += [
            "echo " + _sq("FILE " + path),
            f"sudo -n cat {_sq(path)} 2>/dev/null || cat {_sq(path)} 2>/dev/null || true",
            "echo EOFILE",
        ]
    return "sh -c " + _sq("\n".join(lines))


def _search_command(names: Sequence[str]) -> str:
    wanted = [re.sub(r"[^A-Za-z0-9_.+-]", "", str(n)) for n in names if n]
    wanted = [n for n in wanted if len(n) >= 4]
    if not wanted:
        return ""
    args = " ".join("-e " + _sq(n) for n in wanted)
    dirs = " ".join(SEARCH_DIRS)
    return (
        f"grep -rls --binary-files=without-match {args} {dirs} 2>/dev/null | head -20 || true"
    )


def _probe_command(wallets: Sequence[str], clis: Sequence[str]) -> str:
    lines: list[str] = []
    for raw in clis:
        path = _clean_path(raw)
        if path:
            lines.append(f"if test -x {_sq(path)}; then echo {_sq('X ' + path)}; else echo {_sq('N ' + path)}; fi")
    for raw in wallets:
        path = _clean_path(raw)
        if path:
            lines.append(
                f"if test -f {_sq(path)}; then echo {_sq('W ' + path)} $(stat -c %s {_sq(path)} 2>/dev/null); "
                f"else echo {_sq('W ' + path)} -1; fi"
            )
    if not lines:
        return ""
    return "sh -c " + _sq("\n".join(lines))


async def read_host(runner, miners: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Lectures **seulement** : ps, écrans, unités, crontabs, /proc, scripts, CLI, wallets."""
    _status, ps_text, _err = await runner(PROBE_PS)
    _status, screens, _err = await runner("screen -ls 2>/dev/null || true")
    _status, units_text, _err = await runner(PROBE_UNITS)
    units = [line.split()[0] for line in (units_text or "").splitlines() if line.strip()]

    rows = parse_ps_tree(ps_text)
    by_pid = {str(row.get("pid")): row for row in rows}
    accounts: set[str] = set()
    for row in rows:
        accounts.add(str(row.get("user") or ""))
    for miner in miners or []:
        accounts.add(str(miner.get("user") or ""))
    crontabs: dict[str, str] = {}
    for account in sorted({a for a in accounts if a}):
        cleaned = re.sub(r"[^a-z0-9_.-]", "", str(account).lower())
        if not cleaned:
            continue
        _s, text, _e = await runner(
            f"sudo -n -u {cleaned} crontab -l 2>/dev/null || crontab -l 2>/dev/null || true"
        )
        crontabs[account] = text

    pids = [str(m.get("pid") or "") for m in miners or []]
    _s, proc_text, _e = await runner(_proc_command(pids))
    proc = parse_proc_info(proc_text)

    script_paths: set[str] = set()
    for text in crontabs.values():
        for entry in parse_crontab(text):
            path = _script_path(entry["command"])
            if path:
                script_paths.add(path)
    names: list[str] = []
    wallets: list[str] = []
    clis: list[str] = []
    for miner in miners or []:
        pid = str(miner.get("pid") or "")
        facts = proc.get(pid, {})
        exe = str(facts.get("exe") or "")
        binary = exe.split("/")[-1] if exe else str(miner.get("binary") or "").split("/")[-1]
        if binary:
            names.append(binary)
        if str(miner.get("kind")) == "solo":
            raw_row = str((by_pid.get(pid) or {}).get("args") or facts.get("cmd") or "")
            datadir = str(miner.get("datadir") or "") or _datadir(raw_row)
            wallets.append(_wallet_path(datadir))
            clis.append(_cli_path(exe))
    search = _search_command(names)
    if search:
        _s, found, _e = await runner(search)
        for line in (found or "").splitlines():
            path = _clean_path(line.strip())
            if path:
                script_paths.add(path)
    scripts: dict[str, str] = {}
    if script_paths:
        _s, files, _e = await runner(_read_files_command(sorted(script_paths)))
        scripts = parse_files(files)
    probe = _probe_command(wallets, clis)
    cli_ok: dict[str, bool] = {}
    wallet_info: dict[str, dict[str, Any]] = {}
    if probe:
        _s, probed, _e = await runner(probe)
        cli_ok = parse_cli_ok(probed)
        wallet_info = parse_wallet_info(probed)
    return {
        "ps": ps_text,
        "tree": parse_ps_tree(ps_text),
        "screens": parse_screens(screens),
        "units": units,
        "crontabs": crontabs,
        "proc": proc,
        "scripts": scripts,
        "cli_ok": cli_ok,
        "wallets": wallet_info,
    }


def _enrich_solo(miner: dict[str, Any], raw_args: str, facts: dict[str, Any]) -> dict[str, Any]:
    argv = shlex.split(raw_args or "")
    exe = str(facts.get("exe") or "")
    binary = exe if exe.startswith("/") else (argv[0] if argv and argv[0].startswith("/") else "")
    datadir = str(miner.get("datadir") or "") or _datadir(raw_args)
    return {
        **miner,
        "binary": binary,
        "datadir": datadir,
        "cli": _cli_path(binary),
    }


async def build_plan(runner, machine, miners: Sequence[dict[str, Any]], *, include_secrets: bool = False) -> dict[str, Any]:
    """Plan d'adoption en lecture seule. Aucune commande d'écriture."""
    miners = [dict(m) for m in (miners or [])]
    host = await read_host(runner, miners)
    tree = host["tree"]
    by_pid = {str(row["pid"]): row for row in tree}
    items: list[dict[str, Any]] = []
    for miner in miners:
        pid = str(miner.get("pid") or "")
        row = by_pid.get(pid, {})
        raw = str(row.get("args") or "") or str(miner.get("cmd") or "")
        account = str(row.get("user") or miner.get("user") or "")
        facts = dict(host["proc"].get(pid) or {})
        facts["pid"] = pid
        if str(miner.get("kind")) == "solo":
            miner = _enrich_solo(miner, raw, facts)
        hints = [
            str(miner.get("coin") or ""),
            str(miner.get("name") or "").split("-")[0],
        ]
        launcher = launcher_of(
            raw,
            account=account,
            crontabs=host["crontabs"],
            screens=host["screens"],
            units=host["units"],
            hints=[h for h in hints if h],
            proc=facts,
            tree=tree,
            scripts=host["scripts"],
        )
        wallet_path = _wallet_path(str(miner.get("datadir") or "")) if str(miner.get("kind")) == "solo" else ""
        item = build_item(
            miner,
            raw_args=raw,
            host={"log": miner.get("log") or ""},
            account=account,
            launcher=launcher,
            os_kind=str(getattr(machine, "os_kind", "") or ""),
            proc=facts,
            units=host["units"],
            wallet=host["wallets"].get(wallet_path) if wallet_path else None,
            cli_ok=host["cli_ok"],
        )
        conflict = touches_existing_unit(list(steps_for(item)) + list(rollback_for(item)), host["units"])
        if conflict and item["adoptable"]:
            item["adoptable"] = False
            item["blocked_reason"] = (
                f"le plan toucherait l'unité existante {conflict} : adoption refusée"
            )
        if include_secrets:
            # Jamais renvoyés par l'API : servent uniquement à l'exécution.
            item["_original_argv"] = shlex.split(raw) if raw else []
        else:
            item.pop("_unit", None)
        items.append(item)
    return {
        "machine": {
            "id": getattr(machine, "id", None),
            "name": getattr(machine, "name", ""),
            "host": getattr(machine, "host", ""),
            "os_kind": getattr(machine, "os_kind", ""),
        },
        "miners": items,
        "read_only": True,
    }
