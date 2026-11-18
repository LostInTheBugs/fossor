"""Règle « heures chères » (§4.15) — décisions pures, sans aucun appel sortant.

La règle se déclenche sur un **prix courant au-dessus d'un seuil** ou sur un
**net/jour négatif** d'une machine, et n'agit que sur ce que Fossor a lancé :

* un device occupé par un **mineur externe** est listé « hors règle » ;
* une machine dont un device est engagé par une **campagne en cours** est
  protégée, sauf si l'option ``protect_campaigns`` est explicitement décochée ;
* le power limit est borné par les limites **lues sur la carte** ; sans bornes
  connues, aucune action n'est proposée.

Tout est calculé ici : le module ne connaît ni HTTP ni SSH, ce qui rend chaque
décision testable et la simulation gratuite (l'exécuteur ne fait rien d'autre
que journaliser).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

RULE_KEY = "energy_rule"
MODES = ("stop", "reduce", "power_limit")
MODE_LABELS = {
    "stop": "couper le minage",
    "reduce": "réduire (moins de devices)",
    "power_limit": "power limit GPU",
}

DEFAULT_RULE: dict[str, Any] = {
    "enabled": False,
    "mode": "stop",
    "threshold_eur_kwh": 0.30,
    "trigger_negative_net": True,
    "exclude_device_uuids": [],
    "priority": [],
    "keep_devices": 1,
    "power_limit_w": 100.0,
    "protect_campaigns": True,
    "simulation": True,
}


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "vrai", "oui", "on", "yes")
    return bool(value)


def _uuids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and len(text) <= 64 and text not in out:
            out.append(text)
    return out[:200]


def normalize_rule(raw: Any) -> dict[str, Any]:
    """Valide une règle ; lève ``ValueError`` avec un message en français."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("règle : objet attendu")
    mode = str(raw.get("mode") or "stop").strip().lower()
    if mode not in MODES:
        raise ValueError("action inconnue : stop, reduce ou power_limit attendu")
    try:
        threshold = float(raw.get("threshold_eur_kwh", 0.30))
    except (TypeError, ValueError):
        raise ValueError("seuil : nombre attendu")
    if threshold < 0:
        raise ValueError("seuil : valeur négative refusée")
    try:
        keep = int(raw.get("keep_devices", 1))
    except (TypeError, ValueError):
        raise ValueError("devices conservés : nombre entier attendu")
    if not 1 <= keep <= 64:
        raise ValueError("devices conservés : entre 1 et 64")
    try:
        limit = float(raw.get("power_limit_w", 100.0))
    except (TypeError, ValueError):
        raise ValueError("power limit : nombre attendu")
    if not 0 < limit <= 1000:
        raise ValueError("power limit : entre 1 et 1000 W")

    return {
        "enabled": _bool(raw.get("enabled")),
        "mode": mode,
        "threshold_eur_kwh": threshold,
        "trigger_negative_net": _bool(raw.get("trigger_negative_net", True)),
        "exclude_device_uuids": _uuids(raw.get("exclude_device_uuids")),
        "priority": _uuids(raw.get("priority")),
        "keep_devices": keep,
        "power_limit_w": limit,
        "protect_campaigns": _bool(raw.get("protect_campaigns", True)),
        "simulation": _bool(raw.get("simulation", True)),
    }


def get_rule(db: Session) -> dict[str, Any]:
    from app.ai_config import get_setting

    raw = get_setting(db, RULE_KEY, None)
    if raw is None:
        return normalize_rule(DEFAULT_RULE)
    try:
        return normalize_rule(raw)
    except ValueError:
        return normalize_rule(DEFAULT_RULE)


def set_rule(db: Session, raw: Any) -> dict[str, Any]:
    from app.ai_config import set_setting

    rule = normalize_rule(raw)
    set_setting(db, RULE_KEY, rule)
    db.commit()
    return rule


def trigger_reason(rule: dict[str, Any], price_eur_kwh: float, net_eur_day: float | None) -> str:
    """Raison du déclenchement, ou chaîne vide si la règle ne s'applique pas."""
    if not rule.get("enabled"):
        return ""
    reasons: list[str] = []
    threshold = float(rule.get("threshold_eur_kwh", 0.0))
    if price_eur_kwh > threshold:
        reasons.append(
            f"prix {price_eur_kwh:.4f} €/kWh au-dessus du seuil {threshold:.4f} €/kWh"
        )
    if rule.get("trigger_negative_net") and net_eur_day is not None and net_eur_day < 0:
        reasons.append(f"net/jour négatif ({net_eur_day:.4f} €)")
    return " ; ".join(reasons)


def clamp_power_limit(requested: float, minimum: Any, maximum: Any) -> tuple[float, bool]:
    """Borne un power limit par les limites de la carte ; refuse si inconnues."""
    if minimum is None or maximum is None:
        raise ValueError("bornes du power limit inconnues sur cette carte")
    low, high = float(minimum), float(maximum)
    if high <= low:
        raise ValueError("bornes du power limit incohérentes")
    value = float(requested)
    clamped = min(max(value, low), high)
    return round(clamped, 1), clamped != value


def power_limit_argv(index: int, watts: float) -> list[str]:
    """Commande `nvidia-smi -pl` pour un GPU, unités entières comme l'outil."""
    return ["nvidia-smi", "-i", str(int(index)), "-pl", str(int(round(watts)))]


def _ordered(devices: list[dict[str, Any]], rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Devices dans l'ordre de priorité de la règle, puis dans l'ordre reçu."""
    priority = list(rule.get("priority") or [])
    rank = {uuid: position for position, uuid in enumerate(priority)}
    return sorted(
        devices,
        key=lambda d: (rank.get(d.get("uuid") or "", len(rank)), devices.index(d)),
    )


def _why(rule: dict[str, Any], reason: str) -> str:
    return f"{reason} → {MODE_LABELS.get(rule.get('mode'), rule.get('mode'))}"


def decide(
    rule: dict[str, Any],
    *,
    price_eur_kwh: float,
    machines: list[dict[str, Any]],
) -> dict[str, Any]:
    """Décision de la règle pour un prix et un ensemble de machines.

    Chaque machine porte ``net_eur_day`` et ``devices`` : une machine dont le net
    est négatif est traitée même si le prix reste sous le seuil, et seuls ses
    devices sont concernés. Le déclenchement par le prix, lui, vise toutes les
    machines.
    """
    simulated = bool(rule.get("simulation", True))
    mode = str(rule.get("mode") or "stop")
    excluded = set(rule.get("exclude_device_uuids") or [])
    decision: dict[str, Any] = {
        "active": False,
        "reason": "",
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "simulated": simulated,
        "threshold_eur_kwh": float(rule.get("threshold_eur_kwh", 0.0)),
        "price_eur_kwh": round(float(price_eur_kwh), 6),
        "machines": [],
        "counts": {"stop": 0, "keep": 0, "power_limit": 0, "untouched": 0},
    }
    if not rule.get("enabled"):
        decision["reason"] = "règle désactivée"
        return decision

    global_reason = trigger_reason(rule, price_eur_kwh, None)
    any_active = False

    for machine in machines:
        net = machine.get("net_eur_day")
        reasons = [global_reason] if global_reason else []
        if rule.get("trigger_negative_net") and net is not None and float(net) < 0:
            text = f"net/jour négatif ({float(net):.4f} €)"
            if text not in reasons:
                reasons.append(text)
        reason = " ; ".join(reasons)
        entry: dict[str, Any] = {
            "machine_id": machine.get("machine_id"),
            "name": machine.get("name") or "",
            "net_eur_day": net,
            "active": bool(reason),
            "reason": reason,
            "actions": [],
            "untouched": [],
        }
        if not reason:
            decision["machines"].append(entry)
            continue

        any_active = True
        eligible: list[dict[str, Any]] = []
        for device in machine.get("devices") or []:
            uuid = device.get("uuid") or ""
            if device.get("external"):
                entry["untouched"].append({
                    "uuid": uuid,
                    "why": "mineur externe — hors règle (Fossor ne l'a pas lancé)",
                })
                continue
            if device.get("campaign") and rule.get("protect_campaigns", True):
                entry["untouched"].append({
                    "uuid": uuid,
                    "why": "campagne en cours protégée (option « interrompre les campagnes » décochée)",
                })
                continue
            eligible.append(device)

        if mode == "stop":
            for device in eligible:
                entry["actions"].append({
                    "uuid": device.get("uuid"), "action": "stop", "why": _why(rule, reason),
                })

        elif mode == "reduce":
            ordered = _ordered(eligible, rule)
            kept = 0
            keep_target = int(rule.get("keep_devices", 1))
            for device in ordered:
                uuid = device.get("uuid") or ""
                if uuid in excluded:
                    entry["actions"].append({
                        "uuid": uuid, "action": "stop",
                        "why": "device exclu par la règle",
                    })
                    continue
                if kept < keep_target:
                    kept += 1
                    entry["actions"].append({
                        "uuid": uuid, "action": "keep",
                        "why": f"conservé (priorité {kept}/{keep_target})",
                    })
                else:
                    entry["actions"].append({
                        "uuid": uuid, "action": "stop",
                        "why": _why(rule, reason),
                    })

        else:  # power_limit
            requested = float(rule.get("power_limit_w", 100.0))
            for device in eligible:
                if str(device.get("kind") or "gpu") != "gpu":
                    entry["untouched"].append({
                        "uuid": device.get("uuid"),
                        "why": "power limit GPU seulement",
                    })
                    continue
                try:
                    watts, clamped = clamp_power_limit(
                        requested, device.get("power_limit_min_w"), device.get("power_limit_max_w")
                    )
                except ValueError as exc:
                    entry["untouched"].append({"uuid": device.get("uuid"), "why": str(exc)})
                    continue
                entry["actions"].append({
                    "uuid": device.get("uuid"),
                    "index": device.get("index"),
                    "action": "power_limit",
                    "watts": watts,
                    "original_w": device.get("power_limit_default_w"),
                    "clamped": clamped,
                    "why": (
                        f"power limit {watts:.0f} W"
                        + (" (borné par la carte)" if clamped else "")
                        + f" — {reason}"
                    ),
                })

        for action in entry["actions"]:
            decision["counts"][action["action"]] = decision["counts"].get(action["action"], 0) + 1
        decision["counts"]["untouched"] += len(entry["untouched"])
        decision["machines"].append(entry)

    decision["active"] = any_active
    if any_active:
        decision["reason"] = global_reason or next(
            (m["reason"] for m in decision["machines"] if m["reason"]), ""
        )
    elif not decision["reason"]:
        decision["reason"] = "prix sous le seuil et net positif"
    return decision


def restore_actions(decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Actions de retour arrière (power limit d'origine) d'une décision exécutée."""
    out: list[dict[str, Any]] = []
    for machine in decision.get("machines") or []:
        for action in machine.get("actions") or []:
            if action.get("action") == "power_limit" and action.get("original_w") is not None:
                out.append({
                    "machine_id": machine.get("machine_id"),
                    "uuid": action.get("uuid"),
                    "index": action.get("index"),
                    "watts": action.get("original_w"),
                    "action": "restore_power_limit",
                })
    return out
