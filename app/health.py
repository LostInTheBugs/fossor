"""État de santé des machines (§4.19) — calcul pur, sans base ni réseau.

Une pastille par machine : ``green`` (tout va bien), ``orange`` (à surveiller)
ou ``red`` (hors service).  La première raison rencontrée est rendue en une
ligne, dans l'ordre de gravité : hors ligne, mineur muet, device sans
hashrate, hashrate effondré, température, alimentation.

Les seuils sont réglables dans Configuration ; ils vivent dans la table
``settings`` sous les clés ``health_*`` et retombent sur :data:`DEFAULTS`.
"""

from __future__ import annotations

from typing import Any

#: Seuils par défaut (§4.19).  Une valeur absente ou illisible reprend le défaut.
DEFAULTS: dict[str, float] = {
    "hashrate_min_pct": 70.0,
    "gpu_temp_max_c": 80.0,
    "cpu_temp_max_c": 90.0,
    "psu_warn_pct": 80.0,
    "psu_crit_pct": 90.0,
}

#: Clé de réglage -> nom du seuil.
SETTING_KEYS: dict[str, str] = {
    "health_hashrate_min_pct": "hashrate_min_pct",
    "health_gpu_temp_max_c": "gpu_temp_max_c",
    "health_cpu_temp_max_c": "cpu_temp_max_c",
    "health_psu_warn_pct": "psu_warn_pct",
    "health_psu_crit_pct": "psu_crit_pct",
}

BOUNDS: dict[str, tuple[float, float]] = {
    "hashrate_min_pct": (1.0, 100.0),
    "gpu_temp_max_c": (40.0, 120.0),
    "cpu_temp_max_c": (40.0, 120.0),
    "psu_warn_pct": (10.0, 100.0),
    "psu_crit_pct": (10.0, 100.0),
}


def normalize(values: dict[str, Any] | None) -> dict[str, float]:
    """Seuils complets et bornés : toute valeur douteuse retombe sur le défaut."""
    out = dict(DEFAULTS)
    for name, value in (values or {}).items():
        if name not in DEFAULTS:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        low, high = BOUNDS[name]
        out[name] = round(min(high, max(low, number)), 2)
    if out["psu_crit_pct"] <= out["psu_warn_pct"]:
        out["psu_crit_pct"] = min(100.0, out["psu_warn_pct"] + 1.0)
    return out


def thresholds(db=None) -> dict[str, float]:
    """Seuils lus dans les réglages (``db`` absent : défauts)."""
    if db is None:
        return dict(DEFAULTS)
    from app.ai_config import get_setting

    raw = {name: get_setting(db, key, DEFAULTS[name]) for key, name in SETTING_KEYS.items()}
    return normalize(raw)


def save_thresholds(db, values: dict[str, Any]) -> dict[str, float]:
    """Écrit les seuils (validation stricte) et rend la valeur retenue."""
    from app.ai_config import set_setting

    current = thresholds(db)
    try:
        warn = float((values or {}).get("psu_warn_pct", current["psu_warn_pct"]))
        crit = float((values or {}).get("psu_crit_pct", current["psu_crit_pct"]))
    except (TypeError, ValueError):
        raise ValueError("Seuils d'alimentation invalides")
    if crit <= warn:
        raise ValueError("Le seuil rouge d'alimentation doit dépasser le seuil orange")
    for key, name in SETTING_KEYS.items():
        if name not in (values or {}):
            continue
        try:
            number = float(values[name])
        except (TypeError, ValueError):
            raise ValueError(f"Seuil {name} invalide : {values[name]!r}")
        low, high = BOUNDS[name]
        if not low <= number <= high:
            raise ValueError(f"Seuil {name} hors bornes ({low:g}–{high:g})")
        current[name] = number
    current = normalize(current)
    if current["psu_crit_pct"] <= current["psu_warn_pct"]:
        raise ValueError("Le seuil rouge d'alimentation doit dépasser le seuil orange")
    for key, name in SETTING_KEYS.items():
        set_setting(db, key, current[name])
    db.commit()
    return current


def _device_label(device: dict) -> str:
    if device.get("kind") == "cpu":
        return "CPU"
    index = device.get("index")
    return "GPU" if index is None else f"GPU {index}"


def _with_reason(status: str, reason: str, **extra) -> dict[str, Any]:
    return {"status": status, "reason": reason if status != "green" else "", **extra}


def evaluate(
    machine: dict,
    averages: dict[str, float] | None = None,
    limits: dict[str, float] | None = None,
    psu_pct: float | None = None,
) -> dict[str, Any]:
    """État d'une machine du relevé ``/api/metrics/live``.

    ``averages`` : hashrate moyen sur une heure, par ``device_uuid`` (et la clé
    ``"__machine__"`` pour le total).  ``psu_pct`` : charge de l'alimentation la
    plus chargée, en pourcentage de sa puissance disponible.
    """
    limits = normalize(limits)
    averages = averages or {}
    devices = [d for d in (machine.get("devices") or []) if d.get("present", True)]
    # §4.19 : un GPU intégré non sélectionné n'existe pas pour l'état de santé.
    devices = [d for d in devices if not (d.get("integrated") and not d.get("enabled_for_mining"))]

    if not machine.get("collected"):
        return _with_reason("orange", "jamais collectée")
    if not machine.get("online"):
        detail = (machine.get("error") or "").strip()
        return _with_reason("red", "hors ligne", detail=detail)

    miner = (machine.get("api_error") or "").strip()
    if miner:
        return _with_reason("orange", f"mineur : {miner}")

    if isinstance(psu_pct, (int, float)):
        if psu_pct > limits["psu_crit_pct"]:
            return _with_reason("red", f"alimentation à {psu_pct:.0f} %")
        if psu_pct > limits["psu_warn_pct"]:
            return _with_reason("orange", f"alimentation à {psu_pct:.0f} %")

    for device in devices:
        if not device.get("enabled_for_mining"):
            continue
        if not device.get("hashrate_hs"):
            return _with_reason("orange", f"{_device_label(device)} sans hashrate")

    total = machine.get("total_hashrate_hs")
    reference = averages.get("__machine__")
    if isinstance(total, (int, float)) and isinstance(reference, (int, float)) and reference > 0:
        ratio = 100.0 * float(total) / float(reference)
        if ratio < limits["hashrate_min_pct"]:
            return _with_reason(
                "orange",
                f"hashrate à {ratio:.0f} % de la moyenne 1 h",
                hashrate_pct=round(ratio, 1),
            )

    for device in devices:
        temp = device.get("temp_c")
        if not isinstance(temp, (int, float)):
            continue
        ceiling = limits["cpu_temp_max_c"] if device.get("kind") == "cpu" else limits["gpu_temp_max_c"]
        if temp > ceiling:
            return _with_reason("orange", f"{_device_label(device)} à {temp:.0f} °C")

    cpu = machine.get("cpu") or {}
    cpu_temp = cpu.get("temp_c")
    if isinstance(cpu_temp, (int, float)) and cpu_temp > limits["cpu_temp_max_c"]:
        return _with_reason("orange", f"CPU à {cpu_temp:.0f} °C")

    return _with_reason("green", "")
