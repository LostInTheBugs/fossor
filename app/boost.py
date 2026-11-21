"""Mode boost (§10.3) — calculs **purs** : pool, portée, refus HiveOS, écran.

Aucune base, aucun SSH, aucune horloge implicite : la durée et l'échéance sont
calculées à partir d'un instant passé.  Le planificateur
(:mod:`app.boost_runner`) applique ces règles ; le routeur les expose.

Règles de Fred (§4.1 ter / §4.19) :

* le pool de boost est l'ensemble des devices marqués ``boost_pool`` — par
  défaut tous les GPU du rig HiveOS, et les GPU de l'athlon / du Ryzen
  seulement s'ils sont cochés ;
* le CPU du Ryzen est **toujours** exclu (BigCoin solo externe) ;
* un rig HiveOS se prend **en entier** : une sélection partielle est refusée.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

STATUSES = ("proposed", "approved", "running", "stopping", "done", "failed", "cancelled")
TERMINAL = frozenset({"done", "failed", "cancelled"})
SCOPES = ("pool", "proportion", "machines")

ALLOWED: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "cancelled"}),
    "approved": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"stopping", "failed", "cancelled"}),
    "stopping": frozenset({"done", "failed", "cancelled"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

DEFAULT_HOURS = 6.0
MIN_HOURS = 1.0
MAX_HOURS = 72.0
DEFAULT_MAX_SHARE_PCT = 100
MAX_PSU_PCT = 80.0

#: Coins minés en solo sur un device qui n'appartient à personne d'autre :
#: le CPU du Ryzen (BigCoin) ne doit **jamais** entrer dans un boost.
SOLO_EXCLUDED_COINS = frozenset({"BIGCOIN"})

STATUS_LABELS = {
    "proposed": "proposé",
    "approved": "approuvé",
    "running": "en cours",
    "stopping": "arrêt en cours",
    "done": "terminé",
    "failed": "en échec",
    "cancelled": "annulé",
}


class BoostError(ValueError):
    """Boost invalide — le message est sûr à afficher à l'opérateur."""


def check_status(status: str) -> str:
    if status not in STATUSES:
        raise BoostError(f"statut de boost inconnu : {status!r}")
    return status


def can_transition(source: str, target: str) -> bool:
    try:
        return target in ALLOWED[check_status(source)]
    except BoostError:
        return False


def ensure_transition(source: str, target: str) -> None:
    check_status(source)
    check_status(target)
    if target not in ALLOWED[source]:
        allowed = ", ".join(sorted(ALLOWED[source])) or "aucune (statut final)"
        raise BoostError(f"Transition refusée : {source} → {target} (autorisées : {allowed})")


def apply_transition(boost: Any, target: str) -> str:
    ensure_transition(getattr(boost, "status", ""), target)
    boost.status = target
    return target


# ── Durée bornée ──────────────────────────────────────────────────────
def validate_duration(hours: Any) -> float:
    """Durée du boost : 1 à 72 h, 6 h par défaut."""
    if hours in (None, ""):
        return DEFAULT_HOURS
    try:
        value = float(hours)
    except (TypeError, ValueError):
        raise BoostError("durée invalide : nombre d'heures attendu") from None
    if value < MIN_HOURS or value > MAX_HOURS:
        raise BoostError(
            f"durée hors bornes : {value:g} h (entre {MIN_HOURS:g} h et {MAX_HOURS:g} h)"
        )
    return round(value, 2)


def deadline(start: datetime, hours: float) -> datetime:
    return start + timedelta(hours=validate_duration(hours))


def is_due(boost: Any, moment: datetime) -> bool:
    """Fin d'échéance ou annulation demandée."""
    if getattr(boost, "stop_requested", False):
        return True
    ends = parse_iso(getattr(boost, "ends_at", None))
    return ends is not None and ends <= moment


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ── Pool de boost ─────────────────────────────────────────────────────
def exclusion_reason(device: Any, default_coin: str | None = None) -> str | None:
    """Pourquoi un device ne peut **jamais** entrer dans un boost."""
    kind = str(getattr(device, "kind", "") or "")
    index = getattr(device, "index", None)
    if kind not in ("cpu", "gpu"):
        return "type de device inconnu"
    if kind == "gpu" and index is None:
        return "GPU sans index"
    if not getattr(device, "present", True):
        return "device absent"
    if not getattr(device, "enabled_for_mining", False):
        return "désactivé pour le minage"
    if getattr(device, "integrated", False):
        return "GPU intégré (masqué)"
    if getattr(device, "occupied_external", False):
        return "occupé par un mineur externe"
    coin = str(default_coin or "").upper()
    if coin in SOLO_EXCLUDED_COINS:
        return f"token par défaut {coin} en solo : device exclu du boost"
    if kind == "cpu" and not getattr(device, "boost_pool", False):
        return "CPU non inscrit au pool de boost"
    if not getattr(device, "boost_pool", False):
        return "hors du pool de boost"
    if (getattr(device, "role", None) or "default_and_spec") == "default_only":
        return "rôle « token par défaut uniquement »"
    return None


def pool_rows(devices: Iterable[Any], default_coins: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Devices réellement mobilisables, avec la raison d'exclusion des autres."""
    coins = default_coins or {}
    rows: list[dict[str, Any]] = []
    for device in devices:
        uuid = str(getattr(device, "uuid", "") or "")
        reason = exclusion_reason(device, coins.get(uuid))
        rows.append(
            {
                "uuid": uuid,
                "machine_id": int(getattr(device, "machine_id", 0) or 0),
                "kind": getattr(device, "kind", ""),
                "index": getattr(device, "index", None),
                "model": getattr(device, "model", "") or "",
                "boost_pool": bool(getattr(device, "boost_pool", False)),
                "in_pool": reason is None,
                "excluded_reason": reason or "",
            }
        )
    return rows


def proportion_count(total: int, pct: Any) -> int:
    """Nombre de devices pour une proportion ; au moins 1 quand le pool n'est pas vide."""
    if total <= 0:
        return 0
    try:
        value = float(pct)
    except (TypeError, ValueError):
        raise BoostError("proportion invalide : pourcentage attendu") from None
    if value <= 0 or value > 100:
        raise BoostError("proportion hors bornes : entre 1 et 100 %")
    return max(1, min(total, int(round(total * value / 100.0))))


def select_rows(
    pool: list[dict[str, Any]],
    *,
    scope: str = "pool",
    proportion: Any = 100,
    machine_ids: Iterable[int] | None = None,
    gpu_first: bool = True,
) -> list[dict[str, Any]]:
    """Sélection déterministe : GPU d'abord (index croissant), puis CPU."""
    if scope not in SCOPES:
        raise BoostError(f"portée inconnue : {scope!r} (pool, proportion ou machines)")
    eligible = [row for row in pool if row["in_pool"]]
    eligible.sort(key=lambda row: (0 if row["kind"] == "gpu" else 1, row["index"] if row["index"] is not None else 999))
    if scope == "machines":
        wanted = {int(m) for m in (machine_ids or [])}
        if not wanted:
            raise BoostError("sélection de machines vide")
        chosen = [row for row in eligible if int(row["machine_id"]) in wanted]
        if not chosen:
            raise BoostError("aucune machine sélectionnée ne figure dans le pool de boost")
        return chosen
    if scope == "proportion":
        count = proportion_count(len(eligible), proportion)
        if gpu_first:
            return eligible[:count]
        # Une proportion qui ne prend pas tous les GPU d'un rig HiveOS est
        # refusée plus haut (règle « tout ou rien ») : le découpage reste
        # néanmoins déterministe.
        return eligible[:count]
    return eligible


def hiveos_refusal(
    *,
    machine_name: str,
    hiveos: bool,
    gpu_total: int,
    selected_gpu_indices: Iterable[int],
) -> str | None:
    """Un rig HiveOS se prend en entier, sinon refus explicite (409)."""
    if not hiveos or gpu_total <= 0:
        return None
    engaged = len({int(i) for i in selected_gpu_indices})
    if engaged >= gpu_total:
        return None
    return (
        f"Rig HiveOS « {machine_name} » : {engaged} GPU sélectionné(s) sur {gpu_total}. "
        "Un rig HiveOS se prend en entier — sélection partielle refusée."
    )


# ── Écran de validation (§10.3) ───────────────────────────────────────
def screen(
    *,
    rows: list[dict[str, Any]],
    hours: float,
    wall_power_w: float | None,
    eur_per_kwh: float | None,
    lost_hashrate_hs: float | None,
    boost_price_eur: float | None,
    boost_network_hashrate: float | None,
    max_share_pct: float = DEFAULT_MAX_SHARE_PCT,
) -> dict[str, Any]:
    """Ce que Fred voit **avant** de valider : retirés, perdus, coût, revenu."""
    duration = validate_duration(hours)
    removed = [
        {
            "machine_id": row["machine_id"],
            "uuid": row["uuid"],
            "label": row.get("label") or f"{row['kind']} {row.get('index') or ''}".strip(),
        }
        for row in rows
    ]
    cost = None
    if wall_power_w is not None and eur_per_kwh is not None:
        cost = round((float(wall_power_w) / 1000.0) * duration * float(eur_per_kwh), 4)
    revenue = None
    if (
        boost_price_eur is not None
        and boost_network_hashrate
        and lost_hashrate_hs
        and float(boost_network_hashrate) > 0
    ):
        # Estimation **grossière** : part du réseau × émission supposée nulle
        # sans données de récompense ; on n'invente rien.
        revenue = None
    return {
        "duration_h": duration,
        "ends_at": None,
        "devices": removed,
        "device_count": len(removed),
        "hashrate_lost_hs": lost_hashrate_hs,
        "wall_power_w": wall_power_w,
        "cost_eur": cost,
        "revenue_eur": revenue,
        "revenue_known": revenue is not None,
        "max_share_pct": float(max_share_pct),
        "share_pct": round(100.0 * len(removed) / max(1, len(rows) + 0), 1) if rows else 0.0,
        "warnings": [],
        "blocking": [],
    }


def psu_warning(worst_pct: float | None, confirmed: bool) -> dict[str, str] | None:
    """§4.2 bis : au-delà de 80 %, confirmation explicite."""
    if worst_pct is None or float(worst_pct) <= MAX_PSU_PCT:
        return None
    message = (
        f"Charge prévue de {round(float(worst_pct), 1)} % sur l'alimentation "
        f"(limite {MAX_PSU_PCT:.0f} %)"
    )
    if confirmed:
        return {"code": "psu_overload_confirmed", "message": message + " — confirmée"}
    return {"code": "psu_overload", "message": message + " : confirmation explicite requise"}


def share_refusal(count: int, total: int, max_share_pct: Any) -> str | None:
    """Plafond de devices retirés du token par défaut (§10.3)."""
    if total <= 0:
        return None
    try:
        limit = float(max_share_pct)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_SHARE_PCT
    if limit >= 100:
        return None
    if 100.0 * count / total > limit + 1e-9:
        return (
            f"{count} device(s) sur {total} ({round(100.0 * count / total, 1)} %) dépassent "
            f"le plafond de {limit:g} % retiré du token par défaut"
        )
    return None


def summary_line(boost: Any) -> str:
    status = STATUS_LABELS.get(getattr(boost, "status", ""), getattr(boost, "status", "?"))
    return f"Boost #{getattr(boost, 'id', '?')} {getattr(boost, 'coin', '?')} — {status}"
