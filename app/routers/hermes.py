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
from app import criteria as CRIT
from app import rules as R
from app import veille
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
    criteria_rule = next((rule for rule in rules if rule.id == "criteria"), None)
    criteria = CRIT.criteria_block(db, criteria_rule.params if criteria_rule else None)

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
        # Profils utilisables pour une proposition : jamais le wallet, jamais
        # l'URL du binaire ni la cmdline — seulement de quoi choisir un profil.
        "profiles": [
            {
                "id": profile.id,
                "name": profile.name,
                "coin": profile.coin,
                "software": profile.software,
                "execution_mode": profile.execution_mode,
                "active": bool(profile.enabled),
            }
            for profile in db.query(MinerProfile).order_by(MinerProfile.name).limit(200).all()
        ],
        "capacity": R.free_capacity(db),
        # Critères qualitatifs de Fred : la veille lit CE bloc, pas un skill.
        "criteria": criteria,
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
        entry = A.normalize_analysis(payload, strict=True)
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
    # Compteur du tick : seule une analyse réellement reçue (2xx) compte.
    veille.note_analysis(len(entry.get("candidates") or []))
    return {"created": bool(created), "duplicate": not created, "analysis": A.analysis_to_dict(row)}


@router.post("/proposals", status_code=201)
async def post_proposal(
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
    veille.note_proposal()
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
    from app import campaigns as C
    from app import pilotage as PIL
    from app.models import Machine

    kinds = {device.kind for device in devices}
    kind = "cpu" if kinds == {"cpu"} else ("gpu" if kinds == {"gpu"} else "")
    machines = {machine.id: machine for machine in db.query(Machine).all()}
    # « simulation désactivée » = machine armée (verrou local ou global levé).
    # Une machine absente de la base ne lance jamais rien.
    simulated = any(
        machines.get(device.machine_id) is None
        or PIL.simulated_for(db, machines[device.machine_id])
        for device in devices
    )
    plan = R.plan_auto_launch(db, kind, simulated)
    started = False
    reason = plan["reason"]
    if plan["launch"]:
        C.apply_transition(campaign, "approved")
        RUN.log_event(
            db,
            campaign,
            "approved",
            "approbation automatique (règle auto_launch)",
            actor="hermes",
        )
        db.commit()
        from app.routers.campaigns import start_campaign

        try:
            await start_campaign(campaign.id, db, "hermes")
            db.refresh(campaign)
            started = campaign.status == "running"
        except HTTPException as exc:
            reason = f"lancement refusé : {exc.detail}"
        db.refresh(campaign)
    elif plan["allowed"]:
        RUN.log_event(
            db,
            campaign,
            "auto_launch_simulated",
            "règle auto_launch active, machine en simulation : lancement simulé",
            actor="hermes",
            simulated=True,
        )
        db.commit()
    # §10.3 : Hermes ne fait que **proposer** un boost ; la règle décide.
    boost_brief = None
    if payload.boost:
        from app import boost as B
        from app import boost_runner as BR

        boost_payload = dict(payload.boost)
        boost_payload.setdefault("campaign_id", campaign.id)
        boost_payload.setdefault("coin", campaign.coin)
        facts = dict(payload.candidate.model_dump() if payload.candidate else {})
        facts.update(payload.boost.get("facts") or {})
        plan_boost = R.plan_boost(db, facts, simulated)
        try:
            boost_row = BR.create(db, boost_payload, origin="hermes")
        except B.BoostError as exc:
            boost_brief = {"created": False, "reason": f"boost non proposé : {exc}"}
        else:
            BR.log_event(db, boost_row, "proposal", f"proposition Hermes : boost {boost_row.coin}")
            db.commit()
            if plan_boost["launch"] and not simulated:
                await BR.start_boost(db, boost_row, simulated=False)
            db.refresh(boost_row)
            boost_brief = {
                "created": True,
                "id": boost_row.id,
                "status": boost_row.status,
                "coin": boost_row.coin,
                "rule": plan_boost,
            }
    message = "Proposition déposée : elle attend l'approbation de Fred."
    if started:
        message = "Campagne lancée automatiquement (règle auto_launch)."
    elif plan["allowed"]:
        message = "Règle auto_launch active : lancement simulé, la campagne reste proposée."
    return {
        "campaign": _campaign_brief(db, campaign),
        "status": campaign.status,
        "auto_launch": bool(started),
        "auto_launch_detail": {**plan, "started": started, "kind": kind, "simulated": bool(simulated), "reason": reason},
        "boost": boost_brief,
        "stage": "4b",
        "message": message,
    }
