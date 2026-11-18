"""API de mise à jour (§4.18) : vérification, demande, mise à jour automatique.

L'application n'a jamais le socket Docker : ``POST /api/update/apply`` prend la
sauvegarde (base + configuration) puis dépose ``request.json`` dans le volume
partagé ; c'est le conteneur ``fossor-updater`` qui fait le reste et écrit
``status.json`` / ``history.jsonl``, que ces routes relisent.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app import auto_update, update_checker, update_settings, updater_client
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog


def _installed_version() -> str:
    """Version de l'application lue dans ``VERSION`` (pas de constante en dur)."""
    from pathlib import Path

    try:
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
    except OSError:
        return ""

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
    payload["settings"] = update_settings.read(db)
    # §4.19 : la liste « Mettre à jour vers… » et l'état des boutons viennent
    # du même appel — une seule requête pour tout l'écran.
    payload["known_versions"] = update_checker.known_versions()
    payload["request_pending"] = updater_client.pending_request() is not None
    return payload


@router.post("/check")
async def check_now(_user=Depends(require_login)):
    """Force une vérification GitHub (bouton « Vérifier maintenant »)."""
    await update_checker.check(force=True)
    return update_checker.status()


@router.post("/apply")
async def apply_update(
    payload: dict = Body(default_factory=dict),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Sauvegarde la base puis demande la mise à jour au compagnon."""
    # 2026.11.004-c5 : la cible demandée est utilisée telle quelle ; elle est
    # seulement validée (format puis release publiée, avec un ``check`` si le
    # cache ne la connaît pas).  Sans cible : la dernière version connue.
    requested = payload.get("target") or payload.get("version")
    if requested in (None, ""):
        target = str(update_checker.status().get("latest_version") or "")
    else:
        try:
            target = updater_client.normalize_target(str(requested).strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if target not in update_checker.known_versions():
            await update_checker.check(force=True)
            if target not in update_checker.known_versions():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Version {target} inconnue : aucune release publiée "
                        "sous ce tag"
                    ),
                )
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


# ── Réglages et actions §4.19 ──────────────────────────────────────────────
@router.get("/history")
def update_history(limit: int = 200, _user=Depends(require_login)):
    """Historique des mises à jour (``history.jsonl``), le plus récent d'abord."""
    return {"entries": updater_client.read_history(max(1, min(limit, 500)))}


@router.put("/settings")
def put_update_settings(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Fréquence de vérification, application automatique, version ignorée."""
    try:
        values = update_settings.save(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _audit(
        db,
        "update_settings",
        "update",
        f"intervalle={values['check_interval_s']} auto={values['auto_apply']} "
        f"ignore={values['ignorer_auto']}",
    )
    db.commit()
    return {"ok": True, "settings": values}


def _previous_version() -> dict:
    previous = updater_client.payload().get("precedente") or {}
    if not previous.get("version"):
        raise HTTPException(
            status_code=400,
            detail="Aucune version précédente conservée sur cette installation",
        )
    if not previous.get("image"):
        raise HTTPException(
            status_code=400,
            detail=f"L'image de la version {previous['version']} n'est plus présente sur le serveur",
        )
    return previous


@router.post("/rollback")
def rollback(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Revenir à la version précédente conservée, base restaurée."""
    previous = _previous_version()
    try:
        result = updater_client.request_action(db, "rollback", previous["version"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:  # une version de développement (« -dev ») n'est pas un tag normalisable
        update_settings.ignore_auto(db, _installed_version())
    except ValueError:
        pass
    _audit(db, "update_rollback", previous["version"], f"cible={previous['version']}")
    db.commit()
    return {"ok": True, "target": previous["version"], **result}


@router.post("/forget-previous")
def forget_previous(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Supprimer l'image conservée de la version précédente."""
    previous = _previous_version()
    try:
        result = updater_client.request_action(db, "forget_previous", previous["version"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, "update_forget_previous", previous["version"], f"cible={previous['version']}")
    db.commit()
    return {"ok": True, "target": previous["version"], **result}


@router.delete("/request")
def cancel_request(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Annuler la demande en attente (le fichier n'est pas encore traité)."""
    path = updater_client.request_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    _audit(db, "update_cancel", "request", f"annulée={existed}")
    db.commit()
    return {"ok": True, "cancelled": existed}
