"""SRBMiner-MULTI HTTP API parser — real 3.8.x shape (2026.11.003-c6).

The 3.8.x answer has no flat ``hashrate.total``/``devices`` block: hashrates
live per algorithm (``algorithms[].hashrate.gpu.total`` and
``algorithms[].hashrate.gpu.gpuN``), devices are described once in
``gpu_devices[]`` with their PCI ``bus_id``/``topology_id``, and every
algorithm carries its own pool and share counters.

Never read or store ``pool.wallet``: only ``pool.pool``, the shares and the
hashrates leave this module.
"""

from __future__ import annotations

import re
from typing import Any

from app.coins import coin_for_algo, coin_for_pool
from app.hw_parser import norm_bus
from app.stats_parser import (
    _as_list,
    _first,
    _hashrate_value,
    _load_json,
    _to_float,
    _to_int,
)


def _gpu_bus(entry: dict[str, Any]) -> str:
    """PCI bus of a ``gpu_devices`` entry (``0000:05:00.0`` → ``05:00.0``)."""
    topology = str(entry.get("topology_id") or "").strip()
    if topology:
        normalised = norm_bus(topology)
        if normalised:
            return normalised
    bus = entry.get("bus_id")
    try:
        number = int(bus)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return norm_bus("0000:%02x:00.0" % number)


def _gpu_devices(body: dict[str, Any]) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for position, entry in enumerate(_as_list(body.get("gpu_devices"))):
        if not isinstance(entry, dict):
            continue
        index = _to_int(entry.get("id"))
        devices.append({
            "index": position if index is None else index,
            "pci_bus": _gpu_bus(entry),
            "model": str(entry.get("model") or ""),
            "vendor": str(entry.get("vendor") or ""),
            "temp_c": _to_float(entry.get("temperature")),
            "power_w": _to_float(entry.get("asic_power")),
            "hashrate_hs": None,
        })
    devices.sort(key=lambda item: item["index"])
    return devices


def _package_power(body: dict[str, Any]) -> float | None:
    """CPU package power reported by SRBMiner (``package_power``, e.g. 88.96 W).

    2026.11.003-c8 : la forme réelle de SRBMiner 3.8.x est
    ``cpu_devices[0].package_power`` (88.96 W sur le Ryzen) — la clé racine
    ``package_power`` n'existe pas, d'où une puissance vide en production.
    """
    for key in ("package_power", "cpu_package_power"):
        value = body.get(key)
        if value is not None:
            return _to_float(value)
    cpu_block = body.get("cpu")
    if isinstance(cpu_block, dict):
        value = cpu_block.get("package_power") or cpu_block.get("cpu_package_power")
        if value is not None:
            return _to_float(value)
        devices = cpu_block.get("devices")
        if isinstance(devices, list):
            total = _sum_package_power(devices)
            if total is not None:
                return total
    devices = body.get("cpu_devices")
    if isinstance(devices, list):
        return _sum_package_power(devices)
    return None


def _sum_package_power(devices: list[Any]) -> float | None:
    """Sum ``package_power`` of every CPU device (one per socket/CCD)."""
    total = 0.0
    seen = False
    for entry in devices:
        if not isinstance(entry, dict):
            continue
        value = _to_float(entry.get("package_power"))
        if value is None:
            value = _to_float(entry.get("power"))
        if value is not None:
            total += value
            seen = True
    return round(total, 2) if seen else None


def _parse_algorithms(body: dict[str, Any]) -> dict[str, Any] | None:
    """One entry per algorithm: total, per-GPU hashrate, shares, pool, coin."""
    algorithms: list[dict[str, Any]] = []
    cpu_totals: list[float] = []
    for entry in _as_list(body.get("algorithms")):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("algorithm") or "").strip()
        pool_block = entry.get("pool") if isinstance(entry.get("pool"), dict) else {}
        pool = str(pool_block.get("pool") or entry.get("pool") or "").strip()
        hashrate = entry.get("hashrate") if isinstance(entry.get("hashrate"), dict) else {}
        gpu_block = hashrate.get("gpu") if isinstance(hashrate.get("gpu"), dict) else {}
        cpu_block = hashrate.get("cpu")

        per_gpu: dict[int, float] = {}
        for key, value in gpu_block.items():
            match = re.fullmatch(r"gpu(\d+)", str(key))
            if not match:
                continue
            number = _hashrate_value(value)
            if number is not None:
                per_gpu[int(match.group(1))] = number
        gpu_total = _hashrate_value(gpu_block.get("total"))
        if gpu_total is None and per_gpu:
            gpu_total = sum(per_gpu.values())
        cpu_total = (
            _hashrate_value(cpu_block.get("total"))
            if isinstance(cpu_block, dict)
            else _hashrate_value(cpu_block)
        )
        shares = entry.get("shares") if isinstance(entry.get("shares"), dict) else {}
        if cpu_total is not None:
            cpu_totals.append(cpu_total)
        total = (gpu_total or 0.0) + (cpu_total or 0.0)
        algorithms.append({
            "algo": name,
            "coin": coin_for_pool(pool) or coin_for_algo(name),
            "pool": pool,
            "hashrate_hs": total if total else None,
            "gpu_hashrates": per_gpu,
            "accepted": _to_int(shares.get("accepted")),
            "rejected": _to_int(shares.get("rejected")),
        })

    if not algorithms:
        return None

    devices = _gpu_devices(body)
    for algorithm in algorithms:
        for index, value in algorithm["gpu_hashrates"].items():
            if 0 <= index < len(devices):
                current = devices[index]["hashrate_hs"] or 0.0
                devices[index]["hashrate_hs"] = current + value

    totals = [algorithm["hashrate_hs"] for algorithm in algorithms
              if algorithm["hashrate_hs"] is not None]
    accepted = [algorithm["accepted"] for algorithm in algorithms
                if algorithm["accepted"] is not None]
    rejected = [algorithm["rejected"] for algorithm in algorithms
                if algorithm["rejected"] is not None]
    powers = [device["power_w"] for device in devices if device["power_w"] is not None]

    first = algorithms[0]
    return {
        "miner": str(body.get("miner") or "srbminer").strip(),
        "algo": first["algo"],
        "coin": first["coin"],
        "uptime_s": _first(_to_int(body.get("mining_time")), _to_int(body.get("uptime"))),
        "accepted": sum(accepted) if accepted else None,
        "rejected": sum(rejected) if rejected else None,
        "total_hashrate_hs": sum(totals) if totals else None,
        "device_hashrates_hs": [
            device["hashrate_hs"] for device in devices
            if device["hashrate_hs"] is not None
        ],
        "gpus": devices,
        "algos": algorithms,
        "cpu_hashrate_hs": sum(cpu_totals) if cpu_totals else None,
        "total_power_w": sum(powers) if powers else None,
    }


def _parse_flat(body: dict[str, Any]) -> dict[str, Any] | None:
    """Legacy / HiveOS-like flat payload (``hashrate.total`` + ``devices``)."""
    algorithm = str(body.get("algorithm") or body.get("algo") or "").strip()
    coin = str(body.get("coin") or "").strip()
    miner = str(body.get("miner") or body.get("name") or "srbminer").strip()

    hashrate = body.get("hashrate")
    total = (
        _hashrate_value(hashrate.get("total"))
        if isinstance(hashrate, dict)
        else _hashrate_value(hashrate)
    )
    devices: list[float] = []
    raw_devices = None
    if isinstance(hashrate, dict):
        raw_devices = hashrate.get("devices") or hashrate.get("gpus")
    raw_devices = raw_devices or body.get("devices") or body.get("gpus")
    if isinstance(raw_devices, list):
        for item in raw_devices:
            if isinstance(item, dict):
                number = _hashrate_value(
                    item.get("hashrate") or item.get("hs") or item.get("speed")
                )
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
        "gpus": [],
        "algos": [],
        "cpu_hashrate_hs": None,
        "total_power_w": None,
    }


def distinct_coins(miners: list[dict[str, Any]]) -> list[str]:
    """Coins mined by one machine, in display order, without duplicates."""
    coins: list[str] = []
    for miner in miners:
        ticker = str(miner.get("coin") or "").strip().upper()
        if ticker and ticker not in coins:
            coins.append(ticker)
    return coins


def expand_miner_algos(
    miners: list[dict[str, Any]],
    algos: list[dict[str, Any]],
    aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One line per algorithm for the miner whose API answered (003-c6).

    A single SRBMiner process mines PRL + WAM + KSH: the dashboard needs one
    entry per algorithm with its own coin and hashrate.  The API port stays on
    the first line so the process is not counted several times.
    """
    from app.external_miners import normalize_coin

    expanded: list[dict[str, Any]] = []
    for miner in miners:
        if not miner.get("api_port"):
            expanded.append(miner)
            continue
        for position, algo in enumerate(algos):
            entry = dict(miner)
            entry["algorithm"] = algo.get("algo") or miner.get("algorithm") or ""
            entry["coin"] = normalize_coin(
                algo.get("coin") or miner.get("coin") or "", aliases
            )
            entry["pool"] = algo.get("pool") or miner.get("pool") or ""
            entry["hashrate_hs"] = algo.get("hashrate_hs")
            if position:
                entry["api_port"] = None
            expanded.append(entry)
    return expanded


def parse_srbminer_api(payload: str | dict[str, Any]) -> dict[str, Any] | None:
    """Normalise an SRBMiner-MULTI API answer; ``None`` when unreadable."""
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
    result = (
        _parse_algorithms(body)
        if isinstance(body.get("algorithms"), list)
        else _parse_flat(body)
    )
    # 2026.11.003-c7: CPU package power, whatever the payload shape.
    if result is not None and result.get("cpu_package_power_w") is None:
        result["cpu_package_power_w"] = _package_power(body)
    return result


def merge_srbminer(
    stats: dict[str, Any], api: dict[str, Any] | None
) -> dict[str, Any]:
    """Complete a parsed snapshot with API data (never overwrites a value)."""
    if not api:
        return stats
    if not stats.get("algo"):
        stats["algo"] = api.get("algo", "")
    if not stats.get("miner"):
        stats["miner"] = (api.get("miner") or "").strip().lower()
    for key in ("accepted", "rejected", "uptime_s"):
        if stats.get(key) is None:
            stats[key] = api.get(key)
    if stats.get("total_hashrate_hs") is None:
        stats["total_hashrate_hs"] = api.get("total_hashrate_hs")
    if stats.get("total_power_w") is None and api.get("total_power_w") is not None:
        stats["total_power_w"] = api["total_power_w"]
    if stats.get("cpu_package_power_w") is None and api.get("cpu_package_power_w") is not None:
        stats["cpu_package_power_w"] = api["cpu_package_power_w"]

    gpus = [d for d in stats.get("devices", []) if d.get("kind") == "gpu"]
    api_gpus = [gpu for gpu in (api.get("gpus") or []) if gpu.get("pci_bus")]
    if api_gpus:
        # Attach each GPU hashrate to its device through the PCI bus, never by
        # position: gpu0 is not necessarily the first card of the machine.
        by_bus = {norm_bus(gpu["pci_bus"]): gpu for gpu in api_gpus}
        for gpu in gpus:
            source = by_bus.get(norm_bus(gpu.get("pci_bus")))
            if not source:
                continue
            for key in ("hashrate_hs", "power_w", "temp_c"):
                if gpu.get(key) is None and source.get(key) is not None:
                    gpu[key] = source[key]
    else:
        for gpu, value in zip(gpus, api.get("device_hashrates_hs") or []):
            if gpu.get("hashrate_hs") is None:
                gpu["hashrate_hs"] = value

    cpu = next((d for d in stats.get("devices", []) if d.get("kind") == "cpu"), None)
    api_cpu = api.get("cpu_hashrate_hs")
    if cpu is not None and cpu.get("hashrate_hs") is None and api_cpu:
        cpu["hashrate_hs"] = api_cpu
    # CPU miner (cpuminer/SRBMiner --cpu-threads): the API total belongs to
    # the CPU device when no GPU reported a hashrate.
    if (
        cpu is not None
        and cpu.get("hashrate_hs") is None
        and stats.get("cpu_miners")
        and api.get("total_hashrate_hs") is not None
        and not any(g.get("hashrate_hs") for g in gpus)
    ):
        cpu["hashrate_hs"] = api["total_hashrate_hs"]

    if api.get("algos") and not stats.get("srbminer_algos"):
        stats["srbminer_algos"] = api["algos"]
    if stats.get("total_hashrate_hs") is None:
        values = [g["hashrate_hs"] for g in gpus if g.get("hashrate_hs")]
        if values:
            stats["total_hashrate_hs"] = sum(values)
    return stats
