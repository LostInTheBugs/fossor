"""Alimentations (PSU), charge et puissance à la prise (§4.2 / §4.2 bis).

Calculs purs, sans SSH ni base : :func:`compute_power` reçoit les alimentations,
les composants inventoriés et les devices d'une machine et renvoie la charge de
chaque PSU, son taux de charge, son rendement interpolé et la puissance à la
prise.

Courbes 80 PLUS en 230 V (EU), aux trois points officiels 20 / 50 / 100 % de
charge — table du code, donc vérifiable :

    certification   20 %   50 %   100 %
    aucune          78     80     78
    White           80     82     80
    Bronze          85     88     85
    Silver          87     90     87
    Gold            90     92     89
    Platinum        92     94     90
    Titanium        94     96     94

Rappel de la SPEC : la certification **ne réduit pas** la puissance que
l'alimentation peut fournir. Une 750 W sort 750 W en DC, qu'elle soit Bronze ou
Platinum ; la certification ne fixe que le rendement, donc ce qui est tiré à la
prise et la chaleur dégagée. La puissance disponible est
``min(nominale, A12V × 12) × déclassement``.
"""

from __future__ import annotations

from typing import Any, Sequence

# Rendement (%) à 20 / 50 / 100 % de charge, 230 V.
EFFICIENCY_CURVES: dict[str, tuple[float, float, float]] = {
    "none": (78.0, 80.0, 78.0),
    "white": (80.0, 82.0, 80.0),
    "bronze": (85.0, 88.0, 85.0),
    "silver": (87.0, 90.0, 87.0),
    "gold": (90.0, 92.0, 89.0),
    "platinum": (92.0, 94.0, 90.0),
    "titanium": (94.0, 96.0, 94.0),
}

CERTIFICATIONS = tuple(EFFICIENCY_CURVES)

CERTIFICATION_LABELS = {
    "none": "aucune",
    "white": "White",
    "bronze": "Bronze",
    "silver": "Silver",
    "gold": "Gold",
    "platinum": "Platinum",
    "titanium": "Titanium",
}

# Préréglages courants (§4.2 bis), tout reste modifiable.
PSU_PRESETS = (
    {"name": "PSU 1", "rated_w": 650, "certification": "gold"},
    {"name": "PSU 1", "rated_w": 750, "certification": "gold"},
    {"name": "PSU 1", "rated_w": 850, "certification": "platinum"},
    {"name": "PSU 1", "rated_w": 1200, "certification": "platinum"},
    {"name": "PSU 1", "rated_w": 1600, "certification": "titanium"},
)

# Estimation d'une carte graphique sans mesure (W) : modèles courants d'abord,
# 150 W sinon — une valeur annoncée comme estimée, jamais comme mesurée.
GPU_TDP: tuple[tuple[str, float], ...] = (
    ("4090", 450.0), ("4080", 320.0), ("3090", 350.0), ("3080", 320.0),
    ("3070", 220.0), ("3060 ti", 200.0), ("3060", 170.0), ("2080", 215.0),
    ("2070", 185.0), ("1660", 120.0), ("1080", 180.0), ("1070", 150.0),
    ("1060", 120.0), ("6900", 300.0), ("6800", 250.0), ("6700", 175.0),
    ("6600", 132.0), ("580", 185.0), ("vega 64", 295.0), ("vega 56", 210.0),
)
GPU_DEFAULT_W = 150.0
CPU_DEFAULT_W = 65.0

ALERT_ORANGE_PCT = 80.0
ALERT_RED_PCT = 90.0


def normalize_certification(value: Any) -> str:
    """``Gold``/``80+ standard``/``aucune`` -> clé de :data:`EFFICIENCY_CURVES`."""
    token = str(value or "none").strip().lower()
    aliases = {
        "": "none", "aucune": "none", "no": "none", "0": "none",
        "standard": "white", "80+ white": "white", "80 plus white": "white",
        "80+ standard": "white", "80 plus standard": "white",
        "80+ bronze": "bronze", "80+ silver": "silver", "80+ gold": "gold",
        "80+ platinum": "platinum", "80+ titanium": "titanium",
    }
    token = aliases.get(token, token)
    if token not in EFFICIENCY_CURVES:
        raise ValueError(
            "certification 80 PLUS inconnue : attendu "
            + ", ".join(CERTIFICATIONS)
        )
    return token


def certification_label(certification: str) -> str:
    return CERTIFICATION_LABELS.get(normalize_certification(certification), "")


def efficiency_pct(
    certification: str,
    load_pct: float,
    measured_efficiency: float | None = None,
) -> float:
    """Rendement interpolé à *load_pct* ; un rendement mesuré le remplace.

    Interpolation linéaire entre 20, 50 et 100 %. En dessous de 20 % la valeur
    du point 20 % est retenue (les courbes officielles ne descendent pas plus
    bas) ; au-delà de 100 % celle du point 100 %.
    """
    if measured_efficiency is not None:
        value = float(measured_efficiency)
        if 50.0 <= value <= 100.0:
            return value
    low, mid, high = EFFICIENCY_CURVES[normalize_certification(certification)]
    pct = max(0.0, float(load_pct))
    if pct <= 20.0:
        return low
    if pct <= 50.0:
        return low + (mid - low) * (pct - 20.0) / 30.0
    if pct <= 100.0:
        return mid + (high - mid) * (pct - 50.0) / 50.0
    return high


def available_w(
    rated_w: float,
    amps_12v: float | None = None,
    derate_pct: float = 100.0,
) -> float:
    """Puissance DC réellement disponible : min(nominale, A12V × 12) × déclassement."""
    rated = max(0.0, float(rated_w or 0.0))
    if amps_12v:
        rated = min(rated, float(amps_12v) * 12.0)
    factor = max(0.0, min(100.0, float(derate_pct if derate_pct is not None else 100.0))) / 100.0
    return round(rated * factor, 1)


def estimate_gpu_w(model: str | None) -> float:
    """Estimation d'une carte sans mesure (W), d'après son modèle."""
    text = (model or "").lower()
    for token, watts in GPU_TDP:
        if token in text:
            return watts
    return GPU_DEFAULT_W


def _status(load_pct: float, has_load: bool) -> str:
    if not has_load:
        return "spare"
    if load_pct > ALERT_RED_PCT:
        return "red"
    if load_pct > ALERT_ORANGE_PCT:
        return "orange"
    return "ok"


def compute_power(
    psus: Sequence[dict[str, Any]],
    components: Sequence[dict[str, Any]] = (),
    devices: Sequence[dict[str, Any]] = (),
    machine_power_w: float | None = None,
) -> dict[str, Any]:
    """Charge et puissance à la prise de chaque alimentation.

    *psus* : ``id``, ``name``, ``rated_w``, ``certification``, ``amps_12v``,
    ``derate_pct``, ``measured_efficiency``, ``is_default``.
    *components* : ``kind``, ``label``, ``watts``, ``quantity``, ``psu_id``,
    ``measured``.
    *devices* : ``kind`` (cpu/gpu), ``model``, ``power_w`` (mesuré ou ``None``),
    ``psu_id``, ``psu2_id``, ``split_w``, ``enabled``, ``integrated``.

    Un GPU peut être réparti sur deux PSU : la part ``split_w`` (75 W par défaut,
    slot/riser) sur ``psu_id``, le reste sur ``psu2_id``. Les devices intégrés
    sont exclus du calcul (jamais minés). Ce qui n'est pas affecté va sur
    l'alimentation par défaut, et un GPU sans affectation est signalé.
    """
    entries = [dict(psu) for psu in psus]
    default_id = next((p["id"] for p in entries if p.get("is_default")), None)
    if default_id is None and entries:
        default_id = entries[0]["id"]

    loads = {p["id"]: 0.0 for p in entries}
    measured = {p["id"]: 0.0 for p in entries}
    labels: dict[Any, list[dict[str, Any]]] = {p["id"]: [] for p in entries}

    def bucket(psu_id: Any) -> Any:
        return psu_id if psu_id in loads else default_id

    def add(psu_id: Any, watts: float, label: str, is_measured: bool) -> None:
        target = bucket(psu_id)
        if target is None or watts <= 0:
            return
        loads[target] = round(loads[target] + watts, 1)
        if is_measured:
            measured[target] = round(measured[target] + watts, 1)
        labels[target].append({"label": label, "watts": round(watts, 1)})

    total_dc = 0.0
    total_measured = 0.0
    devices_w = 0.0
    components_w = 0.0
    gpus_without_psu: list[str] = []

    for component in components:
        quantity = max(0, int(component.get("quantity") or 0))
        watts = max(0.0, float(component.get("watts") or 0.0)) * quantity
        if watts <= 0:
            continue
        components_w += watts
        total_dc += watts
        add(component.get("psu_id"), watts,
            f"{component.get('label') or component.get('kind')} ×{quantity}",
            bool(component.get("measured")))

    for device in devices:
        if device.get("enabled") is False or device.get("integrated"):
            continue
        kind = device.get("kind") or "gpu"
        measured_w = device.get("power_w")
        if measured_w is not None and float(measured_w) > 0:
            watts = float(measured_w)
            is_measured = True
        elif kind == "cpu":
            watts = float(machine_power_w or CPU_DEFAULT_W)
            is_measured = False
        else:
            watts = estimate_gpu_w(device.get("model"))
            is_measured = False
        if watts <= 0:
            continue
        devices_w += watts
        total_dc += watts
        if is_measured:
            total_measured += watts
        label = device.get("label") or device.get("model") or kind.upper()
        if kind == "cpu":
            add(device.get("psu_id"), watts, label, is_measured)
            continue
        psu_id, psu2_id = device.get("psu_id"), device.get("psu2_id")
        if psu_id is None and psu2_id is None:
            gpus_without_psu.append(label)
            add(None, watts, label, is_measured)
            continue
        if psu2_id is not None and psu_id is not None:
            split = device.get("split_w")
            slot = 75.0 if split is None else max(0.0, float(split))
            slot = min(slot, watts)
            add(psu_id, slot, f"{label} (slot/riser)", is_measured)
            add(psu2_id, watts - slot, f"{label} (reste)", is_measured)
        elif psu_id is not None:
            add(psu_id, watts, label, is_measured)
        else:
            # 2026.11.004-c5 : une seconde PSU seule porte la carte entière —
            # la répartition slot/riser exige les deux alimentations.
            add(psu2_id, watts, label, is_measured)

    rows: list[dict[str, Any]] = []
    total_wall = 0.0
    worst_pct = 0.0
    worst_name = ""
    alerts: list[dict[str, Any]] = []
    for psu in entries:
        load = round(loads.get(psu["id"], 0.0), 1)
        available = available_w(
            psu.get("rated_w") or 0,
            psu.get("amps_12v"),
            psu.get("derate_pct", 100),
        )
        pct = round(load / available * 100.0, 1) if available > 0 else 0.0
        eff = efficiency_pct(
            psu.get("certification") or "none", pct, psu.get("measured_efficiency")
        )
        wall = round(load / (eff / 100.0), 1) if load > 0 else 0.0
        total_wall += wall
        status = _status(pct, load > 0)
        if load > 0 and pct > worst_pct:
            worst_pct, worst_name = pct, psu.get("name") or ""
        if status in ("orange", "red"):
            alerts.append({
                "level": status,
                "psu_id": psu["id"],
                "psu": psu.get("name") or "",
                "load_pct": pct,
                "message": (
                    f"{psu.get('name')} à {pct} % de sa puissance disponible"
                    + (" — surchargée" if status == "red" else " — proche de la limite")
                ),
            })
        rows.append({
            "id": psu["id"],
            "name": psu.get("name") or "",
            "rated_w": float(psu.get("rated_w") or 0.0),
            "certification": normalize_certification(psu.get("certification")),
            "certification_label": certification_label(psu.get("certification")),
            "amps_12v": psu.get("amps_12v"),
            "derate_pct": float(psu.get("derate_pct") or 100.0),
            "measured_efficiency": psu.get("measured_efficiency"),
            "is_default": psu["id"] == default_id,
            "available_dc_w": available,
            "load_dc_w": load,
            "load_pct": pct,
            "efficiency_pct": round(eff, 1),
            "wall_w": wall,
            "measured_w": round(measured.get(psu["id"], 0.0), 1),
            "estimated_w": round(load - measured.get(psu["id"], 0.0), 1),
            "status": status,
            "spare": load <= 0,
            "items": labels.get(psu["id"], []),
        })

    total_dc = round(total_dc, 1)
    total_wall = round(total_wall, 1)
    if not entries:
        total_wall = total_dc
    if len(rows) > 1 and rows:
        alerts.append({
            "level": "info",
            "psu_id": None,
            "psu": "",
            "load_pct": round(worst_pct, 1),
            "message": f"pire charge : {worst_name} à {round(worst_pct, 1)} %",
        })
    if gpus_without_psu:
        alerts.append({
            "level": "orange",
            "psu_id": None,
            "psu": "",
            "load_pct": 0.0,
            "message": "GPU sans alimentation affectée : " + ", ".join(gpus_without_psu),
        })
    return {
        "psus": rows,
        "total_dc_w": total_dc,
        "total_wall_w": total_wall,
        "measured_w": round(total_measured, 1),
        "estimated_w": round(total_dc - total_measured, 1) if entries else total_dc,
        "devices_w": round(devices_w, 1),
        "components_w": round(components_w, 1),
        "worst_load_pct": round(worst_pct, 1),
        "worst_psu": worst_name,
        "gpus_without_psu": gpus_without_psu,
        "alerts": alerts,
        "wall_factor": round(total_wall / total_dc, 3) if total_dc > 0 else 1.0,
    }
