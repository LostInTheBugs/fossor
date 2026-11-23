"""Adoption des mineurs lancés hors Fossor (§6.7).

Un mineur « lancé hors Fossor » (nœud solo, wam-miner sous ``screen``, script
`@reboot`) reste visible mais non pilotable.  Ce module construit un **plan
d'adoption** en lecture seule, puis la séquence d'adoption et son retour
arrière.  Aucune commande n'est exécutée à la construction du plan.

Règles de sécurité
------------------
* les secrets et les wallets ne sortent jamais : ``mask_command`` masque les
  mots de passe et tronque les wallets, y compris dans les argv proposés ;
* un **nœud solo** n'est arrêté que par ``<coin>-cli -datadir=… stop`` ; jamais
  de ``kill -9``, jamais de signal direct au démon qui porte le wallet ;
* le ``wallet.dat`` est **copié** (droits 600) et vérifié (taille + sha256)
  avant l'arrêt ; son contenu n'est ni lu, ni affiché, ni déplacé ;
* le datadir, le binaire et les options du nœud sont **inchangés** ; l'unité
  lance exactement la même commande, sous le **même compte** ;
* un lancement automatique est **commenté**, jamais supprimé, après une copie
  datée de la crontab ou du script.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from app.external_miners import mask_command, truncate_wallet
from app.miners import slug

UNIT_PREFIX = "fossor-miner-"
INSTALL_ROOT = "/opt/fossor/miners"
VERIFY_MINUTES = 5
CRON_BACKUP_TAG = ".fossor-backup-"
WALLET_BACKUP_TAG = ".fossor-backup-"
SOLO_TIMEOUT = 120
POOL_STOP_TIMEOUT = 30


class AdoptionError(Exception):
    """Le plan ne peut pas être exécuté en l'état."""


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def _sq(value: str) -> str:
    return shlex.quote(str(value))


def _sudo(inner: str) -> str:
    return f"sudo -n sh -c {_sq(inner)}"


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


def launcher_of(
    args: str,
    *,
    account: str,
    crontabs: dict[str, str] | None = None,
    screens: Sequence[str] = (),
    units: Sequence[str] = (),
    hints: Sequence[str] = (),
) -> dict[str, Any]:
    """Mode de lancement actuel : cron ``@reboot``, screen, script, service, main.

    Le cron est cherché pour **tous** les comptes lus (un ``@reboot`` du compte
    administrateur lance un script qui relance ``root`` : c'est le cas Ryzen).
    """
    script = _script_path(args)
    tokens = shlex.split(str(args or ""))
    binary = (tokens or [""])[0].split("/")[-1]
    needles = [n for n in (binary, script, (tokens or [""])[-1], *hints) if n]
    for owner, text in (crontabs or {}).items():
        for entry in parse_crontab(text):
            haystack = entry["command"] + " " + script
            if any(needle and needle in haystack for needle in needles):
                return {
                    "mode": "cron @reboot" if entry["reboot"] else "cron",
                    "account": owner,
                    "detail": entry["line"],
                    "schedule": entry["schedule"],
                    "script": script,
                    "backup": None,
                }
    for session in screens:
        if any(needle and needle in session for needle in needles) or script:
            return {
                "mode": "screen",
                "account": account,
                "detail": session,
                "script": script,
            }
    if script:
        return {"mode": "script", "account": account, "detail": script, "script": script}
    if any(unit.startswith(UNIT_PREFIX) for unit in units):
        return {"mode": "service", "account": account, "detail": ", ".join(units)}
    return {"mode": "manuel", "account": account, "detail": f"processus {binary}"}


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def mask_args(args: str) -> str:
    """argv destiné à l'UI et au rapport : secrets ``•••``, wallets tronqués."""
    return mask_command(args, max_len=600)


def _wallet_path(datadir: str) -> str:
    return (datadir.rstrip("/") + "/wallet.dat") if datadir else ""


def profile_for(miner: dict[str, Any]) -> dict[str, Any]:
    """Profil Fossor proposé, au plus près de ce qui tourne déjà."""
    solo = str(miner.get("kind") or "") == "solo"
    software = str(miner.get("name") or miner.get("miner_name") or "miner").lower()
    coin = str(miner.get("coin") or "").upper()
    version = str(miner.get("version") or "0.0.0").lstrip("vV") or "0.0.0"
    if not re.match(r"^\d+\.\d+", version):
        version = "0.0.0"
    profile: dict[str, Any] = {
        "name": f"{coin or 'Solo'} adopté" if solo else f"{miner.get('name') or software} adopté",
        "software": software,
        "version": version,
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
    """Unité systemd proposée : même argv, même compte pour un nœud solo."""
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
) -> dict[str, Any]:
    """Une entrée du plan : processus, lancement, profil et unité proposés."""
    solo = str(miner.get("kind") or "") == "solo"
    argv = shlex.split(raw_args or "")
    binary = argv[0] if argv else str(miner.get("binary") or "")
    datadir = str(miner.get("datadir") or "")
    profile = profile_for({**miner, "binary": binary})
    # Un nœud solo garde son compte : le datadir lui appartient.
    unit_user = account if solo else "fossor"
    cwd = datadir or "/"
    unit = unit_for(profile, argv, user=unit_user, cwd=cwd)
    # Revue : l'unité montrée à l'opérateur ne contient jamais le mot de passe.
    unit_review = unit_for(profile, mask_args(raw_args).split(), user=unit_user, cwd=cwd)
    warnings: list[str] = []
    blocked = ""
    if solo:
        if not datadir:
            blocked = "datadir illisible : impossible de sauvegarder le wallet"
        elif unit_user != account:
            blocked = f"l'unité exigerait un autre compte ({unit_user} ≠ {account})"
        elif not _wallet_path(datadir):
            blocked = "wallet.dat introuvable"
        if os_kind == "hiveos":
            warnings.append(
                "HiveOS : vérifier que l'unité reste activée après reboot "
                "(systemctl is-enabled + persistance /etc/systemd/system)."
            )
    else:
        if not binary:
            blocked = "binaire illisible"
        if not profile["version"] or profile["version"] == "0.0.0":
            warnings.append("version non lisible : profil proposé en 0.0.0, à corriger avant lancement.")
    return {
        "key": f"{miner.get('pid') or '?'}:{miner.get('name') or miner.get('miner_name') or '?'}",
        "pid": miner.get("pid"),
        "process": {
            "name": miner.get("name") or miner.get("miner_name") or "",
            "binary": binary,
            "version": miner.get("version") or "",
            "kind": miner.get("kind") or "gpu",
            "cpu_pct": miner.get("cpu_pct"),
        },
        "account": account,
        "args_masked": mask_args(raw_args),
        "datadir": datadir,
        "wallet": _wallet_path(datadir) if solo else "",
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


def pool_steps(item: dict[str, Any], *, when: str | None = None) -> list[dict[str, Any]]:
    """Séquence d'adoption d'un mineur de pool, dans l'ordre exigé."""
    when = when or stamp()
    unit = item.get("_unit") or item["unit"]
    binary = item["process"]["binary"]
    dest_dir = f"{INSTALL_ROOT}/{item['profile']['software']}/{item['profile']['version']}"
    dest = f"{dest_dir}/{binary.split('/')[-1]}"
    account = item["account"]
    cron = item.get("launcher") or {}
    cron_account = cron.get("account") or account
    backup = f"/var/backups/fossor/cron-{cron_account}-{when}.cron"
    sed_expr = "s|^(" + re.escape(str(cron.get("detail") or "")) + ")|# fossor-adoption " + when + " : \\1|"
    persist = ""
    if item.get("os_kind") == "hiveos":
        persist = " && test \"$(systemctl is-enabled " + _sq(unit["name"]) + ")\" = enabled"
    steps = [
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
                f"install -d -m 0755 {_sq(dest_dir)} && cp -p {_sq(binary)} {_sq(dest)} "
                f"&& sha256sum {_sq(dest)}"
            ),
        },
        {
            "key": "stop",
            "label": "arrêt propre du processus actuel (SIGTERM, attente de sortie)",
            "command": (
                f"kill -TERM {int(item['pid'])} && "
                f"for i in $(seq 1 {POOL_STOP_TIMEOUT}); do kill -0 {int(item['pid'])} 2>/dev/null || exit 0; sleep 1; done; exit 1"
            ),
        },
        {
            "key": "neutralize",
            "label": "lancement automatique commenté, copie datée conservée",
            "command": (
                f"sudo -n mkdir -p /var/backups/fossor && "
                f"sudo -n -u {_sq(cron_account)} crontab -l | sudo -n tee {_sq(backup)} >/dev/null && "
                f"sudo -n chmod 600 {_sq(backup)} && "
                f"sudo -n -u {_sq(cron_account)} crontab -l | "
                f"sed {_sq(sed_expr)} | "
                f"sudo -n -u {_sq(cron_account)} crontab -"
            ),
            "backup": backup,
            "requires": "launcher=cron",
        },
        {
            "key": "unit",
            "label": "installation de l'unité sous le compte fossor, activation",
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
    if (cron.get("mode") or "") not in ("cron", "cron @reboot"):
        steps[3] = {
            "key": "neutralize",
            "label": "lancement automatique commenté, copie datée conservée",
            "kind": "skip",
            "command": "",
            "backup": "",
        }
    return steps


def solo_steps(item: dict[str, Any], *, when: str | None = None) -> list[dict[str, Any]]:
    """Séquence d'un nœud solo : wallet sauvegardé, arrêt par CLI uniquement."""
    when = when or stamp()
    unit = item.get("_unit") or item["unit"]
    datadir = item["datadir"].rstrip("/")
    wallet = item["wallet"]
    backup = f"{wallet}{WALLET_BACKUP_TAG}{when}"
    cli = item["profile"].get("cli") or ""
    pid = item["pid"]
    if not cli:
        cli = f"{item['profile'].get('node_binary') or ''}-cli".replace("d-cli", "-cli")
    stop_cmd = _sq(cli) + " -datadir=" + _sq(datadir) + " stop"
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
        {
            "key": "stop",
            "label": "arrêt par le CLI du nœud, attente de la fin du processus",
            "command": (
                f"{stop_cmd} && for i in $(seq 1 {SOLO_TIMEOUT}); do kill -0 {int(pid)} 2>/dev/null || exit 0; sleep 1; done; exit 1"
            ),
            "forbids": ["kill -9"],
        },
        {
            "key": "unit",
            "label": "unité identique à la commande d'origine, même compte, même datadir",
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
    """Retour arrière : unité arrêtée, ancien processus et lancement d'origine."""
    steps = steps_for(item, when=when)
    unit = item.get("_unit") or item["unit"]
    original = item["process"]["binary"]
    raw_argv = item.get("_original_argv") or []
    relaunch = " ".join(_sq(token) for token in raw_argv) if raw_argv else _sq(original)
    account = item["account"]
    actions: list[dict[str, Any]] = [
        {
            "key": "stop_unit",
            "label": "arrêt de l'unité Fossor",
            "command": _sudo(f"systemctl disable --now {_sq(unit['name'])} || true"),
        }
    ]
    if item.get("solo"):
        actions.append(
            {
                "key": "relaunch",
                "label": "relance du nœud d'origine, même commande, même compte",
                "command": _sudo(f"runuser -u {_sq(account)} -- sh -c {_sq(relaunch + ' -daemon')}"),
            }
        )
    else:
        actions.append(
            {
                "key": "relaunch",
                "label": "relance de l'ancien processus, même commande, même compte",
                "command": _sudo(f"runuser -u {_sq(account)} -- sh -c {_sq(relaunch)}"),
            }
        )
    neutralize = next((s for s in steps if s["key"] == "neutralize"), None)
    if neutralize and neutralize.get("backup"):
        actions.append(
            {
                "key": "restore_cron",
                "label": "lancement d'origine rétabli depuis la copie datée",
                "command": f"sudo -n -u {_sq(neutralize.get('account') or item['account'])} crontab {_sq(neutralize['backup'])}",
            }
        )
    return actions


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
# Lecture de l'hôte et plan complet
# --------------------------------------------------------------------------- #
PROBE_PS = "ps -eo user,pid,pcpu,etimes,args --no-headers"
PROBE_UNITS = "systemctl list-unit-files 'fossor-miner-*' --no-legend --no-pager 2>/dev/null || true"


def _cli_for(binary: str) -> str:
    base = (binary or "").split("/")[-1]
    return (base[:-1] + "-cli") if base.endswith("d") else (base + "-cli")


def _enrich_solo(miner: dict[str, Any], raw_args: str) -> dict[str, Any]:
    from app.external_miners import _datadir

    argv = shlex.split(raw_args or "")
    binary = argv[0] if argv else ""
    datadir = _datadir(raw_args)
    return {
        **miner,
        "binary": binary,
        "datadir": datadir,
        "cli": _cli_for(binary) if binary else "",
    }


async def read_host(runner, accounts: Iterable[str]) -> dict[str, Any]:
    """Trois lectures **seulement** : ps, crontabs des comptes vus, unités."""
    _status, ps_text, _err = await runner(PROBE_PS)
    _status, screens, _err = await runner("screen -ls 2>/dev/null || true")
    _status, units, _err = await runner(PROBE_UNITS)
    crontabs: dict[str, str] = {}
    for account in sorted({a for a in accounts if a}):
        cleaned = re.sub(r"[^a-z0-9_.-]", "", str(account).lower())
        if not cleaned:
            continue
        _s, text, _e = await runner(
            f"sudo -n -u {cleaned} crontab -l 2>/dev/null || crontab -l 2>/dev/null || true"
        )
        crontabs[account] = text
    return {
        "ps": ps_text,
        "screens": parse_screens(screens),
        "units": [line.split()[0] for line in (units or "").splitlines() if line.strip()],
        "crontabs": crontabs,
    }


async def build_plan(runner, machine, miners: Sequence[dict[str, Any]], *, include_secrets: bool = False) -> dict[str, Any]:
    """Plan d'adoption en lecture seule. Aucune commande d'écriture."""
    host = await read_host(runner, [m.get("user") for m in miners])
    from app.external_miners import parse_ps_rows

    rows = parse_ps_rows(host["ps"])
    by_pid = {str(row["pid"]): row for row in rows if row.get("pid")}
    items: list[dict[str, Any]] = []
    for miner in miners or []:
        pid = str(miner.get("pid") or "")
        row = by_pid.get(pid, {})
        raw = str(row.get("args") or "")
        account = str(row.get("user") or miner.get("user") or "")
        if str(miner.get("kind")) == "solo":
            if raw:
                miner = _enrich_solo(miner, raw)
            if not miner.get("datadir"):
                match = re.search(r"-datadir[= ](\S+)", str(miner.get("cmd") or ""))
                if match:
                    miner = {**miner, "datadir": match.group(1), "cli": _cli_for(str(miner.get("binary") or ""))}
        elif not raw:
            raw = str(miner.get("cmd") or "")
        hints = [
            str(miner.get("coin") or ""),
            str(miner.get("name") or "").split("-")[0],
            str(miner.get("datadir") or "").split("/")[-1].lstrip("."),
        ]
        launcher = launcher_of(
            raw,
            account=account,
            crontabs=host["crontabs"],
            screens=host["screens"],
            units=host["units"],
            hints=[h for h in hints if h],
        )
        item = build_item(
            miner,
            raw_args=raw,
            host={"log": miner.get("log") or ""},
            account=account,
            launcher=launcher,
            os_kind=str(getattr(machine, "os_kind", "") or ""),
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
