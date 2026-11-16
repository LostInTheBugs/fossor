"""Mise à jour automatique (§4.18, partie 2) — fenêtre horaire et déclencheur.

Deux réglages en base, modifiables depuis Configuration → Mise à jour :

* ``auto_update_enabled`` — désactivé par défaut (décision de sûreté : une
  mise à jour automatique n'a de sens qu'une fois la précédente vérifiée) ;
* ``auto_update_window`` — ``{"start": "03:00", "end": "05:00",
  "timezone": "Europe/Paris"}``.  Une plage qui traverse minuit
  (``22:00`` → ``02:00``) est acceptée, ``start == end`` couvre la journée.

La boucle vérifie toutes les 60 s (``FOSSOR_AUTO_UPDATE_TICK``) qu'une
nouvelle version est disponible **et** que l'heure locale est dans la plage,
puis dépose la demande exactement comme le bouton manuel — sauvegarde
comprise, puisque c'est ``updater_client.request_update`` qui s'en charge.
Un échec n'est pas rejoué indéfiniment : même cible = pas de nouvelle
tentative avant ``RETRY_SECONDS``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.version import compare_versions, is_valid_version

try:  # image minimale sans tzdata : ne jamais casser l'import
    PARIS = ZoneInfo("Europe/Paris")
except ZoneInfoNotFoundError:  # pragma: no cover - dépend de l'image
    PARIS = timezone(timedelta(hours=1))

ENABLED_KEY = "auto_update_enabled"
WINDOW_KEY = "auto_update_window"
LAST_KEY = "auto_update_last_attempt"
LAST_STATE_KEY = "auto_update_last_state"

DEFAULT_WINDOW: dict[str, str] = {"start": "03:00", "end": "05:00", "timezone": "Europe/Paris"}
DEFAULT_TICK = 60
RETRY_SECONDS = 6 * 3600

_task: asyncio.Task | None = None


# ── Fenêtre horaire ────────────────────────────────────────────────────
def parse_hhmm(value: str) -> int:
    """``"03:05"`` → minutes depuis minuit ; :class:`ValueError` sinon."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"Horaire invalide : {value!r} (attendu HH:MM)")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"Horaire invalide : {value!r} (attendu HH:MM)")
    return hours * 60 + minutes


def normalize_window(raw) -> dict[str, str]:
    """Valide un réglage de plage et renvoie sa forme canonique."""
    if raw is None:
        return dict(DEFAULT_WINDOW)
    if not isinstance(raw, dict):
        raise ValueError("Plage horaire : objet attendu")
    start = str(raw.get("start") or DEFAULT_WINDOW["start"]).strip()
    end = str(raw.get("end") or DEFAULT_WINDOW["end"]).strip()
    zone = str(raw.get("timezone") or "Europe/Paris").strip() or "Europe/Paris"
    parse_hhmm(start)
    parse_hhmm(end)
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"Fuseau inconnu : {zone!r}")
    return {"start": start, "end": end, "timezone": zone}


def zone_of(window: dict):
    try:
        return ZoneInfo(window.get("timezone") or "Europe/Paris")
    except (ZoneInfoNotFoundError, ValueError, OSError):  # pragma: no cover
        return PARIS


def in_window(now: datetime, window: dict) -> bool:
    """Vrai si l'heure locale de *now* tombe dans la plage (minuit géré)."""
    local = now.astimezone(zone_of(window))
    minute = local.hour * 60 + local.minute
    start = parse_hhmm(window["start"])
    end = parse_hhmm(window["end"])
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


# ── Réglages ───────────────────────────────────────────────────────────
def state(db: Session) -> dict:
    from app.ai_config import get_setting

    return {
        "enabled": bool(get_setting(db, ENABLED_KEY, False)),
        "window": normalize_window(get_setting(db, WINDOW_KEY, DEFAULT_WINDOW)),
        "last_attempt": get_setting(db, LAST_KEY, None),
        "last_state": get_setting(db, LAST_STATE_KEY, None),
    }


def save(db: Session, enabled=None, window=None) -> dict:
    from app.ai_config import set_setting

    if enabled is not None:
        set_setting(db, ENABLED_KEY, bool(enabled))
    if window is not None:
        set_setting(db, WINDOW_KEY, normalize_window(window))
    db.commit()
    return state(db)


# ── Déclencheur ────────────────────────────────────────────────────────
def _last_attempt(db: Session) -> dict:
    from app.ai_config import get_setting

    value = get_setting(db, LAST_KEY, None)
    return value if isinstance(value, dict) else {}


def should_trigger(db: Session, now: datetime, update_status: dict) -> tuple[bool, str]:
    """Décide s'il faut déposer une demande maintenant (et pourquoi pas)."""
    from app import updater_client

    settings = state(db)
    if not settings["enabled"]:
        return False, "désactivée"
    if not updater_client.updater_present():
        return False, "updater absent"
    if not update_status.get("update_available"):
        return False, "déjà à jour"
    latest = update_status.get("latest_version")
    if not is_valid_version(latest or ""):
        return False, "dernière version inconnue"
    target = str(latest)
    installed = update_status.get("installed_version") or ""
    if is_valid_version(installed) and compare_versions(target, installed) <= 0:
        return False, "déjà à jour"
    if not in_window(now, settings["window"]):
        return False, "hors plage horaire"
    attempt = _last_attempt(db)
    if attempt.get("target") == target and isinstance(attempt.get("at"), str):
        try:
            last = datetime.fromisoformat(attempt["at"].replace("Z", "+00:00"))
        except ValueError:
            last = None
        if last is not None and last.tzinfo is not None:
            if (now - last).total_seconds() < RETRY_SECONDS:
                return False, "tentative déjà faite"
    return True, "prête"


def tick(db: Session, now: datetime | None = None) -> dict:
    """Un tour de la boucle : déclenche la demande si tout est réuni."""
    from app import update_checker, updater_client

    moment = now or datetime.now(timezone.utc)
    verdict, reason = should_trigger(db, moment, update_checker.status())
    result = {"triggered": False, "reason": reason, "target": None}
    if not verdict:
        return result
    latest = update_checker.status().get("latest_version")
    target = str(latest)
    try:
        updater_client.request_update(db, target)
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    from app.ai_config import set_setting

    set_setting(db, LAST_KEY, {"target": target, "at": moment.isoformat()})
    set_setting(db, LAST_STATE_KEY, "requested")
    db.commit()
    result.update({"triggered": True, "reason": "demande déposée", "target": target})
    return result


def tick_interval() -> int:
    raw = os.environ.get("FOSSOR_AUTO_UPDATE_TICK")
    if raw is None or raw.strip() == "":
        return DEFAULT_TICK
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TICK


async def _loop() -> None:
    while True:
        interval = tick_interval()
        if interval <= 0:
            return
        try:
            from app.database import SessionLocal

            db = SessionLocal()
            try:
                tick(db)
            finally:
                db.close()
        except Exception:  # pragma: no cover - la boucle ne meurt jamais
            pass
        await asyncio.sleep(interval)


def start() -> asyncio.Task | None:
    """Démarre la boucle si l'intervalle n'est pas nul ; idempotent."""
    global _task
    if tick_interval() <= 0:
        return None
    if _task is not None and not _task.done():
        return _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    _task = loop.create_task(_loop())
    return _task


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
