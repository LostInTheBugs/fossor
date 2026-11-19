"""Campagnes de minage (§4.7) — machine à états, contrôles, gain et coût.

Tout est **pur** : aucune base, aucun SSH, aucune horloge implicite (la date
courante est toujours passée en argument).  Le planificateur
(:mod:`app.campaign_runner`) applique ces règles, le routeur les expose, les
tests les éprouvent sans infrastructure.

Cycle de vie (§4.7) ::

    proposed ──► approved ──► running ──► stopping ──► done | failed
        │            │            │            │
        └────────────┴────────────┴────────────┴──────► cancelled

Toute autre transition est refusée (:class:`CampaignError`) : une campagne ne
ressuscite pas, un `done` ne redevient pas `running`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

STATUSES = ("proposed", "approved", "running", "stopping", "done", "failed",
            "cancelled", "miner_stopped")
ORIGINS = ("manual", "hermes")
TERMINAL = frozenset({"done", "failed", "cancelled", "miner_stopped"})

#: Transitions autorisées, et elles seules.
ALLOWED: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "cancelled"}),
    "approved": frozenset({"running", "cancelled"}),
    "running": frozenset({"stopping", "failed", "cancelled", "miner_stopped"}),
    "stopping": frozenset({"done", "failed", "cancelled"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "miner_stopped": frozenset(),
}

#: §4.2 bis : au-delà, le démarrage exige une confirmation explicite.
MAX_PSU_PCT = 80.0
#: Nombre de nouvelles tentatives de retour au token par défaut (2 essais).
MAX_RESTORE_ATTEMPTS = 2
#: Délai par défaut pour que le hashrate du token par défaut revienne (§4.7).
DEFAULT_RESTORE_TIMEOUT_S = 300

STATUS_LABELS = {
    "proposed": "proposée",
    "approved": "approuvée",
    "running": "en cours",
    "stopping": "arrêt en cours",
    "done": "terminée",
    "failed": "en échec",
    "cancelled": "annulée",
    "miner_stopped": "mineur arrêté",
}


class CampaignError(ValueError):
    """Campagne invalide — le message est sûr à afficher à l'opérateur."""


# ── Machine à états ───────────────────────────────────────────────────
def check_status(status: str) -> str:
    if status not in STATUSES:
        raise CampaignError(f"statut inconnu : {status!r}")
    return status


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def next_statuses(status: str) -> frozenset[str]:
    check_status(status)
    return ALLOWED[status]


def can_transition(source: str, target: str) -> bool:
    try:
        return target in ALLOWED[check_status(source)]
    except CampaignError:
        return False


def ensure_transition(source: str, target: str) -> None:
    """Lève :class:`CampaignError` si la transition est interdite."""
    check_status(source)
    check_status(target)
    if target not in ALLOWED[source]:
        allowed = ", ".join(sorted(ALLOWED[source])) or "aucune (statut final)"
        raise CampaignError(
            f"Transition refusée : {source} → {target} (autorisées : {allowed})"
        )


def apply_transition(campaign: Any, target: str) -> str:
    """Applique la transition sur un objet portant ``status`` ; renvoie le statut."""
    ensure_transition(getattr(campaign, "status", ""), target)
    campaign.status = target
    return target


# ── Dates ─────────────────────────────────────────────────────────────
def parse_iso(value: Any) -> datetime | None:
    """ISO 8601 → datetime *aware* (UTC quand le fuseau manque), sinon ``None``."""
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


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hours_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600.0)


def validate_window(start_at: Any, end_at: Any) -> tuple[str, str | None]:
    """Normalise la fenêtre ; ``end_at`` vide vaut « sans limite » (CPU)."""
    start = parse_iso(start_at)
    if start is None:
        raise CampaignError("date de début invalide (ISO 8601 attendu)")
    if end_at in (None, "", 0):
        return iso(start), None
    end = parse_iso(end_at)
    if end is None:
        raise CampaignError("date de fin invalide (ISO 8601 attendu)")
    if end <= start:
        raise CampaignError("la fin doit être postérieure au début")
    return iso(start), iso(end)


# ── Gain et coût (§4.7, §4.15) ────────────────────────────────────────
def campaign_gain(start_balance: float | None, end_balance: float | None) -> float | None:
    """Gain de la campagne = solde de fin − solde de départ (unités du coin)."""
    if start_balance is None or end_balance is None:
        return None
    return round(float(end_balance) - float(start_balance), 10)


def campaign_cost_eur(
    power_w: float | None, hours: float, eur_per_kwh: float | None
) -> float | None:
    """Coût de la période : puissance **à la prise** × durée × tarif (§4.15)."""
    if power_w is None or eur_per_kwh is None:
        return None
    if power_w < 0 or hours < 0 or eur_per_kwh < 0:
        return None
    return round((float(power_w) / 1000.0) * float(hours) * float(eur_per_kwh), 6)


# ── Contrôles avant démarrage (§4.7.3) ────────────────────────────────
def preflight(
    *,
    machine_online: bool = True,
    profile_error: str | None = None,
    busy_devices: Iterable[str] = (),
    psu_worst_pct: float | None = None,
    confirm_overload: bool = False,
) -> dict[str, Any]:
    """Contrôles bloquants et avertissements avant de lancer une campagne.

    Renvoie ``{"ok": bool, "blocking": [...], "warnings": [...], "psu": {...}}``.
    Un refus est *explicite* : chaque entrée porte un ``code`` et un message
    affichable tel quel dans le journal et dans l'interface.
    """
    blocking: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    busy = [str(label) for label in busy_devices if str(label).strip()]
    if busy:
        blocking.append(
            {
                "code": "device_busy",
                "message": "Device déjà engagé dans une autre campagne en cours : "
                + ", ".join(sorted(busy)),
            }
        )

    if not machine_online:
        blocking.append(
            {"code": "machine_unreachable", "message": "Machine injoignable — campagne non démarrée"}
        )

    if profile_error:
        blocking.append(
            {"code": "profile_invalid", "message": f"Profil invalide : {profile_error}"}
        )

    psu = {"worst_load_pct": psu_worst_pct, "limit_pct": MAX_PSU_PCT, "confirmed": confirm_overload}
    if psu_worst_pct is not None and float(psu_worst_pct) > MAX_PSU_PCT:
        message = (
            f"Charge prévue de {round(float(psu_worst_pct), 1)} % sur l'alimentation "
            f"(limite {MAX_PSU_PCT:.0f} %)"
        )
        if confirm_overload:
            warnings.append({"code": "psu_overload_confirmed", "message": message + " — confirmée"})
        else:
            blocking.append({"code": "psu_overload", "message": message + " : confirmation explicite requise"})

    return {"ok": not blocking, "blocking": blocking, "warnings": warnings, "psu": psu}


def summary_line(campaign: Any) -> str:
    """Une ligne lisible pour une notification ou un journal."""
    status = STATUS_LABELS.get(getattr(campaign, "status", ""), getattr(campaign, "status", "?"))
    return f"Campagne #{getattr(campaign, 'id', '?')} {getattr(campaign, 'coin', '?')} — {status}"
