"""AI analysis configuration and provider probe (§4.12)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app import ai_client, ai_config, veille
from app.ai_config import AIError, WHICHES
from app.database import get_db
from app.dependencies import require_login
from app.models import ActorEnum, AuditLog

router = APIRouter(prefix="/api/config/ai", tags=["ai"])


def _audit(db: Session, action: str, result: str, output: str | None = None) -> None:
    db.add(AuditLog(
        actor=ActorEnum.fred,
        action=action,
        object_ref="ai",
        result=result[:200],
        output=(output or "")[:500] or None,
    ))
    db.commit()


@router.get("")
def get_ai_config(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Current AI configuration — the API key is replaced by ``has_api_key``."""
    return {**ai_config.snapshot(db), "veille_max_minutes": veille.max_minutes(db)}


@router.patch("")
def patch_ai_config(
    payload: dict,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Update one or both providers plus the global switches."""
    try:
        touched = ai_config.patch_config(db, payload)
    except AIError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    names = ", ".join(row.which for row in touched) or "réglages seuls"
    _audit(db, "ai-config", "ok", f"modifié : {names}")
    return ai_config.snapshot(db)


@router.post("/test")
async def test_ai_provider(
    which: str = Query(default="primary"),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Minimal ``POST {base_url}/chat/completions``; the key never comes back."""
    if which not in WHICHES:
        raise HTTPException(status_code=400, detail="which doit être « primary » ou « fallback »")
    row = ai_config.get_provider(db, which)
    if row is None:
        return {"ok": False, "error": f"Fournisseur « {which} » non configuré"}
    provider = ai_config.provider_dict(row)
    if not (provider or {}).get("base_url"):
        return {"ok": False, "error": "Adresse (base_url) vide"}

    api_key = ai_config.provider_secret(row)
    result = await ai_client.probe_provider(provider, api_key)

    if result.get("ok"):
        summary = f"which={which} ok latency_ms={result['latency_ms']} model={result.get('model')}"
        _audit(db, "ai-test", "ok", summary)
    else:
        summary = f"which={which} erreur={result.get('error')} latency_ms={result.get('latency_ms')}"
        _audit(db, "ai-test", "erreur", summary)
    return result


@router.get("/veille")
def get_veille(db: Session = Depends(get_db), _user=Depends(require_login)):
    """État du dernier tick + cadence de la règle « cadence de la veille »."""
    return veille.snapshot(db)


@router.post("/veille/run", status_code=202)
def run_veille(
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Déclenche un tick Hermes ; le tick tourne en arrière-plan (polling GET)."""
    ok, reason = veille.provider_ready(db)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Veille indisponible : {reason}")
    state = veille.trigger(db, "ui", str(request.base_url).rstrip("/"))
    if state.get("already_running"):
        raise HTTPException(status_code=409, detail="Une veille est déjà en cours")
    _audit(db, "veille-hermes", "lancée", "tick déclenché depuis l'écran Analyse des tokens")
    return veille.snapshot(db)


@router.post("/veille/max-minutes")
def set_veille_max_minutes(
    payload: dict,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Durée maximale d'un tick (1 à 240 minutes, 30 par défaut)."""
    raw = payload.get("minutes")
    if isinstance(raw, bool) or raw is None:
        raise HTTPException(status_code=400, detail="minutes doit être un entier")
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="minutes doit être un entier")
    if not 1 <= minutes <= veille.MAX_MINUTES_CEILING:
        raise HTTPException(
            status_code=400,
            detail=f"minutes doit être entre 1 et {veille.MAX_MINUTES_CEILING}",
        )
    ai_config.set_setting(db, veille.MAX_MINUTES_KEY, minutes)
    db.commit()
    _audit(db, "veille-max", "ok", f"durée maximale d'un tick : {minutes} min")
    return veille.snapshot(db)


@router.post("/token")
def regenerate_hermes_token(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Create (or replace) the inbound Hermes token — clear value returned once."""
    existed = ai_config.token_info(db)["has_token"]
    issued = ai_config.new_token(db)
    _audit(
        db,
        "hermes-token",
        "régénéré" if existed else "créé",
        "jeton entrant Hermes " + ("régénéré" if existed else "créé") + " (valeur non journalisée)",
    )
    return issued
