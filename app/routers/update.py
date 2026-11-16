"""API de mise à jour (§4.18) : vérification, demande, mise à jour automatique.

L'application n'a jamais le socket Docker : ``POST /api/update/apply`` prend la
sauvegarde (base + configuration) puis dépose ``request.json`` dans le volume
partagé ; c'est le conteneur ``fossor-updater`` qui fait le reste et écrit
``status.json`` / ``history.jsonl``, que ces routes relisent.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app import auto_update, update_checker, updater_client
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog

router = APIRouter(prefix="/api/update", tags=["update"])


def _audit(db: Session, action: str, object_ref: str, output: str) -> None:
    db.add(
        AuditLog(
            actor="fred",
            action=action,
            object_ref=object_ref,
            result="ok",
            output=output[:500],
        )
    )


@router.get("/status")
async def update_status(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Version installée, notes manquantes, état du compagnon, auto-update."""
    payload = update_checker.status()
    payload["updater"] = updater_client.payload()
    payload["auto"] = auto_update.state(db)
    return payload


@router.post("/check")
async def check_now(_user=Depends(require_login)):
    """Force une vérification GitHub (bouton « Vérifier maintenant »)."""
    await update_checker.check(force=True)
    return update_checker.status()


@router.post("/apply")
def apply_update(
    payload: dict = Body(default_factory=dict),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Sauvegarde la base puis demande la mise à jour au compagnon."""
    target = str(payload.get("target") or update_checker.status().get("latest_version") or "")
    try:
        result = updater_client.request_update(db, target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(
        db,
        "update_request",
        result["request"]["target"],
        f"cible={result['request']['target']} sauvegarde={result['backup']}",
    )
    db.commit()
    return {
        "ok": True,
        "target": result["request"]["target"],
        "backup": result["backup"],
        "requested_at": result["request"]["requested_at"],
        "updater": updater_client.payload(),
    }


@router.get("/auto")
def get_auto(db: Session = Depends(get_db), _user=Depends(require_login)):
    return {**auto_update.state(db), "default_window": auto_update.DEFAULT_WINDOW}


@router.put("/auto")
def put_auto(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Active/désactive et règle la plage horaire de la mise à jour automatique."""
    enabled = payload.get("enabled")
    window = payload.get("window")
    if enabled is None and window is None:
        raise HTTPException(status_code=400, detail="enabled ou window attendu")
    try:
        state = auto_update.save(db, enabled=enabled, window=window)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(
        db,
        "update_auto_settings",
        "auto",
        f"enabled={state['enabled']} plage={state['window']['start']}-{state['window']['end']}",
    )
    db.commit()
    return {"ok": True, **state}
