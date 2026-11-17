"""Pure parsers for the ``collect_stats`` SSH command and the SRBMiner HTTP API.

No SSH, no database, no networking: every function here turns text or JSON into
plain dictionaries, so the whole module is unit-tested against fixtures.

Security
--------
``/run/hive/last_stat.json`` carries ``params.passwd`` and ``params.rig_id``
(HiveOS secrets).  Only the keys read below ever reach a returned dictionary,
so those two values can never land in a metric row, an API response or a log
line.  The parser *is* the filter; ``tests/test_stats_parser.py`` asserts that
neither the fake password nor the fake rig id appears in the parsed output.

Units
-----
HiveOS reports ``miner_stats.hs`` in the unit named by ``hs_units`` ("khs" on
the fleet), one value per GPU, in GPU order.  Everything is normalised to H/s.
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from app.hw_parser import norm_bus
from app.miners import MINER_TOKENS, mask_command, parse_miner_cmd

SECTIONS = ("HIVE_STAT", "HIVE_GPU", "NVSMI", "PS_MINERS", "UPTIME")


_API_PORT = re.compile(r"--api[-_]port[=\s]+(\d+)")

_HS_UNITS: dict[str, float] = {
    "": 1.0,
    "h": 1.0,
    "hs": 1.0,
    "h/s": 1.0,
    "kh": 1e3,
    "khs": 1e3,
    "kh/s": 1e3,
    "mh": 1e6,
    "mhs": 1e6,
    "mh/s": 1e6,
    "gh": 1e9,
    "ghs": 1e9,
    "gh/s": 1e9,
    "th": 1e12,
    "ths": 1e12,
    "th/s": 1e12,
}

_HR_UNITS = (
    ("PH/s", 1e15),
    ("TH/s", 1e12),
    ("GH/s", 1e9),
    ("MH/s", 1e6),
    ("kH/s", 1e3),
    ("H/s", 1.0),
)


# ── Small helpers ─────────────────────────────────────────────────────
def split_sections(stdout: str, names: Sequence[str] = SECTIONS) -> dict[str, str]:
    """Split ``### NAME`` sectioned output; a missing section is ``""``."""
    sections: dict[str, list[str]] = {name: [] for name in names}
    current: str | None = None
    for line in (stdout or "").splitlines():
        if line.startswith("### "):
            name = line[4:].strip()
            current = name if name in sections else None
            continue
        if current:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("["):  # nvidia-smi "[N/A]", "[Not Supported]"
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def hashrate_to_hs(value: Any, units: str | None) -> float | None:
    """Convert a hashrate in *units* (khs, mhs, gh, …) to H/s."""
    number = _to_float(value)
    if number is None:
        return None
    factor = _HS_UNITS.get((units or "").strip().lower().replace(" ", ""), 1.0)
    return number * factor


def format_hashrate(hs: float | None) -> str:
    """Human hashrate: 6.85e13 -> ``68.5 TH/s``; missing value -> ``—``."""
    if hs is None:
        return "—"
    value = float(hs)
    for label, factor in _HR_UNITS:
        if abs(value) >= factor or factor == 1.0:
            scaled = value / factor
            decimals = 1 if 0 < abs(scaled) < 100 else 0
            return f"{scaled:.{decimals}f} {label}"
    return f"{value:.0f} H/s"  # pragma: no cover - loop always returns


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


# ── HiveOS stats (last_stat.json / gpu-stats.json) ────────────────────
def _load_json(blob: str) -> dict[str, Any] | None:
    text = (blob or "").strip()
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _meta_coin_algo(meta: Any) -> tuple[str, str]:
    """Coin/algo from ``params.meta`` (``{"srbminer": {"coin": "PEARL"}}``)."""
    if not isinstance(meta, dict):
        return "", ""
    coin = str(meta.get("coin") or "")
    algo = str(meta.get("algo") or meta.get("algorithm") or "")
    for value in meta.values():
        if isinstance(value, dict):
            coin = coin or str(value.get("coin") or "")
            algo = algo or str(value.get("algo") or value.get("algorithm") or "")
    return coin, algo


def parse_hive_stat(blob: str) -> dict[str, Any]:
    """Machine-level fields from HiveOS ``last_stat.json`` (secrets dropped)."""
    data = _load_json(blob)
    if data is None:
        return {}
    params = data.get("params")
    if not isinstance(params, dict):
        return {}
    miner_stats = params.get("miner_stats")
    miner_stats = miner_stats if isinstance(miner_stats, dict) else {}
    coin, algo = _meta_coin_algo(params.get("meta"))

    accepted = rejected = None
    shares = miner_stats.get("ar")
    if isinstance(shares, (list, tuple)) and len(shares) >= 2:
        accepted, rejected = _to_int(shares[0]), _to_int(shares[1])
    accepted = _first(accepted, _to_int(miner_stats.get("accepted")))
    rejected = _first(rejected, _to_int(miner_stats.get("rejected")))

    total = _first(
        hashrate_to_hs(miner_stats.get("total_khs"), miner_stats.get("hs_units")),
        hashrate_to_hs(params.get("total_khs"), miner_stats.get("hs_units") or params.get("hs_units")),
    )

    return {
        "miner": str(params.get("miner") or "").strip().lower(),
        "algo": algo,
        "coin": coin,
        "uptime_s": _to_int(miner_stats.get("uptime")),
        "accepted": accepted,
        "rejected": rejected,
        "total_hashrate_hs": total,
        "hs": _as_list(miner_stats.get("hs")),
        "hs_units": miner_stats.get("hs_units") or params.get("hs_units"),
        "temp": _as_list(params.get("temp")),
        "fan": _as_list(params.get("fan")),
        "power": _as_list(params.get("power")),
        "cputemp": _as_list(params.get("cputemp")),
    }


def parse_hive_gpu(blob: str) -> list[dict[str, Any]]:
    """Entries of HiveOS ``gpu-stats.json`` with their array position.

    Index 0 is the CPU / iGPU on HiveOS images: the ``brand`` array is kept so
    the caller can drop it, and every other index is a real GPU whose PCI bus
    comes from the same position in ``busids``.
    """
    data = _load_json(blob)
    if data is None:
        return []
    busids = _as_list(data.get("busids"))
    brands = _as_list(data.get("brand"))
    temps = _as_list(data.get("temp"))
    fans = _as_list(data.get("fan"))
    powers = _as_list(data.get("power"))
    loads = _as_list(data.get("load"))

    def at(values: list[Any], index: int) -> Any:
        return values[index] if index < len(values) else None

    entries: list[dict[str, Any]] = []
    count = max(len(busids), len(brands), len(temps))
    for index in range(count):
        entries.append({
            "position": index,
            "brand": str(at(brands, index) or "").strip().lower(),
            "pci_bus": norm_bus(str(at(busids, index) or "")),
            "temp_c": _to_float(at(temps, index)),
            "fan_pct": _to_float(at(fans, index)),
            "power_w": _to_float(at(powers, index)),
            "load_pct": _to_float(at(loads, index)),
        })
    return entries


# ── nvidia-smi ────────────────────────────────────────────────────────
def parse_nvsmi(section: str) -> list[dict[str, Any]]:
    """Rows of ``--query-gpu=index,pci.bus_id,temperature.gpu,power.draw,fan.speed,utilization.gpu``."""
    gpus: list[dict[str, Any]] = []
    for line in (section or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("index"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        index = _to_int(parts[0])
        if index is None:
            continue
        gpus.append({
            "index": index,
            "pci_bus": norm_bus(parts[1]),
            "temp_c": _to_float(parts[2]) if len(parts) > 2 else None,
            "power_w": _to_float(parts[3]) if len(parts) > 3 else None,
            "fan_pct": _to_float(parts[4]) if len(parts) > 4 else None,
            "load_pct": _to_float(parts[5]) if len(parts) > 5 else None,
        })
    gpus.sort(key=lambda gpu: gpu["index"])
    return gpus


# ── Miners process list and uptime ────────────────────────────────────
def parse_ps_miners(section: str) -> dict[str, Any]:
    """Running miner (name + discovered ``--api-port``) from ``ps -eo args``.

    Also returns full external miner details (cmd, algorithm, pool) for UI display.
    """
    found: list[dict[str, Any]] = []
    for line in (section or "").splitlines():
        cmd = line.strip()
        if not cmd or "grep" in cmd.lower():
            continue
        binary = cmd.split()[0].split("/")[-1].lower()
        name = next((token for token in MINER_TOKENS if token in binary), "")
        if not name:
            continue
        match = _API_PORT.search(cmd)
        details = parse_miner_cmd(cmd)
        found.append({
            "name": name,
            "label": details["name"],
            "version": details["version"],
            "algorithm": details["algorithm"],
            "pool": details["pool"],
            "binary": binary,
            "api_port": int(match.group(1)) if match else None,
            "cmd": mask_command(cmd),
        })
    primary = next((m for m in found if m["api_port"]), found[0] if found else None)
    return {
        "miner": primary["name"] if primary else "",
        "api_port": primary["api_port"] if primary else None,
        "miners": found,
    }


def parse_uptime(section: str) -> int | None:
    for line in (section or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        value = _to_float(parts[0])
        if value is not None:
            return int(value)
    return None


# ── Composition ───────────────────────────────────────────────────────
def _merge_nvsmi(gpus: list[dict[str, Any]], nvsmi: list[dict[str, Any]]) -> None:
    """Fill missing values from nvidia-smi, matched by PCI bus, in place."""
    by_bus = {gpu["pci_bus"]: gpu for gpu in nvsmi if gpu["pci_bus"]}
    for gpu in gpus:
        other = by_bus.get(gpu["pci_bus"])
        if not other:
            continue
        for key in ("temp_c", "fan_pct", "power_w", "load_pct"):
            if gpu.get(key) is None:
                gpu[key] = other.get(key)


def _build_gpus(
    hive: dict[str, Any],
    hive_gpu: list[dict[str, Any]],
    nvsmi: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    temps = hive.get("temp") or []
    fans = hive.get("fan") or []
    powers = hive.get("power") or []
    hashrates = hive.get("hs") or []

    def at(values: list[Any], index: int) -> Any:
        return values[index] if index < len(values) else None

    if hive_gpu:
        slots = [entry for entry in hive_gpu if entry["brand"] != "cpu"]
        for entry in slots:
            index = entry["position"]
            gpus.append({
                "kind": "gpu",
                "pci_bus": entry["pci_bus"],
                "hashrate_hs": None,
                "temp_c": _first(entry["temp_c"], _to_float(at(temps, index))),
                "fan_pct": _first(entry["fan_pct"], _to_float(at(fans, index))),
                "power_w": _first(entry["power_w"], _to_float(at(powers, index))),
                "load_pct": entry["load_pct"],
            })
    else:
        # No gpu-stats.json: HiveOS arrays are still CPU-first (index 0), so
        # GPU i sits at index i + 1; nvidia-smi supplies the PCI bus order.
        for position, gpu in enumerate(nvsmi):
            index = position + 1 if hive else position
            gpus.append({
                "kind": "gpu",
                "pci_bus": gpu["pci_bus"],
                "hashrate_hs": None,
                "temp_c": _first(gpu["temp_c"], _to_float(at(temps, index))),
                "fan_pct": _first(gpu["fan_pct"], _to_float(at(fans, index))),
                "power_w": _first(gpu["power_w"], _to_float(at(powers, index))),
                "load_pct": gpu["load_pct"],
            })

    _merge_nvsmi(gpus, nvsmi)

    # miner_stats.hs holds one value per GPU, in GPU order (never the CPU).
    for position, value in enumerate(hashrates):
        if position >= len(gpus):
            break
        gpus[position]["hashrate_hs"] = hashrate_to_hs(value, hive.get("hs_units"))
    return gpus


def parse_collect_stats(stdout: str) -> dict[str, Any]:
    """Parse a full ``collect_stats`` output into a normalised snapshot."""
    sections = split_sections(stdout)
    hive = parse_hive_stat(sections["HIVE_STAT"])
    hive_gpu = parse_hive_gpu(sections["HIVE_GPU"])
    nvsmi = parse_nvsmi(sections["NVSMI"])
    ps = parse_ps_miners(sections["PS_MINERS"])
    uptime = parse_uptime(sections["UPTIME"])

    devices = _build_gpus(hive, hive_gpu, nvsmi)

    cputemp = [_to_float(v) for v in (hive.get("cputemp") or [])]
    cputemp = [v for v in cputemp if v is not None]
    if cputemp:
        devices.insert(0, {
            "kind": "cpu",
            "pci_bus": "",
            "hashrate_hs": None,
            "temp_c": cputemp[0],
            "fan_pct": None,
            "power_w": None,
            "load_pct": None,
        })

    total_hs = _first(hive.get("total_hashrate_hs"), None)
    if total_hs is None:
        values = [d["hashrate_hs"] for d in devices if d.get("hashrate_hs")]
        total_hs = sum(values) if values else None
    powers = [d["power_w"] for d in devices if d.get("power_w")]
    total_w = sum(powers) if powers else None

    return {
        "miner": _first(hive.get("miner"), ps["miner"], "") or "",
        "algo": hive.get("algo", ""),
        "coin": hive.get("coin", ""),
        "uptime_s": _first(uptime, hive.get("uptime_s")),
        "accepted": hive.get("accepted"),
        "rejected": hive.get("rejected"),
        "api_port": ps["api_port"],
        "total_hashrate_hs": total_hs,
        "total_power_w": total_w,
        "devices": devices,
    }


# ── SRBMiner-Multi HTTP API ───────────────────────────────────────────
def _hashrate_value(value: Any) -> float | None:
    """Hashrate from a number, a numeric string, a list or a dict of windows."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        return _to_float(value)
    if isinstance(value, list):
        for item in reversed(value):
            number = _hashrate_value(item)
            if number is not None:
                return number
        return None
    if isinstance(value, dict):
        for key in ("total", "1m", "5m", "15m", "current", "value", "hs", "hashrate"):
            if key in value:
                number = _hashrate_value(value[key])
                if number is not None:
                    return number
    return None


def parse_srbminer_api(payload: str | dict[str, Any]) -> dict[str, Any] | None:
    """Parse SRBMiner-Multi's HTTP API payload (tolerant to key variants).

    The real payload shape is not documented with a stable schema; this reads
    the commonly published keys (``algorithm``/``algo``, ``coin``,
    ``hashrate.total``, ``hashrate.devices``/``devices[].hashrate``,
    ``shares.accepted``/``rejected``) and returns H/s values.  An unreadable
    payload returns ``None`` instead of raising: the collector stays quiet
    when the API is foreign or closed.
    """
    data = payload
    if isinstance(payload, str):
        data = _load_json(payload)
    if not isinstance(data, dict):
        return None
    body = data
    for key in ("result", "mining", "stats"):
        if isinstance(data.get(key), dict):
            body = data[key]
            break

    algorithm = str(body.get("algorithm") or body.get("algo") or "").strip()
    coin = str(body.get("coin") or "").strip()
    miner = str(body.get("miner") or body.get("name") or "srbminer").strip()

    hashrate = body.get("hashrate")
    total = _hashrate_value(hashrate.get("total")) if isinstance(hashrate, dict) else _hashrate_value(hashrate)
    devices: list[float] = []
    raw_devices = None
    if isinstance(hashrate, dict):
        raw_devices = hashrate.get("devices") or hashrate.get("gpus")
    raw_devices = raw_devices or body.get("devices") or body.get("gpus")
    if isinstance(raw_devices, list):
        for item in raw_devices:
            if isinstance(item, dict):
                number = _hashrate_value(item.get("hashrate") or item.get("hs") or item.get("speed"))
            else:
                number = _hashrate_value(item)
            if number is not None:
                devices.append(number)

    accepted = rejected = None
    shares = body.get("shares")
    if isinstance(shares, dict):
        accepted = _to_int(shares.get("accepted"))
        rejected = _to_int(shares.get("rejected"))
    accepted = _first(accepted, _to_int(body.get("accepted")))
    rejected = _first(rejected, _to_int(body.get("rejected")))

    if total is None and devices:
        total = sum(devices)
    return {
        "miner": miner,
        "algo": algorithm,
        "coin": coin,
        "uptime_s": _to_int(body.get("uptime")),
        "accepted": accepted,
        "rejected": rejected,
        "total_hashrate_hs": total,
        "device_hashrates_hs": devices,
    }


def merge_srbminer(stats: dict[str, Any], api: dict[str, Any] | None) -> dict[str, Any]:
    """Complete a parsed snapshot with SRBMiner API data (never overwrites)."""
    if not api:
        return stats
    if not stats.get("algo"):
        stats["algo"] = api.get("algo", "")
    if not stats.get("coin"):
        stats["coin"] = api.get("coin", "")
    if not stats.get("miner"):
        stats["miner"] = (api.get("miner") or "").strip().lower()
    for key in ("accepted", "rejected", "uptime_s"):
        if stats.get(key) is None:
            stats[key] = api.get(key)
    if stats.get("total_hashrate_hs") is None:
        stats["total_hashrate_hs"] = api.get("total_hashrate_hs")

    gpus = [d for d in stats.get("devices", []) if d.get("kind") == "gpu"]
    per_device = api.get("device_hashrates_hs") or []
    for gpu, value in zip(gpus, per_device):
        if gpu.get("hashrate_hs") is None:
            gpu["hashrate_hs"] = value
    if stats.get("total_hashrate_hs") is None:
        values = [g["hashrate_hs"] for g in gpus if g.get("hashrate_hs")]
        if values:
            stats["total_hashrate_hs"] = sum(values)
    return stats
