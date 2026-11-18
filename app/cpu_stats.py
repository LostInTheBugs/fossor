"""Hashrate CPU et état du CPU (2026.11.003-c7).

Pure parsers, no SSH and no database: every function here turns the text of the
``collect_cpu`` command into plain dictionaries so the whole module is tested
against fixtures.

Sources, in the order the brief asks for:

1. **API du mineur** — the port comes from the command line (``--api-bind``,
   ``--api-port``, ``--http-port``) or a known default (cpuminer-opt 4048,
   ccminer/OMS 4068);
2. **log du mineur** — the path comes from ``--log-file``, ``--log``, ``-l`` or a
   shell redirection (``>`` / ``>>``); the last 80 lines are read and matched
   against known hashrate wordings, with a 300 s freshness gate;
3. **nœud solo** — the ``*-cli`` found in the command line, getters only
   (``getmininginfo``, ``getblockchaininfo``, ``getnetworkhashps``), plus the
   « 200k nonces / intervalle des scans » estimate (its log format is NOT
   verified, see ``nonces_hashrate``), and the real ``hashes/s/worker`` log of
   ``mine-bigcoin.sh`` as the primary solo source.

A source that yields nothing produces an explicit note, never a silent « — ».
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

# A log older than this is not a live hashrate: the miner may have been stopped
# while its last line stayed readable (Fred's wam log froze at « 1,21 kH/s »).
LOG_MAX_AGE = 300

# Sections of the ``collect_cpu`` command that carry no dynamic suffix.
FIXED_SECTIONS = (
    "PROC_STAT", "PROC_STAT2", "CPUINFO", "FREQ", "HWMON", "POWER", "POWER2",
)

_HASHRATE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(PH|TH|GH|MH|kH|H)\s*/\s*s", re.IGNORECASE)
_UNIT_HS = {
    "h": 1.0, "kh": 1e3, "mh": 1e6, "gh": 1e9, "th": 1e12, "ph": 1e15,
}
_TTF_RE = re.compile(r"TTF\s*@\s*(\d+(?:[.,]\d+)?)\s*([PTGMk]?[Hh])\s*/\s*s")
_XMRIG_RE = re.compile(
    r"speed\s+10s/60s/15m\s+(\d+(?:\.\d+)?)\s+\S+\s+\S+\s+([kMG]?H)/s", re.IGNORECASE
)
_SCAN_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z).*?\bscan\b.*?(\d+)(k?)\b", re.IGNORECASE
)
_SCAN_NONCES_RE = re.compile(r"(\d+)\s*(k?)\s*(?:nonces|nonce)", re.IGNORECASE)
_FOUND_RE = re.compile(r"FOUND:\s*\[")

# CPU temperature sensors: k10temp Tctl (AMD) then coretemp Package (Intel).
# A GPU or an NVMe sensor must never be reported as the CPU temperature.
_CPU_HWMON = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "soc_thermal")

# Miner families: the same token is used by the probe planner and the matcher.
FAMILIES = (
    ("cpuminer", "cpuminer"),
    ("wam-miner", "wam-miner"),
    ("xmrig", "xmrig"),
    ("supryolo", "oms"),
    ("ccminer", "oms"),
    ("srbminer", "srbminer"),
)
DEFAULT_API_PORTS = {"cpuminer": 4048, "oms": 4068}

_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_.\-/]{1,300}$")
_SAFE_REL_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,300}$")
_SAFE_BIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_LOG_FLAGS = ("--log-file", "--log", "-l", "--logfile")
_API_FLAGS = ("--api-bind", "--api-port", "--http-port")
_REDIRECT_RE = re.compile(r"(?:\d?>>?|\|&?)\s*(/[^\s;|&<>]+)")


# ── Sections ──────────────────────────────────────────────────────────
def split_blocks(stdout: str) -> dict[str, str]:
    """Split ``### NAME`` output, keeping the whole title (``LOG /var/log/x``)."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in (stdout or "").splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in blocks.items()}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    """``237,8`` as well as ``237.8`` — miners print both."""
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    return _to_float(text)


# ── CPU state ─────────────────────────────────────────────────────────
def parse_load(sample_a: str, sample_b: str) -> float | None:
    """CPU load % from two ``/proc/stat`` snapshots (aggregate ``cpu`` line)."""
    def read(text: str) -> tuple[float, float] | None:
        for line in (text or "").splitlines():
            parts = line.split()
            if not parts or parts[0] != "cpu":
                continue
            # user nice system idle iowait irq softirq steal guest guest_nice
            values = [_to_float(p) or 0.0 for p in parts[1:9]]
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0.0)
            return sum(values), idle
        return None

    first, second = read(sample_a), read(sample_b)
    if not first or not second:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    busy = max(0.0, total_delta - idle_delta)
    return round(100.0 * busy / total_delta, 1)


def parse_cpuinfo(text: str) -> dict[str, Any]:
    """Cores, model and average ``cpu MHz`` from ``/proc/cpuinfo``.

    The key is normalised (spaces/tabs collapsed, lower case): ``cpu MHz``,
    ``CPU MHz`` and ``cpu  MHz`` all land on the same branch, which is what the
    real file contains on every x86 machine (2026.11.003-c8).
    """
    cores = 0
    frequencies: list[float] = []
    model = ""
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        key = " ".join(key.strip().lower().split())
        value = value.strip()
        if key == "processor":
            cores += 1
        elif key == "cpu mhz":
            number = _to_float(value)
            if number:
                frequencies.append(number)
        elif key == "model name" and not model:
            model = value
    return {
        "cores": cores or None,
        "model": model,
        "freq_mhz": round(sum(frequencies) / len(frequencies), 1) if frequencies else None,
    }


def parse_scaling_freq(text: str) -> float | None:
    """Average ``scaling_cur_freq`` (kHz) → MHz."""
    values = [_to_float(line) for line in (text or "").splitlines()]
    clean = [value for value in values if value and value > 0]
    if not clean:
        return None
    return round(sum(clean) / len(clean) / 1000.0, 1)


def parse_hwmon(text: str) -> tuple[float | None, str]:
    """CPU temperature from ``name|label|millideg`` lines; returns (°C, sensor)."""
    entries: list[tuple[str, str, float]] = []
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3:
            continue
        name, label, raw = parts
        value = _to_float(raw)
        if value is None:
            continue
        entries.append((name.lower(), label, value / 1000.0))

    def pick(predicate) -> tuple[float | None, str]:
        for name, label, value in entries:
            if predicate(name, label):
                return round(value, 1), (f"{name} {label}".strip())
        return None, ""

    # AMD first (Tctl is the reported die temperature), then Intel Package.
    found = pick(lambda n, l: n.startswith("k10temp") and l.lower() == "tctl")
    if found[0] is not None:
        return found
    found = pick(lambda n, l: n.startswith("coretemp") and l.lower().startswith("package"))
    if found[0] is not None:
        return found
    for sensor in _CPU_HWMON:
        found = pick(lambda n, l, s=sensor: n.startswith(s))
        if found[0] is not None:
            return found
    return None, ""


def parse_rapl(sample_a: str, sample_b: str, interval_s: float = 1.0) -> float | None:
    """Package power (W) from two RAPL readings.

    Chaque ligne est ``chemin|microjoules`` (``energy_uj`` seul) ou
    ``chemin|microjoules|max_energy_range_uj`` quand la sonde lit aussi le
    plafond : le compteur peut repartir à zéro entre les deux lectures, et
    ``max - précédent + courant`` donne alors le delta réel au lieu d'une
    puissance négative écartée.

    Only the package domain (``intel-rapl:0`` / ``intel-rapl`` root) is summed:
    the ``core`` / ``uncore`` / ``dram`` children are parts of the same package
    and adding them doubles the result; ``psys`` (``intel-rapl:1``), qui couvre
    la plateforme entière, et le miroir ``intel-rapl-mmio:*`` sont exclus par le
    même motif.
    """
    def read(text: str) -> dict[str, tuple[float, float | None]]:
        values: dict[str, tuple[float, float | None]] = {}
        for line in (text or "").splitlines():
            parts = line.split("|")
            if len(parts) < 2:
                continue
            path = parts[0].strip()
            value = _to_float(parts[1])
            if not path or value is None:
                continue
            maximum = _to_float(parts[2]) if len(parts) > 2 else None
            leaf = path.rstrip("/").rsplit("/", 2)
            name = leaf[-2] if len(leaf) >= 2 else path
            if not re.search(r"^(?:intel|amd)-rapl(:0)?$", name):
                continue
            values[name] = (value, maximum)
        return values

    first, second = read(sample_a), read(sample_b)
    total = 0.0
    seen = 0
    for name, (value, maximum) in second.items():
        previous = first.get(name)
        if previous is None:
            continue
        delta = value - previous[0]
        if delta < 0:  # compteur reparti à zéro
            limit = maximum or previous[1]
            if not limit or limit <= previous[0]:
                continue
            delta = limit - previous[0] + value
        total += delta / 1_000_000.0 / max(0.1, interval_s)
        seen += 1
    if not seen:
        return None
    return round(total, 2)


def parse_hwmon_power(text: str) -> tuple[float | None, str]:
    """Package power (W) from ``amd_energy`` / ``zenpower`` ``powerN_input``.

    2026.11.003-c8 : troisième source de puissance.  Les valeurs sont en
    microwatts ; ``power1_input`` (paquet) est préféré, un pilote qui n'expose
    que ``power2_input`` (cœurs) reste une mesure utilisable.
    """
    entries: list[tuple[str, str, float]] = []
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3:
            continue
        name, label, raw = parts
        value = _to_float(raw)
        if value is None or value <= 0:
            continue
        entries.append((name.lower(), label.lower(), value / 1_000_000.0))
    for name, label, value in entries:
        if name in ("amd_energy", "zenpower") and label.startswith("power1"):
            return round(value, 2), f"hwmon {name} {label}"
    return None, ""


# ── Hashrate from a log ───────────────────────────────────────────────
def hashrate_from_text(text: str) -> tuple[float | None, str]:
    """Last hashrate of a log tail, in H/s, and the wording it came from."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None, ""
    # cpuminer-opt: « TTF @ 237.8 Mh/s » (athlon-3000G, KORSH).
    for line in reversed(lines):
        match = _TTF_RE.search(line)
        if match:
            number = _number(match.group(1))
            factor = _UNIT_HS.get(match.group(2).lower().replace("h", "h"), 1.0)
            if number is not None:
                return number * factor, match.group(0).strip()
    # xmrig: « speed 10s/60s/15m 1234.5 1200.0 1180.0 H/s max … » (10 s window).
    for line in reversed(lines):
        match = _XMRIG_RE.search(line)
        if match:
            number = _to_float(match.group(1))
            factor = _UNIT_HS.get(match.group(2).lower(), 1.0)
            if number is not None:
                return number * factor, match.group(0).strip()
    # wam-miner, cpuminer summary, SRBMiner CPU: « … 1.21 kH/s » / « Total: 3 MH/s ».
    for line in reversed(lines):
        matches = list(_HASHRATE_RE.finditer(line))
        if not matches:
            continue
        match = matches[-1]
        number = _number(match.group(1))
        if number is None:
            continue
        return number * _UNIT_HS.get(match.group(2).lower(), 1.0), match.group(0).strip()
    return None, ""


def scan_hashrate(text: str) -> float | None:
    """Solo estimate: nonces of a scan / seconds between the last two scans."""
    moments: list[tuple[str, float]] = []
    for line in (text or "").splitlines():
        match = _SCAN_RE.search(line)
        if not match:
            continue
        nonces = _to_float(match.group(2)) or 0.0
        if match.group(3):
            nonces *= 1000.0
        if nonces <= 0:
            continue
        moments.append((match.group(1), nonces))
    if len(moments) < 2:
        return None
    from datetime import datetime, timezone

    def parse(stamp: str) -> float | None:
        try:
            return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            return None

    last, previous = moments[-1], moments[-2]
    end, start = parse(last[0]), parse(previous[0])
    if end is None or start is None:
        return None
    delta = end - start
    if not 0 < delta <= 3600:
        return None
    return round(previous[1] / delta, 1)


_TS_PREFIX_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|\b\d{2}:\d{2}:\d{2}\b)"
)
_NONCES_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(k|M|G)?\s*nonces?\b", re.IGNORECASE)

# ── Log du script solo : format RÉEL, relevé le 2026-11-17 ──────────────
# ``mine-bigcoin.sh`` est lancé dans son dossier et sa sortie est redirigée
# vers ``mine.log`` (retrouvable par ``readlink /proc/<pid du script>/fd/1``).
# Le script y écrit une ligne par minute environ :
#   [21:55:00] mining ... height=20353 difficulty=0.003300662640699338 #   algo=yespower 632 hashes/s/worker (~6952 total over 11 workers)
_SCRIPT_HASHRATE_RE = re.compile(
    r"(\d+)\s+hashes/s/worker\s+\(~(\d+)\s+total\s+over\s+(\d+)\s+workers\s*\)",
    re.IGNORECASE,
)
_SCRIPT_STAMP_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
_SCRIPT_HEIGHT_RE = re.compile(r"\bheight=(\d+)")
_SCRIPT_DIFF_RE = re.compile(r"\bdifficulty=(\d+(?:\.\d+)?)")
_SCRIPT_ALGO_RE = re.compile(r"\balgo=([A-Za-z0-9_-]+)")


def _local_stamp_age(stamp: str, now: float) -> int | None:
    """Âge en secondes d'un horodatage ``[HH:MM:SS]`` (heure locale machine)."""
    from time import localtime

    try:
        hour, minute, second = (int(part) for part in stamp.split(":"))
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None
    current = localtime(now)
    age = (current.tm_hour * 3600 + current.tm_min * 60 + current.tm_sec) - (
        hour * 3600 + minute * 60 + second
    )
    if age < 0:
        age += 86400  # passage de minuit
    return age


def script_hashrate(
    text: str,
    mtime: int | None = None,
    now: float | None = None,
    max_age: int = LOG_MAX_AGE,
) -> dict[str, Any]:
    """Hashrate du log d'un script solo, au format **relevé** sur la machine.

    Dernière ligne ``N hashes/s/worker (~T total over W workers)`` → ``T`` H/s
    et ``W`` workers, plus ``height=``, ``difficulty=`` et ``algo=``.  Fraîcheur :
    la ligne est périmée seulement quand l'horodatage ``[HH:MM:SS]`` (heure
    locale de la machine) **et** le mtime du fichier dépassent *max_age*.
    """
    import time

    moment = time.time() if now is None else now
    last_line, last_match = "", None
    for line in (text or "").splitlines():
        match = _SCRIPT_HASHRATE_RE.search(line)
        if match:
            last_line, last_match = line, match
    if last_match is None:
        return {
            "hashrate_hs": None, "workers": None, "height": None,
            "difficulty": None, "algo": "", "line": "", "age_s": None,
            "state": "unknown",
            "note": "aucune ligne « hashes/s/worker » dans le log",
        }
    total = _to_float(last_match.group(2))
    workers = int(last_match.group(3))
    ages: list[int] = []
    stamp = _SCRIPT_STAMP_RE.search(last_line)
    stamp_age = _local_stamp_age(stamp.group(1), moment) if stamp else None
    if stamp_age is not None:
        ages.append(stamp_age)
    if mtime is not None:
        ages.append(max(0, int(moment - float(mtime))))
    age = min(ages) if ages else None
    height = _SCRIPT_HEIGHT_RE.search(last_line)
    difficulty = _SCRIPT_DIFF_RE.search(last_line)
    algo = _SCRIPT_ALGO_RE.search(last_line)
    result: dict[str, Any] = {
        "hashrate_hs": total if total and total > 0 else None,
        "workers": workers,
        "height": int(height.group(1)) if height else None,
        "difficulty": _to_float(difficulty.group(1)) if difficulty else None,
        "algo": algo.group(1) if algo else "",
        "line": last_line.strip(),
        "age_s": age,
        "state": "ok",
        "note": "",
    }
    if age is not None and age > max_age:
        result.update(
            hashrate_hs=None,
            state="stale",
            note=f"log figé depuis {age} s (mineur arrêté ?)",
        )
    return result


def nonces_hashrate(text: str, workers: int = 1) -> float | None:
    """Dernier recours : nonces d'un scan × workers / intervalle.

    **FORMAT NON VÉRIFIÉ** — aucun relevé réel ne montre des lignes
    « <horodatage> … N nonces » ; le format du log de ``mine-bigcoin.sh`` est
    celui de ``script_hashrate`` ci-dessus.  Aucune paire exploitable ->
    ``None`` (la raison exacte est conservée par l'appelant).
    """
    from datetime import datetime, timezone

    moments: list[tuple[str, float]] = []
    for line in (text or "").splitlines():
        match = _NONCES_RE.search(line)
        if not match:
            continue
        stamp = _TS_PREFIX_RE.search(line)
        if not stamp:
            continue
        number = _to_float(match.group(1)) or 0.0
        factor = {"k": 1e3, "m": 1e6, "g": 1e9}.get((match.group(2) or "").lower(), 1.0)
        number *= factor
        if number > 0:
            moments.append((stamp.group(1), number))
    if len(moments) < 2:
        return None

    def parse(stamp: str) -> float | None:
        try:
            if "T" in stamp:
                return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            now = datetime.now(timezone.utc)
            parsed = datetime.strptime(stamp, "%H:%M:%S").replace(
                year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc
            )
            return parsed.timestamp()
        except ValueError:
            return None

    last, previous = moments[-1], moments[-2]
    end, start = parse(last[0]), parse(previous[0])
    if end is None or start is None:
        return None
    delta = end - start
    if not 0 < delta <= 3600:
        return None
    return round(previous[1] / delta * max(1, int(workers or 1)), 1)


def blocks_found(text: str) -> int:
    """Solo blocks found, counted from the script log (``FOUND: [``)."""
    return len(_FOUND_RE.findall(text or ""))


def log_hashrate(
    text: str,
    mtime: int | None = None,
    now: float | None = None,
    max_age: int = LOG_MAX_AGE,
) -> dict[str, Any]:
    """Hashrate of a log tail with its freshness gate.

    Returns ``{"hashrate_hs", "line", "age_s", "state", "note"}``.  A log whose
    mtime is older than *max_age* is reported as a stopped miner: its last line
    is stale and must never be shown as a live hashrate.
    """
    import time

    moment = time.time() if now is None else now
    age = None if mtime is None else max(0, int(moment - mtime))
    hashrate, line = hashrate_from_text(text)
    if mtime is not None and age is not None and age > max_age:
        return {
            "hashrate_hs": None, "line": line, "age_s": age,
            "state": "stale",
            "note": f"log figé depuis {age} s (mineur arrêté ?)",
        }
    if hashrate is None:
        return {
            "hashrate_hs": None, "line": "", "age_s": age,
            "state": "unknown",
            "note": "aucun motif de hashrate dans les 80 dernières lignes",
        }
    return {"hashrate_hs": hashrate, "line": line, "age_s": age, "state": "ok", "note": ""}


# ── Miner API and node CLI ────────────────────────────────────────────
def parse_api_payload(payload: str) -> dict[str, Any] | None:
    """Hashrate from a miner HTTP API (OMS ``/summary`` or cpuminer ``/summary``).

    OMS / supryolo answer ``{"state": "MINING", "devices": [{"hashrate": 1.2e9}]}``;
    cpuminer-opt answers ``{"hashrate": 237800000.0, "accepted": 12, …}``.
    """
    text = (payload or "").strip()
    if not text or not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    hashrate: float | None = None
    devices = data.get("devices")
    if isinstance(devices, list) and devices:
        values = [
            _number(entry.get("hashrate")) if isinstance(entry, dict) else _number(entry)
            for entry in devices
        ]
        values = [value for value in values if value]
        hashrate = sum(values) if values else None
    for key in ("hashrate", "hashrate_hs", "hs", "speed", "total_hashrate"):
        if hashrate is None and key in data:
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get("total") or value.get("1m") or value.get("10s")
            hashrate = _number(value)
    return {
        "hashrate_hs": hashrate,
        "state": str(data.get("state") or ""),
        "accepted": data.get("accepted"),
        "rejected": data.get("rejected"),
        "algo": str(data.get("algorithm") or data.get("algo") or ""),
        "threads": data.get("threads") or data.get("cpu_threads"),
    }


def parse_mininginfo(payload: str) -> dict[str, Any]:
    """Read-only getters of a node: ``getmininginfo`` / ``getblockchaininfo``."""
    text = (payload or "").strip()
    empty = {"height": None, "network_hs": None, "difficulty": None,
             "blocks": None, "chain": ""}
    if not text.startswith("{"):
        return empty
    try:
        data = json.loads(text)
    except ValueError:
        return empty
    if not isinstance(data, dict):
        return empty
    return {
        "height": _to_int(data.get("blocks")),
        "network_hs": _to_float(data.get("networkhashps")),
        "difficulty": _to_float(data.get("difficulty")),
        "blocks": _to_int(data.get("blocks")),
        "chain": str(data.get("chain") or ""),
    }


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def network_share_pct(local_hs: float | None, network_hs: float | None) -> float | None:
    """Share of the network hashrate, in %, for a solo miner."""
    if not local_hs or not network_hs or network_hs <= 0:
        return None
    return round(100.0 * local_hs / network_hs, 4)


# ── Probe planning (from the ps command lines) ────────────────────────
def miner_family(text: str) -> str:
    """Family token of a miner command line (``cpuminer``, ``wam-miner``…)."""
    lower = (text or "").lower()
    for needle, family in FAMILIES:
        if needle in lower:
            return family
    return ""


def _valid_path(value: str) -> str:
    text = (value or "").strip().strip("\"'").rstrip(",;")
    if not text or text == "/dev/null":
        return ""
    return text if _SAFE_PATH_RE.match(text) else ""


def _flag(tokens: Sequence[str], flags: Iterable[str]) -> str:
    for index, token in enumerate(tokens):
        base, sep, value = token.partition("=")
        if base not in flags:
            continue
        if sep:
            return value
        if index + 1 < len(tokens):
            return tokens[index + 1]
    return ""


def _api_port(tokens: Sequence[str], family: str) -> int | None:
    raw = _flag(tokens, _API_FLAGS)
    if raw:
        candidate = raw.rsplit(":", 1)[-1].strip()
        if candidate.isdigit() and 1 <= int(candidate) <= 65535:
            return int(candidate)
        return None
    return DEFAULT_API_PORTS.get(family)


def _datadir_coin(datadir: str) -> str:
    """Coin name read from a datadir (``…/bigcoin-mine/bigcoin/data`` → bigcoin)."""
    parts = [part for part in (datadir or "").rstrip("/").split("/") if part]
    if not parts:
        return ""
    name = parts[-1].lstrip(".").lower()
    if name in ("data", "chain", "blocks", "db") and len(parts) > 1:
        name = parts[-2].lstrip(".").lower()
    match = re.match(r"[a-z0-9]+", name)
    return match.group(0) if match else ""


_SCRIPT_MINER_RE = re.compile(
    r"(?:^|[\s/])([A-Za-z0-9_.\-]*(?:-miner|mine-)[A-Za-z0-9_.\-]*)\.sh\b"
)


def _script_miner_key(args: str) -> str:
    """Key of a solo script (``korsh-miner.sh``, ``mine-bigcoin.sh``)."""
    match = _SCRIPT_MINER_RE.search(args or "")
    if not match:
        return ""
    name = match.group(1)  # « .sh » n'est pas dans le groupe
    if name.startswith("mine-"):
        name = name[5:]
    for suffix in ("-miner", "-mining", "-mine"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name if _SAFE_BIN_RE.match(name) else ""


#: Lanceur shell d'un script solo : ``bash -c 'cd … && nohup ./mine-x.sh > log &'``
_SOLO_LAUNCHER_RE = re.compile(r"(?:^|\s)-[A-Za-z]*c(?:\s|$)|&&|>>?\s")
#: Redirection du lanceur, absolue ou ``~/…`` (le home vient de ``getent passwd``).
_SOLO_REDIRECT_RE = re.compile(r"(?:^|\s)\d?>>?\s*(~?/[^\s;|&<>]+)")
#: Chemin de log accepté pour (b), absolu ou ``~/…`` (jamais d'espace).
_SOLO_LOG_RE = re.compile(r"^~?/[A-Za-z0-9_.\-/]{1,300}$")
#: Compte dont on lit le home pour développer ``~/…``.
_SOLO_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _solo_launcher(args: str) -> bool:
    """True for the row that *starts* a solo script, not for the script itself.

    2026.11.004-c1 : le lanceur est le parent du script (``bash -c cd … && nohup
    ./mine-bigcoin.sh > ~/…/mine.log 2>&1 &``).  Ses arguments portent la
    redirection, et ``/proc/<pid>/fd/1`` du script en est la conséquence.
    """
    tokens = (args or "").split()
    first = tokens[0].rsplit("/", 1)[-1].lower() if tokens else ""
    if first.startswith("screen"):
        return True
    return bool(_SOLO_LAUNCHER_RE.search(args or ""))


def _ordered_solo_pids(entries: Sequence[dict[str, Any]], limit: int = 2) -> list[int]:
    """Pids du script/lanceur, le plus ancien d'abord (le principal en tête).

    Un ``mine-bigcoin.sh`` apparaît une fois par worker (11 lignes identiques) :
    garder les *limit* plus anciens évite 11 sondes ``/proc/<pid>/fd`` pour le
    même fichier, tout en gardant un repli si le principal a fermé ses fd.
    """
    unique: dict[int, float] = {}
    for entry in entries:
        pid = entry.get("pid")
        if isinstance(pid, int) and pid > 0:
            unique.setdefault(pid, float(entry.get("elapsed_s") or 0))
    ordered = sorted(unique.items(), key=lambda item: (-item[1], item[0]))
    return [pid for pid, _ in ordered[:limit]]


def plan_cpu_probes(ps_section: str) -> dict[str, list[dict[str, Any]]]:
    """Read-only probes to run for the miners found in ``ps``.

    Returns ``{"logs": [...], "apis": [...], "nodes": [...], "fds": [...]}``.
    Every path is validated here (absolute, no space, no shell metacharacter):
    the SSH command interpolates it, so an unvalidated path would be a command
    injection.

    2026.11.003-c8 : un nœud solo est détecté de façon **stable** (démon
    ``*d -daemon`` qui consomme du CPU, script ``*-miner.sh`` / ``mine-*.sh``
    lancé dans ``screen``) ; le client ``*-cli … generatetoaddress``, transitoire,
    ne sert plus qu'à compter les workers et à donner un dossier de travail.
    """
    from app.external_miners import (
        DAEMON_BINARIES,
        _is_real_row,
        basename,
        parse_ps_rows,
        split_launcher,
    )

    logs: list[dict[str, Any]] = []
    apis: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    fds: list[dict[str, Any]] = []
    node_rows: dict[str, dict[str, Any]] = {}
    node_pids: dict[str, list[int]] = {}
    node_scripts: dict[str, list[dict[str, Any]]] = {}
    node_launchers: dict[str, list[dict[str, Any]]] = {}
    seen_logs: set[str] = set()
    seen_apis: set[int] = set()

    def add_node(key: str, force: bool = False, follow: bool = True, **values: Any) -> None:
        if not key or not _SAFE_BIN_RE.match(key):
            return
        entry = node_rows.setdefault(
            key,
            {"key": key, "cli": "", "token": "", "datadir": "", "pid": None, "coin": ""},
        )
        for field, value in values.items():
            if value and (force or not entry.get(field)):
                entry[field] = value
        pid = values.get("pid")
        # ``follow=False`` : le client ``*-cli`` donne seulement le dossier de
        # travail (cwd) ; son fd/1 n'est pas un log et son pid est transitoire.
        if follow and isinstance(pid, int) and pid > 0:
            node_pids.setdefault(key, []).append(pid)

    for row in parse_ps_rows(ps_section):
        if not _is_real_row(row):
            continue
        args = (row.get("args") or "").strip()
        if not args or args.startswith("[") or "### " in args:
            continue
        tokens = args.split()
        base = basename(tokens[0])
        if base in ("sh", "bash", "dash") and len(tokens) > 1 and basename(tokens[1]).endswith("-cli"):
            tokens = tokens[1:]
            base = basename(tokens[0])

        # Node solo: the transient client only counts workers; the daemon and
        # the mining script are what stay visible between two collections.
        if base.endswith("-cli") and "generatetoaddress" in args:
            add_node(
                base[:-4],
                force=True,
                follow=False,
                cli=_valid_path(tokens[0]),
                token=tokens[0] if _SAFE_REL_RE.match(tokens[0]) else "",
                datadir=_flag(tokens, ("-datadir",)),
                pid=row.get("pid"),
            )
            continue
        if base in DAEMON_BINARIES and (
            "-daemon" in tokens or (row.get("cpu_pct") or 0) > 0
        ):
            datadir = _flag(tokens, ("-datadir",))
            add_node(_datadir_coin(datadir) or base[:-1], datadir=datadir, pid=row.get("pid"))
            continue

        family = miner_family(args)
        if not family:
            # ``screen … korsh-miner.sh`` : le script reste présent même quand
            # aucun client ne tourne.  Hors screen, le lanceur
            # ``bash -c 'cd … && nohup ./mine-bigcoin.sh > ~/…/mine.log 2>&1 &'``
            # porte la redirection et le script la subit : les deux pids sont
            # suivis par /proc/<pid>/fd/1|2 (2026.11.004-c1).  Aucun nom de log
            # n'est deviné — ``mine.log`` n'était pas dans la liste.
            script_key = _script_miner_key(args)
            if script_key:
                entry = {
                    "pid": row.get("pid"),
                    "elapsed_s": row.get("elapsed_s"),
                    "user": row.get("user") or "",
                    # (b) redirection de la ligne du lanceur, absolue ou « ~/… ».
                    "redirect": (
                        match.group(1) if (match := _SOLO_REDIRECT_RE.search(args)) else ""
                    ),
                }
                if _solo_launcher(args):
                    node_launchers.setdefault(script_key, []).append(entry)
                else:
                    node_scripts.setdefault(script_key, []).append(entry)
                add_node(script_key, datadir=_flag(tokens, ("-datadir",)))
            continue
        # SRBMiner is a GPU miner unless it was explicitly told to use the CPU:
        # probing its API here would duplicate the SRBMiner collector.
        if family == "srbminer" and "--cpu-threads" not in args and "--disable-gpu" not in args:
            continue
        # The log path may live on the launcher line (``screen … >> /root/wam/miner.log``).
        path = _valid_path(_flag(tokens, _LOG_FLAGS))
        if not path:
            for match in _REDIRECT_RE.finditer(args):
                path = _valid_path(match.group(1))
                if path:
                    break
        switch = split_launcher(args)[1] if split_launcher(args)[0] else args
        path = path or _valid_path(_flag(switch.split(), _LOG_FLAGS))
        if path and path not in seen_logs:
            seen_logs.add(path)
            logs.append({"path": path, "family": family, "pid": row.get("pid")})
        port = _api_port(switch.split(), family)
        if port and port not in seen_apis:
            seen_apis.add(port)
            apis.append({"port": port, "family": family, "pid": row.get("pid")})
        if isinstance(row.get("pid"), int) and row["pid"] > 0:
            fds.append({"pid": row["pid"], "family": family})

    for key, node in node_rows.items():
        from app.external_miners import coin_for_client

        node["coin"] = coin_for_client(key)
        # 2026.11.004-c1 : le log du solo se déduit de la redirection réelle,
        # pas d'un nom deviné — ``/proc/<pid>/fd/1|2`` du **script**, puis de
        # son lanceur, le démon en dernier (son fd est /dev/null ou le
        # journal).  ``mine.log`` est trouvé par (a) ``readlink fd/1`` et
        # (b) la redirection du lanceur, qui est la même ouverture.
        scripts = _ordered_solo_pids(node_scripts.get(key, []), limit=3)
        launchers = _ordered_solo_pids(node_launchers.get(key, []))
        daemons = sorted({pid for pid in node_pids.get(key, []) if pid > 0})
        pids = list(dict.fromkeys(scripts + launchers + daemons))
        node["pids"] = pids
        node["solo_pids"] = {"script": scripts, "parent": launchers, "daemon": daemons}
        for pid in pids:
            fds.append({"pid": pid, "family": f"node:{key}"})
        # (b) la redirection du lanceur, lue en clair dans ``ps`` : elle ne
        # dépend pas de /proc/<pid>/fd (root seul quand le script appartient à
        # un autre compte).  « ~/… » est développé sur la machine avec le home
        # de l'utilisateur du processus.
        for entry in node_launchers.get(key, []):
            raw = str(entry.get("redirect") or "")
            if _SOLO_LOG_RE.match(raw):
                node["redirect"] = raw
                node["redirect_user"] = str(entry.get("user") or "")
                break
        nodes.append(node)
    return {"logs": logs, "apis": apis, "nodes": nodes, "fds": fds}


def parse_cpu_report(stdout: str) -> dict[str, Any]:
    """Parse the ``collect_cpu`` output into state, logs, APIs and nodes."""
    blocks = split_blocks(stdout)
    info = parse_cpuinfo(blocks.get("CPUINFO", ""))
    temp_c, sensor = parse_hwmon(blocks.get("HWMON", ""))
    power = parse_rapl(blocks.get("POWER", ""), blocks.get("POWER2", ""))
    power_source = "rapl" if power is not None else ""
    if power is None:
        # 2026.11.003-c8 : AMD amd_energy/zenpower quand RAPL est absent.
        power, power_source = parse_hwmon_power(blocks.get("POWERH", ""))
    freq = info["freq_mhz"]
    freq_source = "cpuinfo" if freq is not None else ""
    if freq is None:
        freq = parse_scaling_freq(blocks.get("FREQ", ""))
        freq_source = "cpufreq scaling_cur_freq" if freq is not None else ""
    state = {
        "load_pct": parse_load(blocks.get("PROC_STAT", ""), blocks.get("PROC_STAT2", "")),
        "temp_c": temp_c,
        "temp_sensor": sensor,
        "freq_mhz": freq,
        "freq_source": freq_source,
        "cores": info["cores"],
        "model": info["model"],
        "power_w": power,
        "power_source": power_source,
    }
    logs: dict[str, dict[str, Any]] = {}
    log_pids: dict[str, int] = {}
    apis: dict[int, dict[str, Any] | None] = {}
    nodes: dict[str, dict[str, Any]] = {}
    for name, body in blocks.items():
        if name.startswith("LOGI "):
            # ``LOGI <pid> <chemin>`` : log déduit de /proc/<pid>/fd/1 ou 2.
            parts = name[5:].strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                path = parts[1].strip()
                lines = body.splitlines()
                logs[path] = {
                    "mtime": _to_int(lines[0]) if lines else None,
                    "text": "\n".join(lines[1:]) if len(lines) > 1 else "",
                }
                log_pids[path] = int(parts[0])
        elif name.startswith("JLOG "):
            # ``JLOG <pid>`` : sortie d'un mineur journalisée (socket/pty).
            pid_text = name[5:].strip()
            if pid_text.isdigit():
                path = f"journal:{pid_text}"
                logs[path] = {"mtime": None, "text": body}
                log_pids[path] = int(pid_text)
        elif name.startswith("LOG "):
            path = name[4:].strip()
            lines = body.splitlines()
            mtime = _to_int(lines[0]) if lines else None
            text = "\n".join(lines[1:]) if len(lines) > 1 else ""
            logs[path] = {"mtime": mtime, "text": text}
        elif name.startswith("API "):
            port_text = name[4:].strip()
            if port_text.isdigit():
                apis[int(port_text)] = parse_api_payload(body)
        elif name.startswith("NODE "):
            key = name[5:].strip()
            nodes.setdefault(key, {}).update(parse_mininginfo(body))
        elif name.startswith("CHAIN "):
            key = name[6:].strip()
            chain = parse_mininginfo(body)
            entry = nodes.setdefault(key, {"height": None, "network_hs": None,
                                           "difficulty": None, "blocks": None, "chain": ""})
            entry["chain"] = chain.get("chain") or entry.get("chain") or ""
            entry["height"] = entry.get("height") or chain.get("blocks")
        elif name.startswith("NETPS "):
            key = name[6:].strip()
            value = _to_float(body.strip().splitlines()[0]) if body.strip() else None
            entry = nodes.setdefault(key, {"height": None, "network_hs": None,
                                           "difficulty": None, "blocks": None, "chain": ""})
            entry["network_hs"] = entry.get("network_hs") or value
        elif name.startswith("LOGS "):
            # ``LOGS <key> <path>`` — the log a solo script writes to.
            title = name[5:].strip().split(None, 1)
            if len(title) == 2:
                key, path = title[0], title[1].strip()
                entry = nodes.setdefault(key, {})
                lines = body.splitlines()
                entry["log"] = {
                    "path": path,
                    "mtime": _to_int(lines[0]) if lines else None,
                    "text": "\n".join(lines[1:]) if len(lines) > 1 else "",
                }
    return {"state": state, "logs": logs, "log_pids": log_pids,
            "apis": apis, "nodes": nodes, "blocks": blocks}


# ── Attaching a hashrate to every CPU miner ───────────────────────────
def _match_family(miner: dict[str, Any], family: str) -> bool:
    if not family:
        return False
    text = " ".join(
        str(miner.get(key) or "") for key in ("binary", "name", "label", "miner_name")
    ).lower()
    return family in text


def attach_cpu_hashrates(
    miners: Sequence[dict[str, Any]],
    probes: dict[str, Any],
    report: dict[str, Any],
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Give every CPU miner a hashrate *or* an explicit reason (in place).

    Order: miner API (port from the command line) → miner log (with the 300 s
    freshness gate) → solo node estimate → explicit « inconnu » note.
    """
    logs_by_family: dict[str, list[str]] = {}
    for probe in probes.get("logs") or []:
        logs_by_family.setdefault(probe["family"], []).append(probe["path"])
    # Logs déduits de /proc/<pid>/fd/1|2 : le pid donne la famille du mineur.
    families_by_pid = {
        probe["pid"]: probe.get("family", "")
        for probe in (probes.get("fds") or [])
        if isinstance(probe.get("pid"), int)
    }
    for path, pid in (report.get("log_pids") or {}).items():
        family = families_by_pid.get(pid, "")
        if family:
            logs_by_family.setdefault(family, []).append(path)
    api_by_family: dict[str, int] = {}
    for probe in probes.get("apis") or []:
        api_by_family.setdefault(probe["family"], probe["port"])

    for miner in miners:
        if str(miner.get("kind") or "") not in ("cpu", "solo"):
            continue
        family = miner_family(
            " ".join(str(miner.get(key) or "") for key in ("binary", "name", "cmd"))
        )
        miner.setdefault("hashrate_hs", None)
        miner["hashrate_source"] = ""
        miner["hashrate_note"] = ""

        if miner.get("kind") == "solo":
            _attach_solo(miner, probes, report, now)
            continue

        tried: list[str] = []
        port = api_by_family.get(family)
        if port and report["apis"].get(port):
            payload = report["apis"][port]
            if payload.get("hashrate_hs"):
                miner["hashrate_hs"] = payload["hashrate_hs"]
                miner["hashrate_source"] = f"API :{port}/summary"
                if payload.get("threads") and not miner.get("threads"):
                    miner["threads"] = str(payload["threads"])
                continue
            tried.append(f"API :{port} sans hashrate")
        elif port:
            tried.append(f"API :{port} injoignable")

        newest_mtime: int | None = None
        for path in logs_by_family.get(family, []):
            entry = report["logs"].get(path)
            if entry is None:
                tried.append(f"log {path} illisible")
                continue
            if entry.get("mtime"):
                newest_mtime = max(newest_mtime or 0, int(entry["mtime"]))
            result = log_hashrate(entry.get("text", ""), entry.get("mtime"), now)
            if result["hashrate_hs"]:
                miner["hashrate_hs"] = result["hashrate_hs"]
                miner["hashrate_source"] = f"log {path}"
                miner["hashrate_note"] = result["line"]
                break
            tried.append(f"log {path} : {result['note']}")
        if newest_mtime:
            miner["log_mtime"] = _mtime_text(newest_mtime)
        if miner.get("hashrate_hs"):
            continue
        cpu_pct = miner.get("cpu_pct")
        cpu_value = _float(cpu_pct) if cpu_pct is not None else None
        if cpu_value is not None and cpu_value <= 0:
            # 2026.11.003-c8 : un mineur vivant à 0 % CPU n'est pas « inconnu »,
            # c'est un mineur arrêté — le dire, avec la date de son log.
            note = (
                "mineur inactif (0 % CPU, log sans hashrate récent)"
                if tried and any(item.startswith("log ") for item in tried)
                else "mineur inactif (0 % CPU, aucun log exploitable)"
            )
            if miner.get("log_mtime"):
                note += f" — log modifié le {miner['log_mtime']}"
            miner["hashrate_note"] = note
            continue
        miner["hashrate_note"] = "hashrate CPU inconnu (source : " + (
            " ; ".join(tried) if tried else "aucune API, aucun log exploitable"
        ) + ")"
    return list(miners)


def _float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _mtime_text(mtime: int) -> str:
    """Local date of a log's last write, for the diagnostic text."""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(mtime)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _usable_log_text(text: Any) -> bool:
    """Un texte de log exploitable : ni vide, ni le placeholder de journald."""
    stripped = str(text or "").strip()
    return bool(stripped) and stripped != "-- No entries --"


def _attach_solo(
    miner: dict[str, Any],
    probes: dict[str, Any],
    report: dict[str, Any],
    now: float | None = None,
) -> None:
    """Height, network share and blocks of one node-solo entry, plus an estimate."""
    coin = str(miner.get("coin") or "").upper()
    node = None
    probe: dict[str, Any] = {}
    for candidate in probes.get("nodes") or []:
        if candidate.get("coin") and candidate["coin"].upper() == coin:
            probe = candidate
            node = report["nodes"].get(candidate["key"]) or {}
            node.setdefault("key", candidate["key"])
            break
    if node is None and len(probes.get("nodes") or []) == 1:
        # Coin absent du catalogue : un seul nœud sur la machine, c'est lui.
        probe = (probes.get("nodes") or [])[0]
        node = report["nodes"].get(probe["key"]) or {}
        node.setdefault("key", probe["key"])
    if node is None:
        miner["hashrate_note"] = "hashrate CPU inconnu (source : nœud non identifié)"
        return
    log_entry: dict[str, Any] = {}
    candidates = list(probe.get("log_candidates") or node.get("log_candidates") or [])
    if not candidates and node.get("log"):
        candidates = [node["log"]["path"]]
    # 2026.11.003-c8 : le log du script est souvent découvert par
    # /proc/<pid>/fd/1 ou journald (_PID), pas par son nom.
    pids = {pid for pid in (probe.get("pids") or []) + (node.get("pids") or []) if pid}
    for path, pid in (report.get("log_pids") or {}).items():
        if pid in pids and path not in candidates:
            candidates.append(path)
    # 2026.11.004-c5 : la sonde émet un ``### JLOG <pid>`` (« -- No entries -- »
    # quand journald n'a rien pour ce pid) avant le ``### LOGI`` du même pid.
    # Un JLOG vide n'est pas un log : il est écarté, et un vrai fichier passe
    # avant le journal quel que soit l'ordre des sections.
    usable: list[str] = []
    for candidate in candidates:
        entry = report["logs"].get(candidate)
        if candidate in usable or not isinstance(entry, dict):
            continue
        if _usable_log_text(entry.get("text")):
            usable.append(candidate)
    usable.sort(key=lambda path: str(path).startswith("journal:"))
    for candidate in usable:
        log_entry = dict(report["logs"][candidate], path=candidate)
        break
    if not log_entry and _usable_log_text((node.get("log") or {}).get("text")):
        # ``### LOGS`` : la redirection du lanceur, hors ``report["logs"]``.
        entry = node["log"]
        log_entry = {"path": entry.get("path", ""), "mtime": entry.get("mtime"),
                     "text": entry.get("text", "")}
    if log_entry.get("mtime"):
        miner["log_mtime"] = _mtime_text(int(log_entry["mtime"]))
    elif log_entry.get("path") is None and node.get("log", {}).get("mtime"):
        miner["log_mtime"] = _mtime_text(int(node["log"]["mtime"]))
    if not log_entry and (node.get("log") or {}).get("path"):
        # Le chemin est connu (redirection du lanceur) mais rien n'en sort :
        # droits insuffisants ou fichier vide.  Le dire, sans inventer.
        miner["hashrate_note"] = f"log {(node['log'])['path']} illisible ou vide"
    estimate = None
    script: dict[str, Any] = {}
    if log_entry:
        # 1. Format réel du log de mine-bigcoin.sh (une ligne par minute).
        script = script_hashrate(log_entry.get("text", ""), log_entry.get("mtime"), now)
        estimate = script["hashrate_hs"]
        if estimate:
            miner["hashrate_source"] = "log du script (hashes/s/worker × workers)"
            if script.get("workers"):
                miner["workers"] = script["workers"]
            if script.get("algo"):
                miner["algorithm"] = miner.get("algorithm") or script["algo"]
        elif script.get("state") == "stale":
            miner["hashrate_note"] = script["note"]
        if estimate is None:
            estimate = scan_hashrate(log_entry.get("text", ""))
        if estimate is None:
            result = log_hashrate(log_entry.get("text", ""), log_entry.get("mtime"), now)
            estimate = result["hashrate_hs"]
        if estimate is None:
            # 2. Dernier recours seulement : nonces / intervalle.
            #    FORMAT NON VÉRIFIÉ (aucun relevé réel de ce motif).
            estimate = nonces_hashrate(
                log_entry.get("text", ""), int(miner.get("workers") or 1)
            )
            if estimate:
                miner["hashrate_source"] = (
                    "log du script (nonces / intervalle, format non vérifié)"
                )
    if estimate:
        miner["hashrate_hs"] = estimate
        miner["hashrate_source"] = miner.get("hashrate_source") or "log du script"
    else:
        miner["hashrate_note"] = miner.get("hashrate_note") or (
            "hashrate CPU inconnu (source : nœud %s, getmininginfo)" % node.get("key", "?")
        )
    miner["node"] = {
        "key": node.get("key"),
        # Le nœud interrogé peut être muet : le log porte height et difficulty.
        "height": (
            node.get("height") if node.get("height") is not None else script.get("height")
        ),
        "network_hs": node.get("network_hs"),
        "difficulty": (
            node.get("difficulty")
            if node.get("difficulty") is not None
            else script.get("difficulty")
        ),
        "algo": script.get("algo") or "",
        "share_pct": network_share_pct(miner.get("hashrate_hs"), node.get("network_hs")),
        "blocks_found": blocks_found(log_entry.get("text", "")) if log_entry else None,
        "log": log_entry.get("path") if log_entry else "",
    }


def _format_hs(value: float | None) -> str:
    """H/s → kH/s… (mêmes seuils que le tableau de bord)."""
    if not isinstance(value, (int, float)) or value <= 0:
        return "—"
    units = ("H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s")
    index = 0
    number = float(value)
    while number >= 1000 and index < len(units) - 1:
        number /= 1000
        index += 1
    return f"{number:.2f} {units[index]}" if number < 100 else f"{number:.0f} {units[index]}"


#: TDP des modèles courants (W, spécification constructeur) : c'est une
#: **estimation**, affichée comme telle et jamais confondue avec une mesure.
CPU_TDP: tuple[tuple[str, float, str], ...] = (
    ("athlon 3000g", 35.0, "Athlon 3000G"),
    ("ryzen 5 5600g", 65.0, "Ryzen 5 5600G"),
    ("ryzen 5 3600", 65.0, "Ryzen 5 3600"),
    ("i5-12400", 65.0, "Core i5-12400"),
    ("i5-4570", 84.0, "Core i5-4570"),
)
CPU_GENERIC_W = 65.0


def estimate_cpu_w(model: str | None) -> tuple[float, str]:
    """``(watts, source)`` — TDP du modèle, sinon 65 W générique (estimé)."""
    text = str(model or "").lower()
    for token, watts, label in CPU_TDP:
        if token in text:
            return watts, f"TDP du modèle ({label})"
    return CPU_GENERIC_W, "estimation générique 65 W"


def cpu_state_with(
    report: dict[str, Any],
    hive_cputemp: float | None = None,
    api_power_w: float | None = None,
    cpu_miners: Sequence[dict[str, Any]] = (),
    typed_power_w: float | None = None,
) -> dict[str, Any]:
    """Final ``cpu`` object stored per machine and shown by the API.

    Ordre des sources de puissance (§4.2), chacune nommée dans ``power_source`` :
    ``package_power`` de l'API du mineur (le Ryzen annonce 88,96 W), puis RAPL
    (``energy_uj``) ou hwmon (``power1_input``) déjà lus par la sonde, puis la
    valeur saisie sur la machine (``machines.cpu_power_w``) ; « non mesurée »
    est une absence déclarée, jamais un zéro.
    """
    state = dict(report.get("state") or {})
    for key, empty in (("load_pct", None), ("temp_c", None), ("freq_mhz", None),
                       ("power_w", None), ("cores", None), ("model", "")):
        state.setdefault(key, empty)
    state.setdefault("power_source", "")
    freq_value = state.get("freq_mhz")
    if isinstance(freq_value, (int, float)) and freq_value > 0:
        state["freq_mhz"] = round(float(freq_value), 1)
        state["freq_ghz"] = round(float(freq_value) / 1000.0, 2)
    else:
        state["freq_mhz"] = None
        state["freq_ghz"] = None
    if state.get("temp_c") is None and hive_cputemp is not None:
        state["temp_c"] = hive_cputemp
        state["temp_sensor"] = "HiveOS last_stat cputemp"
    # 2026.11.004 : une valeur 0 de l'API veut dire « non mesuré » (l'Athlon
    # renvoie package_power 0.0) — on garde RAPL/hwmon déjà lu, sinon « non mesurée ».
    if api_power_w is not None and float(api_power_w) > 0:
        state["power_w"] = api_power_w
        state["power_source"] = "API SRBMiner package_power"
    # 4e source (§4.2) : la valeur saisie sur la machine, seulement quand rien
    # n'a été mesuré — RAPL/hwmon sont déjà dans ``state`` et priment.
    if (state.get("power_w") is None or float(state["power_w"]) <= 0) and typed_power_w:
        if float(typed_power_w) > 0:
            state["power_w"] = float(typed_power_w)
            state["power_source"] = "saisie (machines.cpu_power_w)"
    # 5e source (2026.11.004-c5) : le TDP du modèle, puis 65 W générique.
    # Une estimation se déclare (``power_estimated``) et porte sa source.
    estimated = False
    if state.get("power_w") is None or float(state["power_w"]) <= 0:
        watts, source = estimate_cpu_w(state.get("model"))
        state["power_w"] = watts
        state["power_source"] = source
        estimated = True
    if state.get("power_w") is not None and float(state["power_w"]) <= 0:
        state["power_w"] = None
        state["power_source"] = "non mesurée"
    state["power_estimated"] = estimated
    state["power_source"] = state.get("power_source") or "non mesurée"
    state["freq_source"] = state.get("freq_source") or ""
    # 2026.11.004 : on n'additionne jamais deux algos. Le total du device CPU
    # n'existe que si un seul algo tourne ; sinon la liste par mineur fait foi
    # (« WAM 1,17 kH/s · KSH 6,25 kH/s »).
    groups: dict[str, float] = {}
    order: list[str] = []
    for miner in cpu_miners:
        value = miner.get("hashrate_hs")
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        label = str(miner.get("coin") or miner.get("name") or "CPU").upper()
        if label not in groups:
            groups[label] = 0.0
            order.append(label)
        groups[label] += float(value)
    state["hashrate_hs"] = groups[order[0]] if len(order) == 1 else None
    state["hashrate_by_algo"] = [
        {"label": label, "hashrate_hs": round(groups[label], 1), "human": _format_hs(groups[label])}
        for label in order
    ]
    state["hashrate_label"] = " · ".join(
        f"{item['label']} {item['human']}" for item in state["hashrate_by_algo"]
    )
    state["miners"] = [
        {
            "name": miner.get("name") or "",
            "coin": miner.get("coin") or "",
            "hashrate_hs": miner.get("hashrate_hs"),
            "threads": miner.get("threads") or "",
            "source": miner.get("hashrate_source") or "",
            "note": miner.get("hashrate_note") or "",
            "node": miner.get("node"),
        }
        for miner in cpu_miners
    ]
    return state
