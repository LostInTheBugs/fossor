"""Pilotage compatible HiveOS (§4.1 ter, §4.6, §4.7) — analyse et plans purs.

Sur un rig ``os_kind=hiveos``, le minage du token par défaut est lancé par
HiveOS (flight sheet → ``miner``), avec son watchdog et l'overclocking de la
flight sheet.  Fossor ne le lançait donc jamais : ce module lit la flight sheet
et construit les commandes HiveOS d'arrêt et de reprise.

Aucun secret ne sort d'ici : ``RIG_PASSWD`` (et toute clé dont le nom évoque un
mot de passe, un jeton ou une clé d'API) est remplacé par :data:`MASKED`, comme
``params.passwd`` de ``last_stat.json``.  Le wallet est masqué de la même façon :
la base ne garde jamais une adresse en clair pour HiveOS.

Aucun I/O : les commandes sont des ``Step`` (argv, jamais une chaîne de shell
construite depuis une entrée) et les plans sont rendus tels quels en simulation.

Sources publiques (comportement vérifiable, jamais un relevé sur le rig de Fred) :

* ``miner start`` / ``miner stop`` pilotent le mineur de la flight sheet —
  <https://hiveon.com/knowledge-base/guides/linux/> (« miner start, miner stop
  — start or stop currently configured miner ») ;
* le watchdog HiveOS relance le mineur puis redémarre le rig (2 min, puis seuil
  de hashrate) : <https://happymining.fr/blogs/infos/monter-son-kit-de-minage> ;
* un ``miner stop`` volontaire **ne suffit pas** : le watchdog de hashrate a
  relancé le mineur puis redémarré le rig, et le script ``hive`` montre
  ``miner start`` suivi de ``[[ $WD_ENABLED == 1 ]] && wd start`` (mode
  maintenance : ``MAINTENANCE=1``) — <https://hiveon.com/forum/t/hashrate-watchdog-issues/11470>.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

MASKED = "•••"

#: Clés de ``wallet.conf`` / ``rig.conf`` qui ne doivent jamais ressortir.
SECRET_MARKERS = (
    "PASSWD",
    "PASSWORD",
    "PASS",
    "PSW",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "PRIVATE",
)
WALLET_KEYS = ("WALLET", "WALLET_ADDRESS", "WALLET_ADDR", "ADDRESS")

#: Message exact du refus de campagne partielle (brief 3e).
PARTIAL_REFUSAL = (
    "HiveOS : campagne partielle impossible, tous les GPU du mineur HiveOS "
    "seraient arrêtés — engagez tous les GPU du rig ou retirez-le de la campagne."
)

WATCHDOG_REFUSAL = (
    "watchdog HiveOS actif (WD_ENABLED=1 dans rig.conf) : il peut relancer le mineur "
    "ou redémarrer le rig pendant la campagne. Refus de lancer sans l'option explicite "
    "« arrêter le watchdog HiveOS » (réglage hiveos_watchdog_ack) ; rig.conf n'est jamais "
    "modifié par Fossor."
)

WATCHDOG_PARADE = (
    "arrêt temporaire par « wd stop » au début, « wd start » à la fin (option explicite), "
    "ou passage du rig en maintenance depuis HiveOS cloud ; rig.conf n'est jamais écrit."
)


class HiveosError(RuntimeError):
    """Refus ou échec HiveOS — message utilisable tel quel par l'opérateur."""


@dataclass(frozen=True)
class Step:
    """Une commande HiveOS, en argv (jamais de chaîne assemblée)."""

    title: str
    argv: tuple[str, ...]
    allow_fail: bool = False
    timeout: int = 60

    def command(self) -> str:
        from shlex import join

        return join(self.argv)


# ── Analyse des fichiers HiveOS ────────────────────────────────────────
def _sections(text: str) -> dict[str, str]:
    """Découpe une sortie de sonde sur les marqueurs ``### NOM``.

    Un fichier HiveOS n'a pas toujours de saut de ligne final (``last_stat.json``
    écrit par ``json.dump``) : le marqueur suivant colle alors au JSON précédent
    (``…}}}### HIVE_GPU``).  On découpe donc sur le motif lui-même, pas seulement
    en début de ligne — sinon la section GPU disparaît et le hashrate reste vide.
    """
    parts = re.split(r"###\s+([A-Z][A-Z0-9_]*)[ \t]*", text or "")
    out: dict[str, str] = {}
    for index in range(1, len(parts) - 1, 2):
        out[parts[index]] = parts[index + 1].strip()
    return out


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def is_secret_key(key: str) -> bool:
    upper = (key or "").upper()
    if upper in WALLET_KEYS:
        return True
    return any(marker in upper for marker in SECRET_MARKERS)


def mask_conf_value(key: str, value: str) -> str:
    return MASKED if is_secret_key(key) else _unquote(value)


def parse_conf(text: str) -> dict[str, str]:
    """Lit un ``*.conf`` HiveOS (``KEY="valeur"``) en masquant les secrets.

    La **clé** est conservée pour prouver sa présence (``RIG_PASSWD`` présent
    mais masqué), la valeur ne sort jamais.
    """
    values: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        values[key] = mask_conf_value(key, value)
    return values


def parse_cur_miner(text: str) -> str:
    for line in (text or "").splitlines():
        name = line.strip()
        if name:
            return name
    return ""


def parse_listing(text: str) -> list[str]:
    """Noms d'un ``ls -1`` : ignore le bruit de ``sudo`` et les vides."""
    names: list[str] = []
    for line in (text or "").splitlines():
        name = line.strip()
        if not name or name.startswith(("total ", "ls:", "sudo:")):
            continue
        names.append(name)
    return names


def parse_status(text: str) -> dict[str, Any]:
    """Interprète ``miner status`` ; ``None`` quand le texte ne tranche pas."""
    raw = (text or "").strip()
    low = raw.lower()
    running: bool | None = None
    if "not running" in low or "is stopped" in low or low.endswith(" stopped"):
        running = False
    elif "running" in low or "mining" in low:
        running = True
    return {"running": running, "text": raw[:400]}


def parse_last_stat(text: str) -> dict[str, Any]:
    """Lit ``/run/hive/last_stat.json`` sans jamais exposer ``params.passwd``."""
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return {"ok": False, "hashrate_hs": None, "miner": "", "coin": "", "algo": ""}
    if not isinstance(data, dict):
        return {"ok": False, "hashrate_hs": None, "miner": "", "coin": "", "algo": ""}
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    meta = params.get("meta") if isinstance(params.get("meta"), dict) else {}
    miner = str(params.get("miner") or "")
    coin = algo = ""
    entry = meta.get(miner) if isinstance(meta.get(miner), dict) else None
    if entry is None:
        for value in meta.values():
            if isinstance(value, dict) and (value.get("coin") or value.get("algo")):
                entry = value
                break
    if isinstance(entry, dict):
        coin = str(entry.get("coin") or "")
        algo = str(entry.get("algo") or "")
    total_khs = params.get("total_khs")
    try:
        hashrate = float(total_khs) if total_khs is not None else None
    except (TypeError, ValueError):
        hashrate = None
    stats = params.get("miner_stats") if isinstance(params.get("miner_stats"), dict) else {}
    hs = stats.get("hs") if isinstance(stats.get("hs"), list) else []
    return {
        "ok": True,
        "miner": miner,
        "coin": coin,
        "algo": algo,
        "hashrate_hs": hashrate,
        "mining_gpus": len(hs),
        "miner_status": str(stats.get("status") or ""),
    }


def parse_gpu_stats(text: str) -> dict[str, Any]:
    """Compte les GPU (``brand`` sans ``cpu``) de ``gpu-stats.json``."""
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return {"gpu_total": 0, "busids": []}
    if not isinstance(data, dict):
        return {"gpu_total": 0, "busids": []}
    brands = data.get("brand") if isinstance(data.get("brand"), list) else []
    busids = data.get("busids") if isinstance(data.get("busids"), list) else []
    gpu_total = sum(1 for brand in brands if str(brand).lower() != "cpu")
    return {"gpu_total": gpu_total, "busids": [str(b) for b in busids]}


def parse_processes(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0].lower() == "pid":
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows.append({"pid": pid, "args": parts[2]})
    return rows


def parse_probe(text: str) -> dict[str, Any]:
    """Analyse complète d'une sortie de sonde ``hiveos_probe``."""
    sections = _sections(text)
    wallet = parse_conf(sections.get("WALLET_CONF", ""))
    rig = parse_conf(sections.get("RIG_CONF", ""))
    root = sections.get("HIVE_ROOT", "")
    last_stat = parse_last_stat(sections.get("HIVE_STAT", ""))
    gpu_stats = parse_gpu_stats(sections.get("HIVE_GPU", ""))
    processes = parse_processes(sections.get("MINER_PS", ""))
    cur_miner = parse_cur_miner(sections.get("CUR_MINER", ""))
    miner_dirs = parse_listing(sections.get("MINER_DIRS", ""))
    custom = parse_listing(sections.get("CUSTOM", ""))
    status = parse_status(sections.get("MINER_STATUS", ""))
    wd_status = (sections.get("WD_STATUS", "") or "").strip()[:200]
    miner_bin = ""
    for line in root.splitlines():
        if line.startswith("MINER_BIN="):
            miner_bin = line.split("=", 1)[1].strip()
    flight_miner = (
        cur_miner
        or str(wallet.get("MINER") or "")
        or str(last_stat.get("miner") or "")
        or (miner_dirs[0] if miner_dirs else "")
    )
    custom_dir = "CUSTOM_DIR=1" in root
    detector = "HIVEOS=1" in root or bool(wallet or rig)
    return {
        "detected": detector,
        "miner_bin": miner_bin or "/hive/bin/miner",
        "custom_dir": custom_dir,
        "custom_miners": custom,
        "wallet": {
            "miner": str(wallet.get("MINER") or ""),
            "coin": str(wallet.get("COIN") or ""),
            "has_wallet": "WALLET" in wallet,
            "wallet": wallet.get("WALLET", MASKED),
        },
        "rig": {
            "watchdog": str(rig.get("WD_ENABLED") or "").strip() == "1",
            "maintenance": str(rig.get("MAINTENANCE") or "").strip() == "1",
            "keys": sorted(rig.keys()),
            "values": rig,
        },
        "cur_miner": cur_miner,
        "miner_dirs": miner_dirs,
        "miner": flight_miner,
        "status": status,
        "wd_status": wd_status,
        "processes": processes,
        "gpu_total": gpu_stats["gpu_total"],
        "busids": gpu_stats["busids"],
        "mining_gpus": int(last_stat.get("mining_gpus") or 0),
        "hashrate_hs": last_stat.get("hashrate_hs"),
        "hashrate_source": "last_stat.json" if last_stat.get("ok") else "",
        "coin": last_stat.get("coin") or str(wallet.get("COIN") or ""),
        "algo": last_stat.get("algo") or "",
    }


# ── Politique de pilotage ──────────────────────────────────────────────
def is_hiveos(machine: Any) -> bool:
    kind = str(getattr(machine, "os_kind", "") or "").lower()
    detected = str(getattr(machine, "os_kind_detected", "") or "").lower()
    return "hiveos" in (kind, detected)


def active_processes(probe: dict[str, Any], miner: str | None = None) -> list[dict[str, Any]]:
    """Processus du mineur de la flight sheet (chemin ``/hive/miners/…``)."""
    name = (miner or probe.get("miner") or "").strip()
    rows = probe.get("processes") or []
    if not name:
        return [r for r in rows if "/hive/miners/" in r["args"]]
    needle = f"/hive/miners/{name}"
    return [r for r in rows if needle in r["args"] or (name in r["args"] and "/hive/miners/" in r["args"])]


def miner_running(probe: dict[str, Any]) -> bool:
    if active_processes(probe):
        return True
    running = (probe.get("status") or {}).get("running")
    return bool(running) if running is not None else False


def watchdog_verdict(probe: dict[str, Any], ack: bool = False) -> dict[str, Any]:
    """Ce que fait le watchdog HiveOS, et la parade applicable."""
    rig = probe.get("rig") or {}
    if rig.get("maintenance"):
        return {
            "level": "ok",
            "enabled": True,
            "reason": (
                "maintenance HiveOS active (MAINTENANCE=1) : le script « hive » ne démarre "
                "ni le mineur ni le watchdog"
            ),
            "parade": "",
        }
    if not rig.get("watchdog"):
        return {
            "level": "off",
            "enabled": False,
            "reason": "watchdog HiveOS inactif (WD_ENABLED≠1 dans rig.conf)",
            "parade": "",
        }
    if not ack:
        return {
            "level": "block",
            "enabled": True,
            "reason": WATCHDOG_REFUSAL,
            "parade": WATCHDOG_PARADE,
        }
    return {
        "level": "ack",
        "enabled": True,
        "reason": (
            "watchdog HiveOS actif : arrêté temporairement par « wd stop » (option validée), "
            "remis en route par « wd start » en fin de campagne"
        ),
        "parade": WATCHDOG_PARADE,
    }


def _miner_argv(probe: dict[str, Any], action: str) -> tuple[str, ...]:
    binary = str(probe.get("miner_bin") or "/hive/bin/miner")
    return ("sudo", "-n", binary, action)


def _wd_argv(action: str) -> tuple[str, ...]:
    return ("sudo", "-n", "/hive/bin/wd", action)


def plan_stop(probe: dict[str, Any], ack: bool = False) -> list[Step]:
    """Arrêt du mineur HiveOS, vérifié par ``ps``."""
    verdict = watchdog_verdict(probe, ack)
    if verdict["level"] == "block":
        raise HiveosError(WATCHDOG_REFUSAL)
    steps: list[Step] = []
    if verdict["level"] == "ack":
        steps.append(Step("watchdog HiveOS → arrêt temporaire", _wd_argv("stop"), allow_fail=True))
    steps.append(Step("mineur HiveOS → arrêt (miner stop)", _miner_argv(probe, "stop")))
    steps.append(Step("vérification : processus du mineur HiveOS", ("ps", "-eo", "pid,args"), allow_fail=True))
    return steps


def plan_start(probe: dict[str, Any], ack: bool = False) -> list[Step]:
    """Reprise par HiveOS (``miner start``), avec retour du hashrate à vérifier."""
    verdict = watchdog_verdict(probe, ack)
    if verdict["level"] == "block":
        raise HiveosError(WATCHDOG_REFUSAL)
    steps = [
        Step("reprise par HiveOS (miner start)", _miner_argv(probe, "start")),
        Step("vérification : processus du mineur HiveOS", ("ps", "-eo", "pid,args"), allow_fail=True),
        Step("vérification : hashrate HiveOS (last_stat.json)", ("cat", "/run/hive/last_stat.json"), allow_fail=True),
    ]
    if verdict["level"] == "ack":
        steps.append(Step("watchdog HiveOS → remise en route", _wd_argv("start"), allow_fail=True))
    return steps


def partial_refusal(requested_gpus: Sequence[int], gpu_total: int) -> str | None:
    """Refus explicite quand la campagne ne prend qu'une partie des GPU du rig."""
    if gpu_total <= 0:
        return None
    engaged = len({int(g) for g in requested_gpus})
    if engaged >= gpu_total:
        return None
    return PARTIAL_REFUSAL


def gpu_indices(devices: Iterable[Any]) -> list[int]:
    """Indices des GPU présents et autorisés au minage."""
    out: list[int] = []
    for device in devices:
        if getattr(device, "kind", "") != "gpu":
            continue
        if not getattr(device, "present", False):
            continue
        if not getattr(device, "enabled_for_mining", False):
            continue
        if getattr(device, "integrated", False):
            continue
        if device.index is None:
            continue
        out.append(int(device.index))
    return sorted(set(out))


def pilotage(
    probe: dict[str, Any] | None,
    devices: Sequence[Any] = (),
    *,
    ack: bool = False,
) -> dict[str, Any]:
    """Vue « Pilotage HiveOS » d'une fiche machine (sans aucun I/O)."""
    if not probe or not probe.get("detected"):
        return {
            "present": False,
            "pilotable": False,
            "reason": "aucune sonde HiveOS enregistrée : lancez « Sonder HiveOS » (lecture seule)",
            "watchdog": {"level": "off", "reason": "", "parade": ""},
            "miner": "",
            "devices": [],
            "refusal": "",
            "commands": {"stop": [], "start": []},
        }
    verdict = watchdog_verdict(probe, ack)
    gpus = gpu_indices(devices)
    total = int(probe.get("gpu_total") or 0) or len(gpus)
    refusal = ""
    if total and gpus and len(gpus) < total:
        refusal = PARTIAL_REFUSAL
    if verdict["level"] == "block":
        refusal = refusal or verdict["reason"]
    try:
        stop_commands = [s.command() for s in plan_stop(probe, ack)] if verdict["level"] != "block" else []
        start_commands = [s.command() for s in plan_start(probe, ack)] if verdict["level"] != "block" else []
    except HiveosError:
        stop_commands, start_commands = [], []
    return {
        "present": True,
        "pilotable": verdict["level"] != "block" and not (total and len(gpus) < total),
        "reason": verdict["reason"],
        "watchdog": verdict,
        "miner": probe.get("miner") or "",
        "coin": probe.get("coin") or "",
        "miner_bin": probe.get("miner_bin") or "",
        "custom_dir": bool(probe.get("custom_dir")),
        "custom_miners": list(probe.get("custom_miners") or []),
        "gpu_total": total,
        "mining_gpus": int(probe.get("mining_gpus") or 0),
        "hashrate_hs": probe.get("hashrate_hs"),
        "running": miner_running(probe),
        "devices": [int(i) for i in gpus],
        "refusal": refusal,
        "commands": {"stop": stop_commands, "start": start_commands},
    }


def mask_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """Version stockable : aucun secret, aucune adresse de wallet en clair."""
    import copy

    out = copy.deepcopy(probe)
    wallet = out.get("wallet")
    if isinstance(wallet, dict):
        wallet["wallet"] = MASKED
    rig = out.get("rig")
    if isinstance(rig, dict) and isinstance(rig.get("values"), dict):
        rig["values"] = {
            key: (MASKED if is_secret_key(key) else str(value)[:200])
            for key, value in rig["values"].items()
        }
    return out
