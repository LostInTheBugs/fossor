"""API du mode boost (§10.3) : pool, écran de validation, lancement, annulation."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import boost as B
from app import boost_runner as RUN
from app import campaigns as C
from app import pilotage
from app.ai_config import get_setting, set_setting
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Boost, BoostDevice, BoostEvent

router = APIRouter(prefix="/api", tags=["boost"])

SETTING_MAX_SHARE = "boost_max_share_pct"
SETTING_DEFAULT_HOURS = "boost_default_hours"


def _audit(db: Session, actor: str, action: str, ref: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor=actor[:50],
            action=action[:50],
            object_ref=ref[:255],
            result=result[:200],
            output=output[:500],
        )
    )
    db.commit()


def settings(db: Session) -> dict:
    return {
        "max_share_pct": float(get_setting(db, SETTING_MAX_SHARE, B.DEFAULT_MAX_SHARE_PCT) or B.DEFAULT_MAX_SHARE_PCT),
        "default_hours": float(get_setting(db, SETTING_DEFAULT_HOURS, B.DEFAULT_HOURS) or B.DEFAULT_HOURS),
    }


def boost_to_dict(db: Session, boost: Boost, *, events: bool = False) -> dict:
    devices = (
        db.query(BoostDevice).filter(BoostDevice.boost_id == boost.id).order_by(BoostDevice.id.asc()).all()
    )
    payload = {
        "id": boost.id,
        "uuid": boost.uuid,
        "campaign_id": boost.campaign_id,
        "coin": boost.coin,
        "status": boost.status,
        "status_label": B.STATUS_LABELS.get(boost.status, boost.status),
        "origin": boost.origin,
        "scope": boost.scope,
        "proportion": boost.proportion,
        "duration_h": boost.duration_h,
        "started_at": boost.started_at,
        "ends_at": boost.ends_at,
        "ended_at": boost.ended_at,
        "restore_deadline": boost.restore_deadline,
        "stop_requested": bool(boost.stop_requested),
        "note": boost.note or "",
        "devices": [
            {
                "machine_id": row.machine_id,
                "device_uuid": row.device_uuid,
                "kind": row.kind,
                "label": row.label,
                "hiveos": bool(row.hiveos),
                "verified": bool(row.verified),
                "restored_at": row.restored_at,
            }
            for row in devices
        ],
        "device_count": len(devices),
    }
    if events:
        rows = (
            db.query(BoostEvent)
            .filter(BoostEvent.boost_id == boost.id)
            .order_by(BoostEvent.id.asc())
            .all()
        )
        payload["events"] = [
            {
                "ts": row.ts,
                "kind": row.kind,
                "message": row.message,
                "result": row.result,
                "simulated": bool(row.simulated),
                "detail": json.loads(row.detail) if row.detail else None,
            }
            for row in rows
        ]
    return payload


@router.get("/boosts/settings")
def read_settings(db: Session = Depends(get_db), _user=Depends(require_login)):
    return settings(db)


@router.put("/boosts/settings")
def update_settings(payload: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    if "max_share_pct" in payload:
        try:
            value = float(payload["max_share_pct"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="max_share_pct : nombre attendu") from None
        if value < 1 or value > 100:
            raise HTTPException(status_code=422, detail="max_share_pct : entre 1 et 100 %")
        set_setting(db, SETTING_MAX_SHARE, value)
    if "default_hours" in payload:
        try:
            value = B.validate_duration(payload["default_hours"])
        except B.BoostError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        set_setting(db, SETTING_DEFAULT_HOURS, value)
    db.commit()
    _audit(db, _user, "boost-settings", "réglages boost", "ok", json.dumps(settings(db))[:400])
    return settings(db)


@router.get("/boosts/pool")
def read_pool(db: Session = Depends(get_db), _user=Depends(require_login)):
    view = RUN.pool_payload(db)
    return {
        "simulation": pilotage.global_simulation(db),
        "settings": settings(db),
        **view,
    }


@router.get("/boosts")
def list_boosts(db: Session = Depends(get_db), _user=Depends(require_login)):
    rows = db.query(Boost).order_by(Boost.id.desc()).limit(100).all()
    payload = [boost_to_dict(db, row) for row in rows]
    return {
        "simulation": pilotage.global_simulation(db),
        "settings": settings(db),
        "boosts": payload,
        "current": next((item for item in payload if item["status"] in ("running", "stopping")), None),
    }


@router.post("/boosts/preview")
def preview_boost(payload: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    """Écran de validation obligatoire : devices retirés, hashrate perdu, coût."""
    try:
        return RUN.preview(db, payload)
    except RUN.BoostSelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except B.BoostError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/boosts", status_code=201)
async def create_boost(payload: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    """Crée **et démarre** le boost (l'écran de validation a été montré avant)."""
    payload = {**payload, "max_share_pct": payload.get("max_share_pct", settings(db)["max_share_pct"])}
    if payload.get("duration_h") in (None, ""):
        payload["duration_h"] = settings(db)["default_hours"]
    try:
        boost = RUN.create(db, payload)
    except RUN.BoostSelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except B.BoostError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    simulated = RUN.simulation_for(db, boost)
    try:
        result = await RUN.start_boost(db, boost, simulated=simulated)
    except Exception as exc:  # le boost reste lisible en échec
        boost.status = "failed"
        boost.note = str(exc)[:1000]
        db.commit()
        raise HTTPException(status_code=409, detail=f"boost non démarré : {exc}") from exc
    db.refresh(boost)
    _audit(db, _user, "boost-start", f"boost #{boost.id} {boost.coin}", boost.status, json.dumps(result)[:400])
    return {"boost": boost_to_dict(db, boost, events=True), "start": result}


@router.get("/boosts/{boost_id}")
def read_boost(boost_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    boost = db.get(Boost, boost_id)
    if boost is None:
        raise HTTPException(status_code=404, detail="boost introuvable")
    return boost_to_dict(db, boost, events=True)


@router.post("/boosts/{boost_id}/cancel")
async def cancel_boost(boost_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    """Annulation en un clic : arrêt et retour vérifié au token par défaut."""
    boost = db.get(Boost, boost_id)
    if boost is None:
        raise HTTPException(status_code=404, detail="boost introuvable")
    if boost.status in ("proposed", "approved"):
        B.apply_transition(boost, "cancelled")
        boost.ended_at = C.iso(C.now_utc())
        db.commit()
        RUN.log_event(db, boost, "cancelled", f"boost annulé avant démarrage par {_user}")
        db.commit()
        _audit(db, _user, "boost-cancel", f"boost #{boost.id}", "annulé")
        return boost_to_dict(db, boost, events=True)
    if boost.status not in ("running", "stopping"):
        raise HTTPException(
            status_code=409,
            detail=f"Boost {boost.status} : annulation impossible",
        )
    simulated = RUN.simulation_for(db, boost)
    result = await RUN.stop_boost(db, boost, simulated=simulated)
    db.refresh(boost)
    _audit(db, _user, "boost-cancel", f"boost #{boost.id}", boost.status, json.dumps(result)[:400])
    return {"boost": boost_to_dict(db, boost, events=True), "stop": result}
