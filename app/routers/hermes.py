"""API Hermes (§5.4) — jeton dédié, droits limités.

Lire : règles actives, campagnes, catalogue, capacité libre CPU/GPU.
Écrire : analyses et **propositions** de campagne (statut ``proposed``).

Jamais de wallet complet, de mot de passe, d'IP de machine, de commande SSH ni
de lancement : une proposition attend l'approbation de Fred, et la règle
« lancement auto » reste à l'étape 4b (aucun lancement par l'API).
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import analyses as A
from app import campaign_runner as RUN
from app import rules as R
from app.database import get_db
from app.dependencies import require_hermes_token
from app.models import (
    Analysis,
    AuditLog,
    Campaign,
    CampaignDevice,
    Coin,
    Device,
    Machine,
    MinerProfile,
    Rule,
)

router = APIRouter(prefix="/api/hermes", tags=["hermes"])


def _audit(db: Session, action: str, ref: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor="hermes",
            action=action[:50],
            object_ref=ref[:255],
            result=result[:200],
            output=output[:500],
        )
    )
    db.commit()


def _device_label(device: Device) -> str:
    parts = [part for part in (device.vendor, device.model) if part]
    return " ".join(parts) or f"{device.kind} #{device.index if device.index is not None else '?'}"


def _machine_name(db: Session, machine_id: int) -> str:
    machine = db.get(Machine, machine_id)
    return machine.name if machine else ""


def _campaign_brief(db: Session, campaign: Campaign) -> dict:
    """Vue campagne **sans** IP, sans secret, sans commande."""
    profile = db.get(MinerProfile, campaign.profile_id)
    rows = db.query(CampaignDevice).filter(CampaignDevice.campaign_id == campaign.id).all()
    devices = [
        {
            "device_uuid": row.device_uuid,
            "kind": row.kind,
            "label": row.label,
            "machine": _machine_name(db, row.machine_id),
        }
        for row in rows
    ]
    return {
        "id": campaign.id,
        "coin": campaign.coin,
        "profile": profile.name if profile else None,
        "status": campaign.status,
        "origin": campaign.origin,
        "execution_mode": campaign.execution_mode,
        "start_at": campaign.start_at,
        "end_at": campaign.end_at,
        "analysis_id": campaign.analysis_id,
        "devices": devices,
        "note": campaign.note or "",
    }


@router.get("/ping")
async def ping(actor: str = Depends(require_hermes_token)):
    """200 with a valid token, 401 otherwise."""
    return {"ok": True, "actor": actor, "service": "fossor"}


@router.get("/context")
def context(db: Session = Depends(get_db), _actor: str = Depends(require_hermes_token)):
    """Contexte de veille : règles actives, campagnes, catalogue, capacité, dernière analyse."""
    rules = db.query(Rule).order_by(Rule.sort_order, Rule.id).all()
    active = [rule for rule in rules if rule.enabled]
    auto = next((rule for rule in rules if rule.id == "auto_launch"), None)

    running = (
        db.query(Campaign)
        .filter(Campaign.status.in_(("running", "stopping")))
        .order_by(Campaign.id.desc())
        .all()
    )
    proposed = (
        db.query(Campaign)
        .filter(Campaign.status == "proposed")
        .order_by(Campaign.id.desc())
        .all()
    )
    catalogue = db.query(Coin).order_by(Coin.ticker).limit(500).all()
    last = db.query(Analysis).order_by(Analysis.ts.desc(), Analysis.id.desc()).first()

    return {
        "service": "fossor",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": [
            {"id": rule.id, "name": rule.name, "scope": rule.scope, "params": rule.params or {}}
            for rule in active
        ],
        "disabled_rules": [rule.id for rule in rules if not rule.enabled],
        "auto_launch": {
            "enabled": bool(auto.enabled) if auto is not None else False,
            "stage": "4b",
            "note": "Aucun lancement automatique dans cette étape : déposer une proposition.",
        },
        "campaigns": {
            "running": [_campaign_brief(db, campaign) for campaign in running],
            "proposed": [_campaign_brief(db, campaign) for campaign in proposed],
        },
        "catalogue": [
            {
                "ticker": coin.ticker,
                "name": coin.name,
                "algo": coin.algo,
                "kind": coin.kind,
                "links": [url for url in (coin.site_url, coin.ann_url, coin.explorer_url) if url],
            }
            for coin in catalogue
        ],
        "capacity": R.free_capacity(db),
        "last_analysis": A.analysis_to_dict(last) if last is not None else None,
        "limits": {
            "read": ["rules", "campaigns", "catalogue", "capacity", "last_analysis"],
            "write": ["analyses", "proposals"],
            "forbidden": ["wallets", "secrets", "machine_ips", "ssh", "launch"],
        },
    }


@router.post("/analyses")
def post_analysis(
    payload: dict, db: Session = Depends(get_db), _actor: str = Depends(require_hermes_token)
):
    """Enregistre une analyse ; rejouer la même ne crée pas de doublon."""
    try:
        entry = A.normalize_analysis(payload)
    except A.AnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    digest = A.fingerprint(entry)
    existing = db.query(Analysis).filter(Analysis.fingerprint == digest).first()
    if existing is not None:
        return {"created": False, "duplicate": True, "analysis": A.analysis_to_dict(existing)}
    created = A.store_entries(db, [entry], source="hermes")
    row = db.query(Analysis).filter(Analysis.fingerprint == digest).first()
    if row is None:  # course improbable entre deux appels identiques
        raise HTTPException(status_code=409, detail="Analyse concurrente : réessayer")
    _audit(db, "analyse Hermes", f"analyse #{row.id}", "ok", f"{created} création(s)")
    return {"created": bool(created), "duplicate": not created, "analysis": A.analysis_to_dict(row)}


@router.post("/proposals", status_code=201)
def post_proposal(
    payload: R.ProposalIn,
    db: Session = Depends(get_db),
    _actor: str = Depends(require_hermes_token),
):
    """Valide une proposition contre les règles, puis la dépose en ``proposed``."""
    verdict = R.validate_proposal(db, payload)
    if not verdict["ok"]:
        reasons = verdict["reasons"]
        _audit(
            db,
            "proposition Hermes refusée",
            f"{payload.coin} profil #{payload.profile_id}",
            "refusée",
            "; ".join(f"{item['code']}: {item['message']}" for item in reasons),
        )
        raise HTTPException(
            status_code=422,
            detail={"detail": "Proposition refusée", "reasons": reasons},
        )

    data = verdict["payload"]
    devices = (
        db.query(Device).filter(Device.uuid.in_(data["device_uuids"])).all()
        if data["device_uuids"]
        else []
    )
    campaign = Campaign(
        uuid=str(uuid_lib.uuid4()),
        coin=data["coin"],
        profile_id=data["profile_id"],
        start_at=data["start_at"],
        end_at=data["end_at"],
        status="proposed",
        origin="hermes",
        execution_mode=data["execution_mode"],
        note=data["note"] or None,
        analysis_id=data["analysis_id"],
    )
    db.add(campaign)
    db.flush()
    for device in devices:
        db.add(
            CampaignDevice(
                campaign_id=campaign.id,
                machine_id=device.machine_id,
                device_uuid=device.uuid,
                kind=device.kind,
                label=_device_label(device),
            )
        )
    db.commit()
    db.refresh(campaign)
    RUN.log_event(
        db,
        campaign,
        "proposal",
        f"proposition Hermes : {campaign.coin}, profil #{campaign.profile_id}, {len(devices)} device(s)",
        actor="hermes",
        detail={"analysis_id": campaign.analysis_id, "reasons": []},
    )
    db.commit()
    _audit(
        db,
        "proposition Hermes acceptée",
        f"campagne #{campaign.id} {campaign.coin}",
        "proposée",
        f"{len(devices)} device(s), mode {campaign.execution_mode}",
    )
    return {
        "campaign": _campaign_brief(db, campaign),
        "status": "proposed",
        "auto_launch": False,
        "stage": "4b",
        "message": "Proposition déposée : elle attend l'approbation de Fred.",
    }
