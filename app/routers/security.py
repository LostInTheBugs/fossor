"""Réglages de sécurité, niveau de journalisation et voyants des routines.

``GET /api/auth/state`` est volontairement sans authentification : le bandeau
« Authentification désactivée » doit s'afficher sur toutes les pages, y compris
l'écran de connexion.  Il n'expose ni secret ni liste complète : uniquement
l'état, l'IP vue par le serveur et le fait qu'un contournement s'applique.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import auth_settings, observability
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog

router = APIRouter(prefix="/api", tags=["security"])


@router.get("/auth/state")
def auth_state(request: Request, db: Session = Depends(get_db)):
    state = auth_settings.evaluate(db, request)
    return {
        "auth_enabled": state["auth_enabled"],
        "bypass": state["bypass"],
        "client_ip": state["client_ip"],
        "trusted_networks": state["trusted_networks"],
        "stay_logged_in_days": state["stay_logged_in_days"],
    }


@router.get("/security/settings")
def read_security(db: Session = Depends(get_db), _user=Depends(require_login)):
    return auth_settings.read_settings(db)


@router.put("/security/settings")
def write_security(
    payload: dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    before = auth_settings.read_settings(db)
    try:
        after = auth_settings.write_settings(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    detail = (
        f"authentification={'activée' if after['auth_enabled'] else 'désactivée'}"
        f" ; réseaux={', '.join(after['trusted_networks'])}"
        f" ; X-Forwarded-For={'de confiance' if after['trust_proxy'] else 'ignoré'}"
        f" ; rester connecté={after['stay_logged_in_days']} j"
        f" ; depuis {auth_settings.client_ip(request, after['trust_proxy'])}"
    )
    changed = before != after
    db.add(
        AuditLog(
            actor=str(user),
            action="security-settings",
            object_ref="auth",
            result="ok" if changed else "ok",
            output=detail[:500],
        )
    )
    db.commit()
    return after


@router.get("/logs/settings")
def read_logs(db: Session = Depends(get_db), _user=Depends(require_login)):
    return {"level": observability.get_log_level(db), "levels": list(observability.LOG_LEVELS)}


@router.put("/logs/settings")
def write_logs(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    try:
        level = observability.set_log_level(db, str(payload.get("level") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(
        AuditLog(
            actor=str(user),
            action="log-level",
            object_ref="logging",
            result="ok",
            output=f"niveau appliqué : {level}",
        )
    )
    db.commit()
    return {"level": level, "levels": list(observability.LOG_LEVELS)}


@router.get("/routines")
def read_routines(db: Session = Depends(get_db), _user=Depends(require_login)):
    return {
        "level": observability.get_log_level(db),
        "routines": observability.routine_snapshot(db),
    }
