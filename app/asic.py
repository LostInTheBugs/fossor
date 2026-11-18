"""ASIC en lecture seule (§4.20) — parseurs purs, sans réseau ni base.

Trois familles de pilotes :

* **AxeOS** (Bitaxe / NerdQaxe) : ``GET /api/system/info``.  Hashrate en GH/s,
  nominal dans ``expectedHashrate``, domaines de hachage dans
  ``hashrateMonitor.asics[].domains``.  Format **vérifié** sur une fixture
  réelle (AxeOS v2.12.2, Gamma 602).
* **cgminer / bmminer / BOSminer** : API TCP 4028, commandes ``summary``,
  ``stats`` et ``pools``.  Couvre Antminer et Whatsminer.  Format **non
  vérifié** (aucune sortie réelle fournie) : parseur tolérant, dernier recours.
* **IceRiver** (KS0 Ultra, Kaspa) : ``GET /api/overview`` du firmware, tel que
  documenté par l'apidoc ``iceriver-oc`` (``/api/overview`` -> ``data.model``,
  ``rtpow``/``avgpow`` + ``unit``, ``pools[]``, ``boards[]``).  Format **non
  vérifié** sur la machine de Fred.

Aucun pilotage : ces fonctions ne lisent que des états.
"""

from __future__ import annotations

import re
from typing import Any

#: Seuils propres aux ASIC, séparés de ceux des GPU/CPU.
ASIC_DEFAULTS: dict[str, float] = {
    "asic_hashrate_min_pct": 70.0,
    "asic_temp_max_c": 75.0,
}

ASIC_SETTING_KEYS: dict[str, str] = {
    "health_asic_hashrate_min_pct": "asic_hashrate_min_pct",
    "health_asic_temp_max_c": "asic_temp_max_c",
}

ASIC_BOUNDS: dict[str, tuple[float, float]] = {
    "asic_hashrate_min_pct": (1.0, 100.0),
    "asic_temp_max_c": (30.0, 120.0),
}

DRIVERS = ("axeos", "cgminer", "iceriver")

#: Algorithme par famille — sert au regroupement par coin du tableau de bord.
DRIVER_COIN: dict[str, dict[str, str]] = {
    "axeos": {"coin": "BTC", "algo": "SHA256d"},
    "cgminer": {"coin": "BTC", "algo": "SHA256d"},
    "iceriver": {"coin": "KAS", "algo": "kHeavyHash"},
}

_RATE_UNITS = {"h": 1.0, "kh": 1e3, "mh": 1e6, "gh": 1e9, "th": 1e12, "ph": 1e15}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u202f", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def parse_short_rate(text: str) -> float | None:
    """``"150G"``, ``"37.68G"``, ``"1.18T"`` -> H/s (IceRiver)."""
    match = re.search(r"([0-9][0-9.,]*)\s*([kKmMgGtTpP]?)", str(text or ""))
    if not match:
        return None
    number = _num(match.group(1))
    if number is None:
        return None
    unit = (match.group(2) or "").lower()
    return number * _RATE_UNITS.get(f"{unit}h", 1.0)


def parse_axeos(payload: dict[str, Any]) -> dict[str, Any]:
    """État normalisé d'un Bitaxe/NerdQaxe depuis ``/api/system/info``."""
    hs_gh = _num(payload.get("hashRate"))
    hs_1h_gh = _num(payload.get("hashRate_1h"))
    expected_gh = _num(payload.get("expectedHashrate"))
    domains: list[float] = []
    for asic in ((payload.get("hashrateMonitor") or {}).get("asics") or []):
        for domain in asic.get("domains") or []:
            value = _num(domain)
            if value is not None:
                domains.append(value)
    inactive = [value for value in domains if value <= 0]
    # Un domaine peut rendre ~1,7 GH/s là où les autres font ~430 GH/s : AxeOS
    # affiche « 0 » et Fred le voit comme inactif.  En dessous de 5 % du
    # domaine le plus fort, il ne compte pas.
    strongest = max(domains) if domains else 0.0
    if strongest > 0:
        inactive = [value for value in domains if value <= strongest * 0.05]
    hashrate_hs = None if hs_gh is None else hs_gh * 1e9
    expected_hs = None if expected_gh is None else expected_gh * 1e9
    reference = (hs_1h_gh if hs_1h_gh else hs_gh) or 0.0
    nominal_pct = None
    if expected_gh and reference:
        nominal_pct = round(100.0 * float(reference) / float(expected_gh), 1)
    return {
        "driver": "axeos",
        "online": True,
        "model": f"Bitaxe {payload.get('ASICModel') or ''}".strip(),
        "board_version": str(payload.get("boardVersion") or ""),
        "asic_model": str(payload.get("ASICModel") or ""),
        "firmware": str(payload.get("axeOSVersion") or payload.get("version") or ""),
        "hashrate_hs": hashrate_hs,
        "hashrate_1h_hs": None if hs_1h_gh is None else hs_1h_gh * 1e9,
        "expected_hashrate_hs": expected_hs,
        "nominal_pct": nominal_pct,
        "power_w": _num(payload.get("power")),
        "temp_c": _num(payload.get("temp")),
        "vr_temp_c": _num(payload.get("vrTemp")),
        "fan_rpm": _num(payload.get("fanrpm")),
        "fan_pct": _num(payload.get("fanspeed")),
        "pool": str(payload.get("stratumURL") or ""),
        "pool_port": payload.get("stratumPort"),
        "pool_user_masked": _mask_user(payload.get("stratumUser")),
        "difficulty": _num(payload.get("poolDifficulty")),
        "shares_accepted": _num(payload.get("sharesAccepted")),
        "shares_rejected": _num(payload.get("sharesRejected")),
        "best_diff": _num(payload.get("bestDiff")),
        "best_session_diff": _num(payload.get("bestSessionDiff")),
        "uptime_s": _num(payload.get("uptimeSeconds")),
        "domains_total": len(domains),
        "domains_inactive": len(inactive),
        "algorithm": "SHA256d",
        "coin": "BTC",
        "read_only": True,
    }


def _mask_user(value: Any) -> str:
    """Adresse de pool masquée : jamais le wallet complet dans l'API."""
    text = str(value or "")
    if len(text) <= 12:
        return text
    return f"{text[:6]}…{text[-4:]}"


def parse_cgminer(
    summary: dict[str, Any] | None,
    stats: dict[str, Any] | None = None,
    pools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """État normalisé d'un Antminer/Whatsminer depuis l'API TCP 4028.

    **Format non vérifié** (aucune sortie réelle) : les clés varient selon le
    firmware (``GHS av``, ``MHS av``, ``HS av``…), le parseur essaie les trois.
    """
    rows = (summary or {}).get("SUMMARY") or []
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    hashrate_hs = None
    for key in ("GHS av", "GHS 5s", "MHS av", "MHS 5s", "THS av", "HS av", "HS 5s"):
        value = _num(row.get(key))
        if value:
            unit = key.split()[0][0].lower()
            hashrate_hs = value * _RATE_UNITS.get(f"{unit}h", 1.0)
            break
    stat_rows = (stats or {}).get("STATS") or []
    stat = stat_rows[0] if stat_rows and isinstance(stat_rows[0], dict) else {}
    pool_rows = [p for p in ((pools or {}).get("POOLS") or []) if isinstance(p, dict)]
    active = next((p for p in pool_rows if _num(p.get("Stratum Active"))), pool_rows[0] if pool_rows else {})
    temp = _num(stat.get("Temperature1")) or _num(row.get("Temperature"))
    fans = [value for value in (_num(stat.get(f"fan{i}")) for i in range(1, 5)) if value]
    return {
        "driver": "cgminer",
        "online": True,
        "model": str(stat.get("Type") or stat.get("Model") or ""),
        "firmware": str(stat.get("Miner") or ""),
        "board_version": "",
        "asic_model": "",
        "hashrate_hs": hashrate_hs,
        "hashrate_1h_hs": hashrate_hs,
        "expected_hashrate_hs": None,
        "nominal_pct": None,
        "power_w": _num(stat.get("Power")) or _num(row.get("Power")),
        "temp_c": temp,
        "vr_temp_c": _num(stat.get("Temperature2")),
        "fan_rpm": fans[0] if fans else None,
        "fan_pct": None,
        "pool": str(active.get("URL") or ""),
        "pool_port": None,
        "pool_user_masked": _mask_user(active.get("User")),
        "difficulty": None,
        "shares_accepted": _num(row.get("Accepted")),
        "shares_rejected": _num(row.get("Rejected")),
        "best_diff": None,
        "best_session_diff": None,
        "uptime_s": _num(row.get("Elapsed")),
        "domains_total": 0,
        "domains_inactive": 0,
        "algorithm": "SHA256d",
        "coin": "BTC",
        "read_only": True,
        "format_verified": False,
    }


def parse_iceriver(payload: dict[str, Any]) -> dict[str, Any]:
    """État normalisé d'un IceRiver depuis ``GET /api/overview``.

    Documenté par l'apidoc ``iceriver-oc`` (KS0) : ``data.rtpow``/``avgpow``
    avec ``data.unit`` (``G`` = GH/s), ``pools[]`` (``accepted``/``rejected``,
    ``addr``), ``boards[]`` (``chiptmp``/``intmp``), ``fans[]``.  **Format non
    vérifié** sur la machine de Fred (IP inconnue à ce jour).
    """
    data = (payload or {}).get("data") if isinstance((payload or {}).get("data"), dict) else payload or {}
    unit = str(data.get("unit") or "G").strip() or "G"
    hashrate = parse_short_rate(f"{data.get('rtpow') or data.get('avgpow') or ''}")
    if hashrate is None:
        hashrate = parse_short_rate(f"{data.get('avgpow') or ''}")
    if hashrate is not None and unit.upper() != "G":
        hashrate = hashrate * _RATE_UNITS.get(f"{unit.lower()}h", 1.0) / 1e9
    pools = [p for p in (data.get("pools") or []) if isinstance(p, dict)]
    active = next((p for p in pools if _num(p.get("connect")) or _num(p.get("state"))), pools[0] if pools else {})
    boards = [b for b in (data.get("boards") or []) if isinstance(b, dict)]
    temps = [value for value in (_num(b.get("chiptmp")) for b in boards) if value]
    fans = [value for value in (_num(v) for v in (data.get("fans") or [])) if value]
    accepted = sum(_num(p.get("accepted")) or 0.0 for p in pools) or None
    rejected = sum(_num(p.get("rejected")) or 0.0 for p in pools) or None
    return {
        "driver": "iceriver",
        "online": True,
        "model": str(data.get("model") or ""),
        "firmware": str(data.get("softver1") or data.get("firmver1") or ""),
        "board_version": "",
        "asic_model": "",
        "hashrate_hs": hashrate,
        "hashrate_1h_hs": hashrate,
        "expected_hashrate_hs": None,
        "nominal_pct": None,
        "power_w": None,
        "temp_c": max(temps) if temps else None,
        "vr_temp_c": _num(boards[0].get("intmp")) if boards else None,
        "fan_rpm": max(fans) if fans else None,
        "fan_pct": None,
        "pool": str(active.get("addr") or ""),
        "pool_port": None,
        "pool_user_masked": _mask_user(active.get("user")),
        "difficulty": str(active.get("diff") or ""),
        "shares_accepted": accepted,
        "shares_rejected": rejected,
        "best_diff": None,
        "best_session_diff": None,
        "uptime_s": None,
        "domains_total": 0,
        "domains_inactive": 0,
        "algorithm": "kHeavyHash",
        "coin": "KAS",
        "read_only": True,
        "format_verified": False,
    }


def driver_from_payload(payload: dict[str, Any]) -> str:
    """Reconnaît le pilote d'une réponse JSON déjà lue."""
    if not isinstance(payload, dict):
        return ""
    if "hashRate" in payload and "expectedHashrate" in payload:
        return "axeos"
    if "SUMMARY" in payload or "STATS" in payload:
        return "cgminer"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if "rtpow" in data or "avgpow" in data or (data.get("model") and "pools" in data):
        return "iceriver"
    return ""


def state_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """État normalisé depuis une réponse brute, quel que soit le pilote."""
    driver = driver_from_payload(payload)
    if driver == "axeos":
        return parse_axeos(payload)
    if driver == "cgminer":
        return parse_cgminer(payload, payload, payload)
    if driver == "iceriver":
        return parse_iceriver(payload)
    return None


def limits(values: dict[str, Any] | None = None) -> dict[str, float]:
    """Seuils ASIC complets et bornés."""
    out = dict(ASIC_DEFAULTS)
    for name, value in (values or {}).items():
        if name not in out:
            continue
        number = _num(value)
        if number is None:
            continue
        low, high = ASIC_BOUNDS[name]
        out[name] = round(min(high, max(low, number)), 2)
    return out


def health(state: dict[str, Any] | None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pastille d'un ASIC : ``green`` / ``orange`` / ``red`` + raison.

    Un domaine de hachage à zéro passe **avant** le seuil de hashrate : c'est
    le défaut constaté sur les trois Bitaxe de Fred (~75 % du nominal avec un
    domaine inactif sur quatre).
    """
    limits_ = limits(settings)
    if not state:
        return {"status": "orange", "reason": "ASIC non interrogé"}
    if not state.get("online"):
        return {"status": "red", "reason": "hors ligne", "detail": state.get("error") or ""}
    total = int(state.get("domains_total") or 0)
    inactive = int(state.get("domains_inactive") or 0)
    pct = state.get("nominal_pct")
    if inactive > 0 and total > 0:
        suffix = f", {pct:.0f} % du nominal" if isinstance(pct, (int, float)) else ""
        return {
            "status": "orange",
            "reason": f"domaine de hachage inactif ({inactive}/{total}){suffix}",
            "domains_inactive": inactive,
            "domains_total": total,
            "nominal_pct": pct,
        }
    if isinstance(pct, (int, float)) and pct < limits_["asic_hashrate_min_pct"]:
        return {
            "status": "orange",
            "reason": f"hashrate à {pct:.0f} % du nominal",
            "nominal_pct": pct,
        }
    temp = state.get("temp_c")
    if isinstance(temp, (int, float)) and temp > limits_["asic_temp_max_c"]:
        return {"status": "orange", "reason": f"température {temp:.0f} °C"}
    return {"status": "green", "reason": ""}
