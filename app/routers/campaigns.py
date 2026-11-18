"""API des campagnes de minage (§4.7) — CRUD, transitions, journal.

Chaque transition passe par la machine à états de :mod:`app.campaigns` : un
`done` ne redevient pas `running`, une transition interdite répond 409 avec le
motif.  Tout est audité (acteur, action, résultat) et journalisé dans
``campaign_events``, y compris le caractère simulé.
"""

from __future__ import annotations

import json
import uuid as uuid_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import campaigns as C
from app import campaign_runner as RUN
from app.database import get_db
from app.dependencies import require_login
from app.models import (
    ActorEnum,
    AuditLog,
    Campaign,
    CampaignDevice,
    CampaignEvent,
    Device,
    Machine,
    MachineStatus,
    MinerProfile,
)

router = APIRouter(prefix="/api", tags=["campaigns"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(db: Session, actor: str, action: str, ref: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor=(actor or ActorEnum.fred)[:20],
            action=action,
            object_ref=ref[:255],
            result=result,
            output=output[:500],
        )
    )
    db.commit()


def _get(db: Session, campaign_id: int) -> Campaign:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campagne introuvable")
    return campaign


def _devices(db: Session, campaign_id: int) -> list[dict]:
    rows = (
        db.query(CampaignDevice, Machine)
        .join(Machine, Machine.id == CampaignDevice.machine_id)
        .filter(CampaignDevice.campaign_id == campaign_id)
        .order_by(CampaignDevice.id.asc())
        .all()
    )
    return [
        {
            "machine_id": device.machine_id,
            "machine_name": machine.name,
            "device_uuid": device.device_uuid,
            "kind": device.kind,
            "label": device.label,
        }
        for device, machine in rows
    ]


def campaign_to_dict(db: Session, campaign: Campaign) -> dict:
    devices = _devices(db, campaign.id)
    profile = db.query(MinerProfile).filter(MinerProfile.id == campaign.profile_id).first()
    hashrate = None
    live_power = None
    machine_ids = sorted({row["machine_id"] for row in devices})
    if campaign.status in ("running", "stopping") and machine_ids:
        machines = db.query(Machine).filter(Machine.id.in_(machine_ids)).all()
        total = 0.0
        seen = False
        for machine in machines:
            row = (
                db.query(MachineStatus)
                .filter(MachineStatus.machine_uuid == machine.uuid)
                .first()
            )
            if row is not None and row.total_hashrate_hs:
                total += float(row.total_hashrate_hs)
                seen = True
        hashrate = total if seen else None

    cost_running = None
    started = C.parse_iso(campaign.started_at)
    if campaign.status == "running" and started is not None:
        hours = C.hours_between(started, C.now_utc())
        power = campaign.power_w
        if power is None:
            power = live_power
        if power is None:
            power = RUN.wall_power(db, campaign, RUN.campaign_targets(db, campaign), started, C.now_utc())
        tariff = campaign.tariff_eur_kwh
        cost_running = C.campaign_cost_eur(power, hours, tariff) if power is not None else None

    return {
        "id": campaign.id,
        "uuid": campaign.uuid,
        "coin": campaign.coin,
        "profile_id": campaign.profile_id,
        "profile_name": profile.name if profile else None,
        "start_at": campaign.start_at,
        "end_at": campaign.end_at,
        "status": campaign.status,
        "status_label": C.STATUS_LABELS.get(campaign.status, campaign.status),
        "origin": campaign.origin,
        "devices": devices,
        "started_at": campaign.started_at,
        "ended_at": campaign.ended_at,
        "start_balance": campaign.start_balance,
        "end_balance": campaign.end_balance,
        "balance_unit": campaign.balance_unit,
        "balance_source": campaign.balance_source,
        "gain": campaign.gain,
        "power_w": campaign.power_w,
        "tariff_eur_kwh": campaign.tariff_eur_kwh,
        "cost_eur": campaign.cost_eur,
        "cost_running_eur": cost_running,
        "hashrate_hs": hashrate,
        "verified": campaign.verified,
        "attempts": campaign.attempts,
        "stop_requested": bool(campaign.stop_requested),
        "confirm_overload": bool(campaign.confirm_overload),
        "note": campaign.note,
        "created_at": campaign.created_at,
        "updated_at": campaign.updated_at,
        "allowed": sorted(C.next_statuses(campaign.status)),
    }


def events_to_list(db: Session, campaign_id: int) -> list[dict]:
    rows = (
        db.query(CampaignEvent)
        .filter(CampaignEvent.campaign_id == campaign_id)
        .order_by(CampaignEvent.id.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "ts": row.ts,
            "actor": row.actor,
            "kind": row.kind,
            "message": row.message,
            "result": row.result,
            "simulated": bool(row.simulated),
            "detail": row.detail,
        }
        for row in rows
    ]


# ── Lecture ───────────────────────────────────────────────────────────
@router.get("/campaigns")
def list_campaigns(
    status: str | None = None, db: Session = Depends(get_db), _user=Depends(require_login)
):
    query = db.query(Campaign).order_by(Campaign.id.desc())
    if status:
        query = query.filter(Campaign.status == status)
    rows = query.all()
    payload = [campaign_to_dict(db, row) for row in rows]
    current = next(
        (item for item in payload if item["status"] in ("running", "stopping")), None
    )
    return {
        "simulation": RUN.simulation_enabled(db),
        "restore_timeout_s": RUN.restore_timeout(db),
        "campaigns": payload,
        "current": current,
    }


@router.get("/campaigns/options")
def campaign_options(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Machines et devices proposables à la création (GPU intégrés masqués)."""
    from app.devices import integrated_hidden
    from app.routers.miners import default_profile_id

    hide = integrated_hidden(db)
    machines = db.query(Machine).filter(Machine.enabled == True).order_by(Machine.name.asc()).all()  # noqa: E712
    payload = []
    for machine in machines:
        devices = (
            db.query(Device)
            .filter(Device.machine_id == machine.id, Device.present == True)  # noqa: E712
            .order_by(Device.kind.desc(), Device.index.asc())
            .all()
        )
        rows = []
        for device in devices:
            if device.kind == "gpu" and device.integrated and hide:
                continue
            label = (
                f"CPU {device.model or ''}".strip()
                if device.kind == "cpu"
                else f"GPU {device.index if device.index is not None else '?'} — {device.model or ''}".strip()
            )
            rows.append(
                {
                    "uuid": device.uuid,
                    "kind": device.kind,
                    "index": device.index,
                    "label": label,
                    "enabled": bool(device.enabled_for_mining),
                    "occupied_external": bool(device.occupied_external),
                    "disabled_reason": (
                        "occupé par un mineur externe (hors Fossor)"
                        if device.occupied_external
                        else ("" if device.enabled_for_mining else "désactivé pour le minage")
                    ),
                }
            )
        payload.append(
            {
                "id": machine.id,
                "name": machine.name,
                "pilotable": machine.auth_type == "fossor_key",
                "default_profile_id": machine.default_miner_profile_id or default_profile_id(db),
                "devices": rows,
            }
        )
    profiles = (
        db.query(MinerProfile).filter(MinerProfile.enabled == True).order_by(MinerProfile.name.asc()).all()  # noqa: E712
    )
    return {
        "simulation": RUN.simulation_enabled(db),
        "machines": payload,
        "profiles": [
            {
                "id": profile.id,
                "name": profile.name,
                "coin": profile.coin,
                "software": profile.software,
                "execution_mode": profile.execution_mode,
                "sha256": profile.sha256,
            }
            for profile in profiles
        ],
    }


@router.get("/campaigns/{campaign_id}")
def read_campaign(campaign_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    campaign = _get(db, campaign_id)
    payload = campaign_to_dict(db, campaign)
    payload["events"] = events_to_list(db, campaign.id)
    return payload


@router.get("/campaigns/{campaign_id}/events")
def read_events(campaign_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    campaign = _get(db, campaign_id)
    return events_to_list(db, campaign.id)


# ── Création / modification ───────────────────────────────────────────
def _clean_devices(db: Session, devices: list[dict]) -> list[dict]:
    if not devices:
        raise HTTPException(status_code=400, detail="Au moins un device est requis")
    clean: list[dict] = []
    seen: set[str] = set()
    for item in devices:
        machine_id = item.get("machine_id")
        device_uuid = (item.get("device_uuid") or "").strip() or None
        if not machine_id:
            raise HTTPException(status_code=400, detail="Device sans machine")
        machine = db.query(Machine).filter(Machine.id == int(machine_id)).first()
        if machine is None:
            raise HTTPException(status_code=400, detail=f"Machine {machine_id} introuvable")
        device = None
        if device_uuid:
            device = db.query(Device).filter(Device.uuid == device_uuid).first()
            if device is None or device.machine_id != machine.id:
                raise HTTPException(status_code=400, detail=f"Device {device_uuid} absent de {machine.name}")
            if device.occupied_external:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{machine.name} : {device.model or device_uuid} est tenu par un mineur externe — "
                        "Fossor ne le pilote pas (cohabitation HiveOS à l'étape 3e)"
                    ),
                )
            label = (
                f"CPU {device.model or ''}".strip()
                if device.kind == "cpu"
                else f"GPU {device.index if device.index is not None else '?'} — {device.model or ''}".strip()
            )
        else:
            label = machine.name
        key = device_uuid or f"machine:{machine.id}"
        if key in seen:
            continue
        seen.add(key)
        clean.append(
            {
                "machine_id": machine.id,
                "device_uuid": device_uuid,
                "kind": (device.kind if device else "machine"),
                "label": label,
            }
        )
    return clean


@router.post("/campaigns", status_code=201)
def create_campaign(data: dict, db: Session = Depends(get_db), _user=Depends(require_login)):
    profile = db.query(MinerProfile).filter(MinerProfile.id == data.get("profile_id")).first()
    if profile is None:
        raise HTTPException(status_code=400, detail="Profil de minage introuvable")
    coin = str(data.get("coin") or profile.coin).strip().upper()
    if not coin or len(coin) > 20 or not coin.isalnum():
        raise HTTPException(status_code=400, detail="coin : 2 à 20 caractères alphanumériques")
    try:
        start_at, end_at = C.validate_window(data.get("start_at"), data.get("end_at"))
    except C.CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    devices = _clean_devices(db, data.get("devices") or [])
    campaign = Campaign(
        uuid=str(uuid_lib.uuid4()),
        coin=coin,
        profile_id=profile.id,
        start_at=start_at,
        end_at=end_at,
        status="proposed",
        origin=str(data.get("origin") or "manual"),
        confirm_overload=bool(data.get("confirm_overload")),
        note=str(data.get("note") or "") or None,
    )
    db.add(campaign)
    db.flush()
    for item in devices:
        db.add(CampaignDevice(campaign_id=campaign.id, **item))
    db.commit()
    db.refresh(campaign)
    RUN.log_event(
        db,
        campaign,
        "created",
        f"campagne proposée : {coin}, profil « {profile.name} », {len(devices)} device(s)",
        actor=_user,
        detail={"start_at": start_at, "end_at": end_at},
    )
    db.commit()
    _audit(db, _user, "campaign-create", f"campagne #{campaign.id} {coin}", "ok", profile.name)
    return campaign_to_dict(db, campaign)


@router.patch("/campaigns/{campaign_id}")
def update_campaign(
    campaign_id: int, data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    campaign = _get(db, campaign_id)
    if campaign.status != "proposed":
        raise HTTPException(status_code=409, detail="Seule une campagne proposée se modifie")
    changed: list[str] = []
    if "profile_id" in data:
        profile = db.query(MinerProfile).filter(MinerProfile.id == data.get("profile_id")).first()
        if profile is None:
            raise HTTPException(status_code=400, detail="Profil de minage introuvable")
        campaign.profile_id = profile.id
        changed.append("profil")
    if "coin" in data:
        coin = str(data.get("coin") or "").strip().upper()
        if not coin.isalnum() or not 2 <= len(coin) <= 20:
            raise HTTPException(status_code=400, detail="coin invalide")
        campaign.coin = coin
        changed.append("coin")
    if "start_at" in data or "end_at" in data:
        try:
            start_at, end_at = C.validate_window(
                data.get("start_at", campaign.start_at), data.get("end_at", campaign.end_at)
            )
        except C.CampaignError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        campaign.start_at, campaign.end_at = start_at, end_at
        changed.append("fenêtre")
    if "devices" in data:
        devices = _clean_devices(db, data.get("devices") or [])
        db.query(CampaignDevice).filter(CampaignDevice.campaign_id == campaign.id).delete()
        for item in devices:
            db.add(CampaignDevice(campaign_id=campaign.id, **item))
        changed.append(f"{len(devices)} device(s)")
    if "confirm_overload" in data:
        campaign.confirm_overload = bool(data["confirm_overload"])
        changed.append("confirmation PSU")
    campaign.updated_at = _now()
    db.commit()
    RUN.log_event(db, campaign, "updated", "modification : " + ", ".join(changed or ["aucun champ"]), actor=_user)
    db.commit()
    _audit(db, _user, "campaign-update", f"campagne #{campaign.id}", "ok", ", ".join(changed))
    return campaign_to_dict(db, campaign)


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    campaign = _get(db, campaign_id)
    if campaign.status != "proposed":
        raise HTTPException(status_code=409, detail="Seule une campagne proposée se supprime")
    db.query(CampaignDevice).filter(CampaignDevice.campaign_id == campaign.id).delete()
    db.query(CampaignEvent).filter(CampaignEvent.campaign_id == campaign.id).delete()
    db.delete(campaign)
    db.commit()
    _audit(db, _user, "campaign-delete", f"campagne #{campaign_id}", "ok")
    return {"ok": True}


# ── Transitions ───────────────────────────────────────────────────────
def _transition(campaign: Campaign, target: str) -> None:
    try:
        C.ensure_transition(campaign.status, target)
    except C.CampaignError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/campaigns/{campaign_id}/approve")
def approve_campaign(
    campaign_id: int, data: dict | None = None, db: Session = Depends(get_db), _user=Depends(require_login)
):
    data = data or {}
    campaign = _get(db, campaign_id)
    _transition(campaign, "approved")
    if "confirm_overload" in data:
        campaign.confirm_overload = bool(data["confirm_overload"])
    C.apply_transition(campaign, "approved")
    campaign.updated_at = _now()
    db.commit()
    RUN.log_event(
        db,
        campaign,
        "approved",
        "campagne approuvée" + (" (charge PSU confirmée)" if campaign.confirm_overload else ""),
        actor=_user,
    )
    db.commit()
    _audit(db, _user, "campaign-approve", f"campagne #{campaign.id}", "ok")
    return campaign_to_dict(db, campaign)


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(
    campaign_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    """« Démarrer maintenant » : la fenêtre commence à l'instant, le tick agit."""
    campaign = _get(db, campaign_id)
    if campaign.status != "approved":
        raise HTTPException(
            status_code=409,
            detail="La campagne doit être approuvée avant de démarrer"
            if campaign.status == "proposed"
            else f"Campagne {campaign.status} : démarrage impossible",
        )
    campaign.start_at = C.iso(C.now_utc())
    if campaign.end_at is not None:
        end = C.parse_iso(campaign.end_at)
        if end is not None and end <= C.now_utc():
            raise HTTPException(status_code=409, detail="La fin de la campagne est déjà passée")
    campaign.updated_at = _now()
    db.commit()
    RUN.log_event(db, campaign, "start-requested", "démarrage demandé maintenant", actor=_user)
    db.commit()
    results = await RUN.tick(db)
    db.refresh(campaign)
    _audit(db, _user, "campaign-start-now", f"campagne #{campaign.id}", campaign.status, json.dumps(results)[:400])
    return {"campaign": campaign_to_dict(db, campaign), "actions": results}


@router.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(
    campaign_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    campaign = _get(db, campaign_id)
    if campaign.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"Campagne {campaign.status} : seul un arrêt d'une campagne en cours a un sens",
        )
    campaign.stop_requested = True
    campaign.updated_at = _now()
    db.commit()
    RUN.log_event(db, campaign, "stop-requested", "arrêt demandé à la main", actor=_user)
    db.commit()
    results = await RUN.tick(db)
    db.refresh(campaign)
    _audit(db, _user, "campaign-stop-now", f"campagne #{campaign.id}", campaign.status)
    return {"campaign": campaign_to_dict(db, campaign), "actions": results}


@router.post("/campaigns/{campaign_id}/cancel")
def cancel_campaign(
    campaign_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    campaign = _get(db, campaign_id)
    if campaign.status not in ("proposed", "approved"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Campagne en cours : utilisez « Arrêter » (le mineur doit être arrêté "
                "et le token par défaut restauré avant toute annulation)"
                if campaign.status in ("running", "stopping")
                else f"Campagne {campaign.status} : annulation impossible"
            ),
        )
    _transition(campaign, "cancelled")
    C.apply_transition(campaign, "cancelled")
    campaign.ended_at = _now()
    campaign.updated_at = _now()
    db.commit()
    RUN.log_event(db, campaign, "cancelled", "campagne annulée", actor=_user)
    db.commit()
    _audit(db, _user, "campaign-cancel", f"campagne #{campaign.id}", "ok")
    return campaign_to_dict(db, campaign)
