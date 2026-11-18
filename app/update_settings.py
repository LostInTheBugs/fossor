"""Réglages de mise à jour (§4.19) : fréquence, application automatique, version ignorée.

Trois réglages, stockés dans la table ``settings`` (aucune migration) :

``update_check_interval_s``
    0 (jamais), 1 h, 6 h, 12 h ou 24 h — 1 h par défaut.
``update_auto_apply``
    applique automatiquement la dernière version trouvée (défaut : non).
``update_ignorer_auto``
    version quittée par un retour arrière : l'application automatique ne la
    réinstalle pas, jusqu'à une version plus récente ou une mise à jour manuelle.

``tick()`` est appelé par la boucle de fond existante (``app.auto_update``) :
il vérifie la cadence puis dépose une demande, sans jamais toucher au socket
Docker — c'est toujours ``updater_client`` qui écrit ``request.json``.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

#: Cadences proposées, en secondes (0 = jamais).
INTERVALS = (0, 3600, 6 * 3600, 12 * 3600, 24 * 3600)

DEFAULTS = {"check_interval_s": 3600, "auto_apply": False, "ignorer_auto": ""}

KEYS = {
    "check_interval_s": "update_check_interval_s",
    "auto_apply": "update_auto_apply",
    "ignorer_auto": "update_ignorer_auto",
}


def _cache() -> dict:
    """Dernier ``check`` réussi, pour ne pas relire GitHub à chaque tick."""
    return {"last_at": 0.0}


def read(db: Session) -> dict:
    from app.ai_config import get_setting
    from app.updater_client import is_valid_tag

    out = {
        "check_interval_s": get_setting(db, KEYS["check_interval_s"], DEFAULTS["check_interval_s"]),
        "auto_apply": bool(get_setting(db, KEYS["auto_apply"], DEFAULTS["auto_apply"])),
        "ignorer_auto": str(get_setting(db, KEYS["ignorer_auto"], "") or ""),
    }
    try:
        out["check_interval_s"] = int(out["check_interval_s"])
    except (TypeError, ValueError):
        out["check_interval_s"] = DEFAULTS["check_interval_s"]
    if out["check_interval_s"] not in INTERVALS:
        out["check_interval_s"] = DEFAULTS["check_interval_s"]
    if not is_valid_tag(out["ignorer_auto"]):
        out["ignorer_auto"] = ""
    return out


def save(db: Session, values: dict) -> dict:
    """Valide et écrit ; lève :class:`ValueError` sur une valeur refusée."""
    from app.ai_config import set_setting

    current = read(db)
    if "check_interval_s" in values:
        try:
            interval = int(values["check_interval_s"])
        except (TypeError, ValueError):
            raise ValueError("Fréquence de vérification invalide")
        if interval not in INTERVALS:
            raise ValueError("Fréquence attendue : 0, 3600, 21600, 43200 ou 86400 secondes")
        current["check_interval_s"] = interval
    if "auto_apply" in values:
        current["auto_apply"] = bool(values["auto_apply"])
    if "ignorer_auto" in values:
        from app.updater_client import is_valid_tag

        candidate = str(values.get("ignorer_auto") or "").strip()
        if candidate and not is_valid_tag(candidate):
            raise ValueError(f"Version ignorée invalide : {candidate!r}")
        current["ignorer_auto"] = candidate
    for key, setting in KEYS.items():
        set_setting(db, setting, current[key])
    db.commit()
    return current


def ignore_auto(db: Session, version: str) -> dict:
    """Après un retour arrière : l'auto ne réinstalle plus ``version`` ('' = plus rien)."""
    return save(db, {"ignorer_auto": version or ""})


def _last_check() -> float:
    """Horodatage du dernier ``check`` connu (état de ``update_checker``)."""
    try:
        from app import update_checker

        return float((update_checker.status().get("checked_at") or 0))
    except Exception:  # noqa: BLE001 — un état illisible ne bloque pas le tick
        return 0.0


async def tick(db: Session) -> str | None:
    """Cadence + application automatique.  Rend une action ('check' / 'apply') ou None."""
    from app import update_checker, updater_client

    settings = read(db)
    interval = settings["check_interval_s"]
    if not interval:
        return None
    since = time.time() - _last_check()
    if since >= interval:
        await update_checker.check(force=False)
        return "check"
    return None


async def auto_apply_tick(db: Session) -> str | None:
    """Dépose la demande si l'auto est active, qu'une version existe et n'est pas ignorée."""
    from app import update_checker, updater_client

    settings = read(db)
    if not settings["auto_apply"]:
        return None
    status = update_checker.status()
    latest = str(status.get("latest_version") or "")
    if not latest or latest == settings["ignorer_auto"]:
        return None
    if updater_client.pending_request() is not None:
        return None
    try:
        updater_client.request_update(db, latest)
    except ValueError:
        return None
    return "apply"
