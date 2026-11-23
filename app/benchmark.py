"""Benchmark a la demande de la limite de puissance GPU (SPEC 10.4).

Aucune I/O ici : paliers bornes par `nvidia-smi`, choix des paliers
« performance » et « rendement », arret sur sante, estimation affichee avant
lancement, application apres redemarrage. Le pilote est
:mod:`app.benchmark_runner`, l'API :mod:`app.routers.benchmark`.

« Overclocking » = **limite de puissance seulement** : jamais de frequence
coeur ou memoire, jamais de tension. Trois modes : aucun (defaut),
performance maximale, rendement optimise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

MODES = ("none", "performance", "efficiency")
MODE_LABELS = {
    "none": "Aucun",
    "performance": "Performance maximale",
    "efficiency": "Rendement optimise",
}
STEP_PCTS = (100, 90, 80, 70, 60)
DEFAULT_MEASURE_MIN = 3
DEFAULT_WARMUP_S = 60
MIN_MEASURE_MIN = 1
MAX_MEASURE_MIN = 30
DEFAULT_TEMP_LIMIT_C = 80.0
DEFAULT_REJECT_LIMIT_PCT = 2.0
DEFAULT_HASHRATE_FLOOR = 0.60
#: Bornes de repli quand la machine est simulee (aucune lecture nvidia-smi).
SIM_BOUNDS = {"min": 100.0, "max": 250.0, "default": 180.0}
SIM_HASHRATE_PER_WATT = 55_000.0


def normalize_mode(mode: str | None) -> str:
    value = (mode or "none").strip().lower()
    if value not in MODES:
        raise ValueError("mode inconnu : aucun, performance ou rendement attendu")
    return value


def levels(minimum: Any, maximum: Any, default: Any,
           pcts: tuple[int, ...] = STEP_PCTS) -> list[dict[str, Any]]:
    """Paliers partant de la limite par defaut puis descendant.

    Chaque palier est un pourcentage de la limite **par defaut** de la carte,
    borne par le minimum et le maximum `nvidia-smi` : jamais en dessous du
    minimum, jamais au-dessus du maximum. Deux paliers qui tombent sur la meme
    valeur entiere sont fusionnes (le premier garde son pourcentage).
    """
    try:
        low, high, base = float(minimum), float(maximum), float(default)
    except (TypeError, ValueError):
        raise ValueError("bornes du power limit inconnues")
    if low <= 0 or high <= 0 or base <= 0:
        raise ValueError("bornes du power limit invalides")
    if high < low:
        raise ValueError("bornes du power limit incoherentes")
    base = min(max(base, low), high)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for pct in pcts:
        try:
            share = float(pct)
        except (TypeError, ValueError):
            continue
        if share <= 0 or share > 100:
            continue
        raw = base * share / 100.0
        watts = int(round(min(max(raw, low), high)))
        if watts in seen:
            continue
        seen.add(watts)
        out.append({"percent": int(share), "watts": watts, "clamped": watts != round(raw)})
    return out


def efficiency(hashrate_hs: Any, power_w: Any) -> float | None:
    """Rendement en H/s par watt, ou None si la mesure n'a pas de sens."""
    try:
        hashrate, power = float(hashrate_hs), float(power_w)
    except (TypeError, ValueError):
        return None
    if hashrate <= 0 or power <= 0:
        return None
    return round(hashrate / power, 2)


def measured(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in steps
            if step.get("result") == "ok" and (step.get("hashrate_hs") or 0) > 0]


def pick_bests(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Palier « performance » (hashrate max) et « rendement » (hashrate/W max)."""
    rows = measured(steps)
    empty = {"performance": None, "efficiency": None}
    if not rows:
        return empty
    best_h = max(rows, key=lambda s: (float(s.get("hashrate_hs") or 0),
                                      -int(s.get("watts") or 0)))
    rated = [s for s in rows if efficiency(s.get("hashrate_hs"), s.get("power_w")) is not None]
    best_e = max(rated, key=lambda s: (efficiency(s.get("hashrate_hs"), s.get("power_w")),
                                       float(s.get("hashrate_hs") or 0),
                                       -int(s.get("watts") or 0))) if rated else best_h

    def row(step: dict[str, Any]) -> dict[str, Any]:
        return {
            "watts": int(step.get("watts") or 0),
            "percent": step.get("percent"),
            "hashrate_hs": step.get("hashrate_hs"),
            "power_w": step.get("power_w"),
            "efficiency": efficiency(step.get("hashrate_hs"), step.get("power_w")),
        }

    return {"performance": row(best_h), "efficiency": row(best_e)}


def reject_pct(rejected: Any, accepted: Any) -> float:
    """Part de shares rejetees, en pourcentage (0 si aucun share)."""
    try:
        bad, good = int(rejected or 0), int(accepted or 0)
    except (TypeError, ValueError):
        return 0.0
    total = bad + good
    if total <= 0:
        return 0.0
    return round(100.0 * bad / total, 2)


def stop_reason(sample: dict[str, Any], *, first_hashrate: float | None = None,
                temp_limit_c: float = DEFAULT_TEMP_LIMIT_C,
                reject_limit_pct: float = DEFAULT_REJECT_LIMIT_PCT,
                hashrate_floor: float = DEFAULT_HASHRATE_FLOOR) -> str | None:
    """Raison d'arreter le palier : temperature, rejets, hashrate effondre."""
    temp = sample.get("temperature_c")
    if temp is not None:
        try:
            if float(temp) > float(temp_limit_c):
                return (f"temperature {float(temp):.0f} C au-dessus du seuil "
                        f"{float(temp_limit_c):.0f} C")
        except (TypeError, ValueError):
            pass
    rejects = sample.get("reject_pct")
    if rejects is not None:
        try:
            if float(rejects) > float(reject_limit_pct):
                return (f"rejets {float(rejects):.1f} % au-dessus du seuil "
                        f"{float(reject_limit_pct):.1f} %")
        except (TypeError, ValueError):
            pass
    hashrate = sample.get("hashrate_hs")
    if first_hashrate and hashrate is not None:
        try:
            if float(hashrate) < float(first_hashrate) * float(hashrate_floor):
                return (f"hashrate {float(hashrate):.0f} H/s sous "
                        f"{float(hashrate_floor) * 100:.0f} % du premier palier")
        except (TypeError, ValueError):
            pass
    return None


def estimate(*, level_count: int, warmup_s: int, measure_min: int) -> int:
    """Duree estimee du balayage, en secondes."""
    levels_n = max(1, int(level_count or 1))
    warm = max(0, int(warmup_s or 0))
    measure = max(1, int(measure_min or 1)) * 60
    return levels_n * (warm + measure)


def claimed_watts(mode: str | None, bests: dict[str, Any], *, minimum: Any,
                  maximum: Any, default: Any) -> int:
    """Limite a appliquer pour un mode ; « aucun » remet la limite par defaut."""
    value = normalize_mode(mode)
    if value == "none":
        target = float(default)
    else:
        chosen = (bests or {}).get(value) or {}
        if chosen.get("watts") is None:
            raise ValueError("aucun palier mesure pour ce mode")
        target = float(chosen["watts"])
    low, high = float(minimum), float(maximum)
    return int(round(min(max(target, low), high)))


def needs_reapply(applied_uptime_s: Any, uptime_s: Any) -> bool:
    """La limite a-t-elle ete perdue par un redemarrage de la machine ?

    `nvidia-smi -pl` ne survit pas a un reboot : une uptime actuelle plus
    faible que celle relevee a l'application prouve le redemarrage.
    """
    try:
        applied, current = int(applied_uptime_s), int(uptime_s)
    except (TypeError, ValueError):
        return False
    if applied <= 0 or current < 0:
        return False
    return current < applied


def hiveos_hint(index: Any, watts: Any) -> str:
    """Valeur a saisir dans l'OC HiveOS : HiveOS ecrase `nvidia-smi -pl`."""
    return (f"HiveOS : GPU {index} -> limite de puissance {int(round(float(watts)))} W "
            "dans l'OC de la flight sheet (Fossor ne l'applique pas sur ce rig).")


def parse_probe(text: str) -> dict[int, dict[str, float]]:
    """Lignes `index,power.draw,temperature.gpu,power.limit` du faux/ vrai nvidia-smi."""
    out: dict[int, dict[str, float]] = {}
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            index = int(parts[0])
            values = [float(part) for part in parts[1:4]]
        except ValueError:
            continue
        if index < 0:
            continue
        out[index] = {"power_w": values[0], "temperature_c": values[1], "limit_w": values[2]}
    return out


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def shape_curve(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Points de la courbe hashrate / puissance / rendement, par palier."""
    points: list[dict[str, Any]] = []
    for step in steps:
        if step.get("result") not in ("ok", "stopped"):
            continue
        points.append({
            "device_uuid": step.get("device_uuid"),
            "percent": step.get("percent"),
            "watts": step.get("watts"),
            "hashrate_hs": step.get("hashrate_hs"),
            "power_w": step.get("power_w"),
            "efficiency": efficiency(step.get("hashrate_hs"), step.get("power_w")),
        })
    return points
