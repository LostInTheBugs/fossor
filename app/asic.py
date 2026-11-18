"""ASIC en lecture seule (§4.20) — parseurs purs, sans réseau ni base.

Trois familles de pilotes :

* **AxeOS** (Bitaxe / NerdQaxe) : ``GET /api/system/info``.  Hashrate en GH/s,
  nominal dans ``expectedHashrate``, domaines de hachage dans
  ``hashrateMonitor.asics[].domains``.  Format **vérifié** sur une fixture
  réelle (AxeOS v2.12.2, Gamma 602).
* **cgminer / bmminer / BOSminer** : API TCP 4028, commandes ``summary``,
  ``stats`` et ``pools``.  Couvre Antminer et Whatsminer.  Format **non
  vérifié** (aucune sortie réelle fournie) : parseur tolérant, dernier recours.
* **IceRiver** (KS0 Ultra, Kaspa) : ``POST /user/userpanel`` avec ``post=4``,
  l'appel que fait l'écran « Miner » du firmware ICM168.  Format **vérifié**
  sur la réponse réelle d'un KS0 Ultra (``ICM168_3_2_14_ks0ultra``) : ``data``
  porte ``rtpow``/``avgpow`` + ``unit``, ``runtime`` ``j:h:m:s``, ``fans``,
  ``fanstate``, ``pools[]`` et ``boards[]``.  **Aucune authentification** et
  aucun identifiant de pool n'est lu, stocké ni affiché.

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

#: Nominal par modèle IceRiver (H/s), dans l'ordre de reconnaissance : le
#: modèle se lit dans ``data.softver1`` (``…_ks0ultra_miner``).  « ks0pro »
#: contient « ks0 », donc l'ultra puis le pro passent avant le ks0 nu.
ICERIVER_NOMINALS: dict[str, tuple[str, float]] = {
    "ks0ultra": ("KS0 Ultra", 400e9),
    "ks0pro": ("KS0 Pro", 200e9),
    "ks0": ("KS0", 100e9),
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


def parse_rate(value: Any, unit: str = "") -> float | None:
    """Débit IceRiver -> H/s, en retombant sur ``unit`` si la valeur est nue.

    Le firmware écrit ``"271G"`` avec ``unit="G"`` ; certains modèles
    n'écrivent que ``"271"`` : c'est alors ``unit`` qui porte l'échelle.
    """
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"([0-9][0-9.,]*)\s*([kKmMgGtTpP]?)", text)
    if not match:
        return None
    number = _num(match.group(1))
    if number is None:
        return None
    letter = (match.group(2) or unit or "").strip()[:1].lower()
    return number * _RATE_UNITS.get(f"{letter}h", 1.0)


def iceriver_model(softver: Any) -> tuple[str, float | None]:
    """``softver1`` -> (modèle lisible, nominal en H/s)."""
    text = re.sub(r"[^a-z0-9]", "", str(softver or "").lower())
    for key, (label, nominal) in ICERIVER_NOMINALS.items():
        if key in text:
            return label, nominal
    return "", None


def parse_runtime(text: Any) -> int | None:
    """``"00:05:20:11"`` (jours:heures:minutes:secondes) -> secondes."""
    parts = str(text or "").strip().split(":")
    if len(parts) != 4:
        return None
    try:
        days, hours, minutes, seconds = (int(float(part)) for part in parts)
    except ValueError:
        return None
    if min(days, hours, minutes, seconds) < 0:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


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
    """État normalisé d'un IceRiver depuis ``POST /user/userpanel`` (``post=4``).

    Format **vérifié** sur la réponse réelle d'un KS0 Ultra
    (``ICM168_3_2_14_ks0ultra``) : ``data.rtpow``/``avgpow`` + ``unit``,
    ``runtime`` ``j:h:m:s``, ``reject`` en %, ``fans[]``/``fanstate``,
    ``pools[]`` (``connect``, ``accepted``, ``rejected``, ``state``) et
    ``boards[]`` (``freq``, ``rtpow``, ``avgpow``, ``intmp``, ``outtmp``,
    ``chipnum``).  Le modèle se lit dans ``softver1``.

    ``pools[].user`` et ``pools[].pass`` ne sont **jamais** lus : ni stockés,
    ni affichés, ni reversés dans l'état normalisé.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload or {}
    unit = str(data.get("unit") or "").strip() or "G"
    rt_hs = parse_rate(data.get("rtpow"), unit)
    avg_hs = parse_rate(data.get("avgpow"), unit)
    softver = str(data.get("softver1") or "")
    model, nominal_hs = iceriver_model(softver)
    reference = avg_hs if avg_hs else rt_hs
    nominal_pct = None
    if nominal_hs and reference:
        nominal_pct = round(100.0 * reference / nominal_hs, 1)

    pools = [p for p in (data.get("pools") or []) if isinstance(p, dict)]
    connected = [p for p in pools if _num(p.get("connect")) == 1]
    active = connected[0] if connected else (pools[0] if pools else {})
    pool_host = str(active.get("addr") or "")

    boards = [b for b in (data.get("boards") or []) if isinstance(b, dict)]
    board_rows = []
    for board in boards:
        board_rows.append({
            "no": _num(board.get("no")),
            "freq_mhz": _num(board.get("freq")),
            "rtpow_hs": parse_rate(board.get("rtpow"), unit),
            "avgpow_hs": parse_rate(board.get("avgpow"), unit),
            "intmp_c": _num(board.get("intmp")),
            "outtmp_c": _num(board.get("outtmp")),
            "chipnum": _num(board.get("chipnum")),
        })
    temps = [v for v in (_num(b.get("intmp")) for b in boards) if v is not None]
    out_temps = [v for v in (_num(b.get("outtmp")) for b in boards) if v is not None]
    freqs = [v for v in (_num(b.get("freq")) for b in boards) if v]
    chips = [v for v in (_num(b.get("chipnum")) for b in boards) if v]

    fans = [v for v in (_num(v) for v in (data.get("fans") or [])) if v is not None]
    fan_state = data.get("fanstate")
    fans_zero = bool(fans) and not any(v > 0 for v in fans)
    if fan_state is False:
        fans_zero = True

    accepted = sum(_num(p.get("accepted")) or 0.0 for p in pools) or None
    rejected = sum(_num(p.get("rejected")) or 0.0 for p in pools) or None
    reject_pct = _num(data.get("reject"))
    if reject_pct is None and accepted and rejected:
        reject_pct = round(100.0 * rejected / (accepted + rejected), 3)

    text = f"{pool_host} {softver}".lower()
    if "kas" in text or model:
        coin, algorithm = "KAS", "kHeavyHash"
    else:
        coin, algorithm = "", ""

    return {
        "driver": "iceriver",
        "online": bool(data.get("online", True)),
        "model": model or softver,
        "firmware": softver,
        "board_version": "",
        "asic_model": model,
        "hashrate_hs": rt_hs if rt_hs is not None else avg_hs,
        "hashrate_1h_hs": avg_hs,
        "expected_hashrate_hs": nominal_hs,
        "nominal_hs": nominal_hs,
        "nominal_pct": nominal_pct,
        "power_w": None,
        "temp_c": max(temps + out_temps) if (temps or out_temps) else None,
        "vr_temp_c": None,
        "intemp_c": max(temps) if temps else None,
        "outtemp_c": max(out_temps) if out_temps else None,
        "fan_rpm": max(fans) if fans else None,
        "fan_pct": None,
        "fan_state": fan_state,
        "fans": fans,
        "fans_zero": fans_zero,
        "freq_mhz": max(freqs) if freqs else None,
        "chipnum": sum(chips) if chips else None,
        "boards": board_rows,
        "pool": pool_host,
        "pool_port": None,
        "pool_connected": bool(connected) if pools else None,
        "pool_priority": _num(active.get("priority")),
        "difficulty": str(active.get("diff") or ""),
        "shares_accepted": accepted,
        "shares_rejected": rejected,
        "reject_pct": reject_pct,
        "best_diff": None,
        "best_session_diff": None,
        "uptime_s": parse_runtime(data.get("runtime")),
        "runtime": str(data.get("runtime") or ""),
        "powstate": data.get("powstate"),
        "netstate": data.get("netstate"),
        "tempstate": data.get("tempstate"),
        "domains_total": 0,
        "domains_inactive": 0,
        "algorithm": algorithm,
        "coin": coin,
        "read_only": True,
        "format_verified": True,
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
    if "rtpow" in data or "avgpow" in data:
        return "iceriver"
    softver = str(data.get("softver1") or "").lower()
    if softver and "pools" in data and ("ks0" in softver or "iceriver" in softver):
        return "iceriver"
    if data.get("model") and "pools" in data and "boards" in data:
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

    L'ordre est celui du danger : ventilateurs arrêtés, pool déconnecté, puis
    hashrate sous le nominal.  Un domaine de hachage à zéro passe **avant** le
    seuil de hashrate : c'est le défaut constaté sur les trois Bitaxe de Fred
    (~75 % du nominal avec un domaine inactif sur quatre).
    """
    limits_ = limits(settings)
    if not state:
        return {"status": "orange", "reason": "ASIC non interrogé"}
    if not state.get("online"):
        return {"status": "red", "reason": "hors ligne", "detail": state.get("error") or ""}
    if state.get("requires_login"):
        return {
            "status": "orange",
            "reason": "identifiants requis pour lire le panneau",
            "detail": state.get("error") or "",
        }
    if state.get("fans_zero"):
        return {
            "status": "orange",
            "reason": "ventilateurs à 0 tr/min",
            "fan_rpm": state.get("fan_rpm"),
            "nominal_pct": state.get("nominal_pct"),
        }
    if state.get("pool_connected") is False:
        return {
            "status": "orange",
            "reason": "pool déconnecté",
            "nominal_pct": state.get("nominal_pct"),
        }
    reject = state.get("reject_pct")
    if isinstance(reject, (int, float)) and reject > 5.0:
        return {"status": "orange", "reason": f"rejets {reject:.1f} % du travail"}
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
