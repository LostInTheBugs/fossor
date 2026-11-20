"""Niveau de journalisation, voyants des tâches de routine et sauvegarde.

Le bruit vient des tâches de routine : collecte, vérification de version, tick de
veille, battement de l'updater, sauvegarde.  Elles n'écrivent plus une ligne
d'audit à chaque passage ; leur état est un voyant sur le tableau de bord et une
ligne n'est écrite qu'au **changement d'état** (WARNING) ou sur une erreur
(ERROR), dans la limite du niveau configuré.

Les actions de l'utilisateur et de Hermes, ainsi que les changements de
configuration, restent toujours audités : c'est la traçabilité, indépendante du
niveau.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_LEVEL_KEY = "log_level"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "WARNING"
SEVERITY_RANK = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

ROUTINE_KEY = "routine_status"
BACKUP_INTERVAL = 6 * 3600
KEEP_BACKUPS = 7
ROUTINE_LABELS = {
    "backup": "Sauvegarde",
    "collect": "Collecte",
    "update-check": "Vérification de version",
    "veille": "Veille Hermes",
    "updater": "Battement de l'updater",
}
DEFAULT_INTERVALS = {
    "backup": BACKUP_INTERVAL,
    "collect": 60,
    "update-check": 6 * 3600,
    "veille": 6 * 3600,
    "updater": 300,
}

logger = logging.getLogger("fossor.routines")
_backup_task: asyncio.Task | None = None


# ── Niveau de log ─────────────────────────────────────────────────────
def get_log_level(db: Any) -> str:
    from app.ai_config import get_setting

    value = get_setting(db, LOG_LEVEL_KEY, DEFAULT_LOG_LEVEL)
    text = str(value or "").strip().upper()
    return text if text in LOG_LEVELS else DEFAULT_LOG_LEVEL


def apply_logging(level: str) -> None:
    """Applique le niveau aux logs applicatifs écrits sur stdout."""
    numeric = getattr(logging, str(level).upper(), logging.WARNING)
    logging.getLogger("fossor").setLevel(numeric)
    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in root.handlers:
        handler.setLevel(numeric)


def set_log_level(db: Any, level: str) -> str:
    from app.ai_config import set_setting

    text = str(level or "").strip().upper()
    if text not in LOG_LEVELS:
        raise ValueError("niveau invalide : DEBUG, INFO, WARNING, ERROR ou CRITICAL attendu")
    set_setting(db, LOG_LEVEL_KEY, text)
    db.commit()
    apply_logging(text)
    return text


def allows(level: str, severity: str) -> bool:
    return SEVERITY_RANK.get(str(severity).upper(), 30) >= SEVERITY_RANK.get(
        str(level).upper(), 30
    )


# ── Voyants des routines ──────────────────────────────────────────────
def _load(db: Any) -> dict[str, Any]:
    from app.ai_config import get_setting

    value = get_setting(db, ROUTINE_KEY, {})
    return value if isinstance(value, dict) else {}


def mark_routine(
    db: Any,
    name: str,
    ok: bool,
    detail: str = "",
    *,
    interval_s: int | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """Enregistre le passage d'une routine ; n'audite qu'au changement ou erreur."""
    from app.ai_config import set_setting
    from app.models import AuditLog

    states = _load(db)
    previous = states.get(name) or {}
    state = "ok" if ok else "fail"
    message = str(detail or "")[:300]
    # Premier passage : on enregistre l'état sans ligne d'audit (rien n'a changé).
    changed = bool(previous) and previous.get("state") != state
    # Une erreur identique à la précédente n'est pas un nouvel événement : on
    # évite une ligne toutes les 60 s sur une machine injoignable.
    interesting = changed or (not ok and previous.get("detail") != message)
    level = get_log_level(db)
    severity = (severity or ("ERROR" if not ok else "WARNING")).upper()
    if interesting and allows(level, severity):
        db.add(
            AuditLog(
                actor="system",
                action=f"routine-{name}",
                object_ref=name[:255],
                result="ok" if ok else "fail",
                output=message,
            )
        )
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "state": state,
        "last_run": now,
        "detail": message,
        "interval_s": int(interval_s or previous.get("interval_s") or DEFAULT_INTERVALS.get(name, 3600)),
    }
    if ok:
        entry["last_ok"] = now
    elif previous.get("last_ok"):
        entry["last_ok"] = previous["last_ok"]
    states[name] = entry
    set_setting(db, ROUTINE_KEY, states)
    db.commit()
    return entry


def routine_snapshot(db: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """État de chaque routine : ``ok`` (vert), ``late`` (orange), ``fail`` (rouge)."""
    states = _load(db)
    moment = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for name, label in ROUTINE_LABELS.items():
        entry = states.get(name) or {}
        interval = int(entry.get("interval_s") or DEFAULT_INTERVALS.get(name, 3600))
        age: int | None = None
        last_run = entry.get("last_run")
        if last_run:
            try:
                age = int((moment - datetime.fromisoformat(last_run)).total_seconds())
            except ValueError:
                age = None
        if not entry:
            state = "unknown"
        elif entry.get("state") == "fail":
            state = "fail"
        elif age is not None and age > interval * 2:
            state = "late"
        else:
            state = "ok"
        out.append(
            {
                "name": name,
                "label": label,
                "state": state,
                "age_s": age,
                "detail": entry.get("detail") or "",
                "last_run": last_run,
                "interval_s": interval,
            }
        )
    return out


# ── Sauvegarde ────────────────────────────────────────────────────────
def _data_dir() -> Path:
    env_dir = os.environ.get("FOSSOR_DATA_DIR")
    if env_dir:
        path = Path(env_dir)
        if path.is_dir():
            return path
    default = Path("/data")
    if default.is_dir():
        return default
    return Path(__file__).resolve().parent.parent / "data"


def _db_path() -> Path | None:
    url = os.environ.get("DATABASE_URL") or ""
    if not url.startswith("sqlite"):
        return None
    raw = url.split("sqlite:///", 1)[-1]
    return Path(raw) if raw else None


def run_backup() -> dict[str, Any]:
    """Copie cohérente de la base (API ``sqlite3.backup``) + rotation."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        source = _db_path()
        if source is None or not source.exists():
            return mark_routine(db, "backup", False, "base de données introuvable", interval_s=BACKUP_INTERVAL)
        target_dir = _data_dir() / "backups"
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = target_dir / f"fossor-{stamp}.db"
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
        old = sorted(target_dir.glob("fossor-*.db"))[:-KEEP_BACKUPS]
        for path in old:
            path.unlink(missing_ok=True)
        return mark_routine(
            db,
            "backup",
            True,
            f"{target.name} ({max(1, target.stat().st_size // 1024)} Kio)",
            interval_s=BACKUP_INTERVAL,
        )
    except Exception as exc:  # pragma: no cover - défensif
        return mark_routine(db, "backup", False, f"sauvegarde impossible : {exc}", interval_s=BACKUP_INTERVAL)
    finally:
        db.close()


async def _backup_loop() -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            await asyncio.to_thread(run_backup)
        except Exception:  # pragma: no cover - défensif
            logger.warning("sauvegarde en échec", exc_info=True)


def start_backup() -> None:
    """Une sauvegarde au démarrage, puis toutes les six heures."""
    global _backup_task
    try:
        run_backup()
    except Exception:  # pragma: no cover - défensif
        logger.warning("sauvegarde de démarrage en échec", exc_info=True)
    if _backup_task is not None and not _backup_task.done():
        return
    try:
        _backup_task = asyncio.create_task(_backup_loop())
    except RuntimeError:  # pragma: no cover - pas de boucle au moment de l'import
        _backup_task = None


def dump_state(db: Any) -> dict[str, Any]:
    return {"level": get_log_level(db), "routines": routine_snapshot(db)}


__all__ = [
    "LOG_LEVELS",
    "apply_logging",
    "allows",
    "dump_state",
    "get_log_level",
    "mark_routine",
    "routine_snapshot",
    "run_backup",
    "set_log_level",
    "start_backup",
]
