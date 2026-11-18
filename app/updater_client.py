"""Client du conteneur compagnon ``fossor-updater`` (§4.18, partie 2).

Fossor n'a **jamais** accès au socket Docker : la mise à jour Docker est
pilotée par un petit conteneur frère, seul à monter ``/var/run/docker.sock``.
Le dialogue passe par trois fichiers dans ``<data>/update/`` — aucune API
réseau, aucun jeton :

``request.json``
    écrit par Fossor : ``{"target": "2026.11.004", "image": "...",
    "requested_at": "...", "nonce": "..."}``.
``status.json``
    écrit par l'updater : ``state`` (idle, pulling, backing_up, switching,
    health_check, done, rolled_back, failed), ``from``, ``to``, ``step``,
    ``message``, horodatages et ``heartbeat_at`` (rafraîchi toutes les 10 s,
    c'est ce qui prouve que le compagnon est vivant).
``history.jsonl``
    une ligne JSON par transition, plus les refus.

Avant d'écrire une demande, Fossor prend une sauvegarde **cohérente** :
``<data>/backups/pre-<cible>-<horodatage>/`` avec ``fossor.db`` (API
``sqlite3.Connection.backup``, pas une copie de fichier) et ``config.json``
(export de configuration sans secret).  C'est cette sauvegarde que l'updater
fait restaurer par l'application en cas de retour arrière.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

#: Le tag accepté par l'updater : ``ANNEE.MOIS.NNN`` (+ ``-cN``).
TAG_RE = re.compile(r"^20\d\d\.\d\d\.\d{3}(-c\d+)?$")

#: Image configurée pour l'installation (le compose la substitue aussi).
DEFAULT_IMAGE = "ghcr.io/lostinthebugs/fossor"

#: Au-delà, le battement de cœur du compagnon est considéré comme périmé.
STALE_HEARTBEAT_SECONDS = 180

#: États pendant lesquels une demande est en cours.
ACTIVE_STATES = ("pulling", "backing_up", "switching", "health_check")

#: §4.19 — actions acceptées par le compagnon.
ACTIONS = ("update", "rollback", "forget_previous")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def data_dir() -> Path:
    return Path(os.environ.get("FOSSOR_DATA_DIR") or "/data")


def update_dir() -> Path:
    """Dossier d'échange, réglable par ``FOSSOR_UPDATE_DIR`` (tests, dev)."""
    override = os.environ.get("FOSSOR_UPDATE_DIR")
    return Path(override) if override else data_dir() / "update"


def status_path() -> Path:
    return update_dir() / "status.json"


def request_path() -> Path:
    return update_dir() / "request.json"


def history_path() -> Path:
    return update_dir() / "history.jsonl"


def backup_dir() -> Path:
    return data_dir() / "backups"


def expected_image() -> str:
    """Image autorisée : celle que l'installation a configurée."""
    return (os.environ.get("FOSSOR_IMAGE") or DEFAULT_IMAGE).strip()


def updater_disabled() -> bool:
    return (os.environ.get("FOSSOR_UPDATER_DISABLED") or "").strip() not in ("", "0", "false")


# ── Validation ─────────────────────────────────────────────────────────
def is_valid_tag(value) -> bool:
    return isinstance(value, str) and bool(TAG_RE.match(value.strip()))


def normalize_target(value) -> str:
    """Tag de cible validé, sinon :class:`ValueError` explicite."""
    if not is_valid_tag(value):
        raise ValueError(
            f"Cible invalide : {value!r} (attendu ANNEE.MOIS.NNN ou ANNEE.MOIS.NNN-cN)"
        )
    return value.strip()


# ── Fichiers ───────────────────────────────────────────────────────────
def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Écriture atomique : l'updater ne lit jamais un fichier à moitié écrit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_status() -> dict | None:
    return _read_json(status_path())


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def heartbeat_age() -> float | None:
    status = read_status() or {}
    moment = _parse_time(status.get("heartbeat_at") or status.get("updated_at"))
    if moment is None:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


def updater_present() -> bool:
    """Compagnon installé et vivant ?

    Présent = ``status.json`` existe et son battement de cœur a moins de trois
    minutes, **ou** une mise à jour est en cours (un ``docker pull`` long fait
    naturellement vieillir le battement).
    """
    if updater_disabled():
        return False
    status = read_status()
    if status is None:
        return False
    if status.get("state") in ACTIVE_STATES:
        return True
    age = heartbeat_age()
    return age is not None and age <= STALE_HEARTBEAT_SECONDS


def read_history(limit: int = 50) -> list[dict]:
    """Dernières lignes de ``history.jsonl``, la plus récente en premier."""
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines[-max(1, limit):]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    entries.reverse()
    return entries


# ── Sauvegarde avant mise à jour ───────────────────────────────────────
def _database_path() -> Path:
    from app.database import engine

    url = engine.url
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("La sauvegarde avant mise à jour ne gère que SQLite")
    return Path(url.database)


def backup_database(source: Path, destination: Path) -> None:
    """Copie cohérente d'une base SQLite vivante (API ``backup``)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(destination))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_pre_update_backup(db: Session, target: str) -> Path:
    """``<data>/backups/pre-<cible>-<horodatage>/`` : base + configuration."""
    from app.config_io import build_export
    from app.version import is_valid_version

    tag = normalize_target(target)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir() / f"pre-{tag}-{stamp}"
    suffix = 1
    while path.exists():
        suffix += 1
        path = backup_dir() / f"pre-{tag}-{stamp}-{suffix}"
    path.mkdir(parents=True, exist_ok=False)

    database = _database_path()
    backup_database(database, path / "fossor.db")

    config = build_export(db, include_secrets=False)
    (path / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    installed = ""
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        installed = version_file.read_text().strip()
    except OSError:
        pass
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "target": tag,
                "from": installed,
                "created_at": stamp,
                "database": "fossor.db",
                "config": "config.json",
                "schema_valid": is_valid_version(tag),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


# ── Demande de mise à jour ─────────────────────────────────────────────
def pending_request() -> dict | None:
    """Demande déposée et pas encore prise en charge par le compagnon.

    L'updater note le ``nonce`` qu'il traite dans ``status.json`` : tant qu'il
    ne l'a pas fait, la demande est en attente (même si l'état est encore
    ``idle``) ; une fois traitée, elle n'est plus « en cours ».
    """
    request = _read_json(request_path())
    if request is None:
        return None
    status = read_status() or {}
    if status.get("state") in ACTIVE_STATES:
        return request
    if status.get("request_nonce") == request.get("nonce"):
        return None
    return request


def request_update(db: Session, target: str, image: str | None = None) -> dict:
    """Sauvegarde puis dépose ``request.json``.  Lève :class:`ValueError` sinon."""
    tag = normalize_target(target)
    if not updater_present():
        raise ValueError(
            "Le service fossor-updater n'est pas joignable : mise à jour pilotée "
            "indisponible sur cette installation"
        )
    active = pending_request()
    if active is not None:
        raise ValueError(
            f"Une mise à jour vers {active.get('target')} est déjà en cours"
        )

    backup = create_pre_update_backup(db, tag)
    request = {
        "target": tag,
        "image": (image or expected_image()).strip(),
        "requested_at": _now(),
        "nonce": uuid.uuid4().hex,
        "backup": str(backup),
    }
    _write_json_atomic(request_path(), request)
    return {"request": request, "backup": str(backup)}


def request_action(db: Session, action: str, target: str) -> dict:
    """Dépose une demande d'action (``rollback``, ``forget_previous``).

    Une mise à jour prend une sauvegarde préalable ; un retour arrière utilise
    celle prise avant l'installation de la version quittée, et un oubli ne
    touche pas à la base.
    """
    if action not in ACTIONS:
        raise ValueError(f"Action inconnue : {action!r}")
    tag = normalize_target(target)
    if not updater_present():
        raise ValueError(
            "Le service fossor-updater n'est pas joignable : action indisponible "
            "sur cette installation"
        )
    active = pending_request()
    if active is not None:
        raise ValueError(f"Une demande vers {active.get('target')} est déjà en attente")

    request = {
        "target": tag,
        "image": expected_image().strip(),
        "requested_at": _now(),
        "nonce": uuid.uuid4().hex,
        "action": action,
    }
    backup = None
    if action == "update":
        backup = create_pre_update_backup(db, tag)
        request["backup"] = str(backup)
    _write_json_atomic(request_path(), request)
    return {"request": request, "backup": str(backup) if backup else None}


def payload() -> dict:
    """Bloc ``updater`` de ``GET /api/update/status``."""
    status = read_status()
    present = updater_present()
    age = heartbeat_age()
    installed = status is not None
    stale = installed and age is not None and age > STALE_HEARTBEAT_SECONDS and not present
    return {
        "present": present,
        # 2026.11.003-c5: un conteneur présent dont le battement est figé n'est
        # pas « absent » — c'est un compagnon sans réponse, et l'écran doit le
        # dire autrement (marche à suivre différente).
        "installed": installed,
        "stale": stale,
        "stale_minutes": None if age is None else round(age / 60, 1),
        "state": (status or {}).get("state") or ("idle" if present else "absent"),
        # 2026.11.003-c4: the companion is a *separate* image and can lag
        # behind the app (prod kept a -c1 updater after a -c3 app update).
        "version": (status or {}).get("updater_version")
        or (os.environ.get("FOSSOR_UPDATER_TAG") or "").strip()
        or None,
        "from": (status or {}).get("from"),
        "to": (status or {}).get("to"),
        "step": (status or {}).get("step"),
        "message": (status or {}).get("message"),
        "started_at": (status or {}).get("started_at"),
        "updated_at": (status or {}).get("updated_at"),
        "heartbeat_at": (status or {}).get("heartbeat_at"),
        "heartbeat_age_seconds": None if age is None else round(age, 1),
        "image": expected_image(),
        "directory": str(update_dir()),
        "pending": (pending_request() or {}).get("target"),
        # §4.19 : version conservée après une mise à jour (retour arrière possible).
        "precedente": (
            status.get("precedente")
            if isinstance((status or {}).get("precedente"), dict)
            else None
        ),
        "history": read_history(20),
    }
