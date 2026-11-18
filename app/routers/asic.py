"""ASIC (§4.20) — ajout par IP, sonde en lecture seule, **aucun pilotage**.

Un ASIC est une ``Machine`` de type ``asic`` : pas de SSH, ``usage=none`` pour
que la boucle SSH l'ignore, et son état vit dans ``machine_status.asic_json``.
Les routes n'exposent que des lectures (état, santé) ; aucun redémarrage, aucun
changement de pool.

Depuis la 2026.11.008, le firmware IceRiver ``ICM168_3_2_14`` sert son panneau
en HTTPS avec un certificat auto-signé et exige une connexion.  Les identifiants
sont optionnels à l'ajout, chiffrés en base comme les autres secrets (jamais
renvoyés, journalisés ni exportés), et l'empreinte du certificat est épinglée
puis affichée ; elle est refusée si elle change, avec une action explicite de
ré-épinglage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import asic, asic_client
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Machine, MachineStatus, UsageEnum

router = APIRouter(prefix="/api/asics", tags=["asics"])

LOGIN_REQUIRED = (
    "identifiants requis : le panneau IceRiver est servi en HTTPS et exige une connexion"
)


class AsicIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1, max_length=255)
    driver: str = "auto"
    model: str = ""
    expected_hashrate_hs: float | None = None
    power_w: float | None = None
    port: int | None = None
    #: Connexion au panneau (facultative : les anciens firmwares n'en veulent pas).
    username: str = ""
    password: str = ""


class AsicPatch(BaseModel):
    """Édition d'un ASIC : tout champ absent est inchangé, ``password`` vide aussi."""

    name: str | None = None
    host: str | None = None
    username: str | None = None
    password: str | None = None
    clear_password: bool = False
    model: str | None = None
    expected_hashrate_hs: float | None = None
    power_w: float | None = None


def _status_row(db: Session, machine: Machine) -> MachineStatus | None:
    return db.query(MachineStatus).filter(MachineStatus.machine_uuid == machine.uuid).first()


def state_of(db: Session, machine: Machine) -> dict | None:
    row = _status_row(db, machine)
    if not row or not row.asic_json:
        return None
    try:
        return json.loads(row.asic_json)
    except ValueError:
        return None


def _encrypt(password: str) -> str:
    from app.secrets import encrypt_secret

    return encrypt_secret(password)


def password_of(machine: Machine) -> str:
    """Mot de passe en clair **pour la seule connexion**, jamais renvoyé par l'API."""
    reference = getattr(machine, "asic_password_ref", None)
    if not reference:
        return ""
    try:
        from app.secrets import decrypt_secret

        return decrypt_secret(reference)
    except Exception:
        return ""


def public(machine: Machine, state: dict | None, thresholds: dict | None = None) -> dict:
    state = state or {}
    if state.get("power_w") is not None:
        source = "mesurée par l'ASIC"
    elif machine.asic_power_w:
        source = "saisie"
    else:
        source = "non mesurée"
    return {
        "id": machine.id,
        "uuid": machine.uuid,
        "name": machine.name,
        "host": machine.host,
        "driver": machine.asic_driver or "",
        "model": machine.asic_model or "",
        "expected_hashrate_hs": machine.asic_expected_hashrate_hs,
        "power_w": machine.asic_power_w,
        "read_only": True,
        # Connexion : l'identifiant est visible, le mot de passe jamais.
        "username": machine.asic_user or "",
        "has_password": bool(getattr(machine, "asic_password_ref", None)),
        "cert_sha256": machine.asic_cert_sha256 or "",
        "scheme": machine.asic_scheme or "http",
        "login_required": bool(state.get("requires_login")),
        "power_source": source,
        "asic": state or None,
        "health": asic.health(state or None, thresholds),
    }


def _store_state(db: Session, machine: Machine, state: dict) -> None:
    row = _status_row(db, machine)
    stamp = datetime.now(timezone.utc).isoformat()
    if row is None:
        row = MachineStatus(machine_uuid=machine.uuid, ts=stamp)
        db.add(row)
    row.ts = stamp
    row.online = bool(state.get("online"))
    row.error = state.get("error")
    row.miner = str(state.get("model") or "")
    row.algo = str(state.get("algorithm") or "")
    row.coin = str(state.get("coin") or "")
    row.total_hashrate_hs = state.get("hashrate_hs")
    power = state.get("power_w")
    row.total_power_w = power if power is not None else machine.asic_power_w
    uptime = state.get("uptime_s")
    row.uptime_s = int(uptime) if isinstance(uptime, (int, float)) else None
    accepted = state.get("shares_accepted")
    row.accepted = int(accepted) if isinstance(accepted, (int, float)) else None
    rejected = state.get("shares_rejected")
    row.rejected = int(rejected) if isinstance(rejected, (int, float)) else None
    row.asic_json = json.dumps(state, ensure_ascii=False)


def _error_state(machine: Machine, driver: str, exc: Exception) -> dict:
    """État écrit quand la sonde échoue : la raison reste lisible dans l'UI."""
    message = str(exc)
    base = {
        "driver": driver,
        "model": machine.asic_model or "",
        "firmware": "",
        "power_w": None,
        "hashrate_hs": None,
        "algorithm": asic.DRIVER_COIN.get(driver, {}).get("algo", ""),
        "coin": asic.DRIVER_COIN.get(driver, {}).get("coin", ""),
        "read_only": True,
        "error": message,
    }
    if isinstance(exc, asic_client.AsicCertError):
        return {**base, "online": True, "cert_changed": True}
    if isinstance(exc, asic_client.AsicAuthError):
        return {**base, "online": True, "requires_login": True}
    return {**base, "online": False}


async def probe_machine(machine: Machine, db: Session) -> dict:
    """Interroge l'ASIC et met à jour son état (lecture seule)."""
    driver = machine.asic_driver or "axeos"
    try:
        state = await asic_client.poll(
            driver,
            machine.host,
            username=machine.asic_user or "",
            password=password_of(machine),
            cert_sha256=machine.asic_cert_sha256 or None,
        )
    except asic_client.AsicError as exc:
        state = _error_state(machine, driver, exc)
        _store_state(db, machine, state)
        return state
    _store_state(db, machine, state)
    return state


async def _probe_for_add(
    driver: str, host: str, username: str, password: str
) -> tuple[str, dict, str | None, bool]:
    """Rend (pilote, état, empreinte du certificat, https)."""
    if driver == "iceriver":
        found = await asic_client.iceriver_probe(host, username, password)
        if found["payload"] is None:
            state = {
                "driver": "iceriver",
                "online": True,
                "requires_login": True,
                "model": "",
                "firmware": "",
                "hashrate_hs": None,
                "power_w": None,
                "algorithm": "kHeavyHash",
                "coin": "KAS",
                "read_only": True,
                "format_verified": True,
                "cert_sha256": found.get("cert_sha256") or "",
                "error": LOGIN_REQUIRED,
            }
        else:
            state = asic.parse_iceriver(found["payload"])
        return "iceriver", state, found.get("cert_sha256"), bool(found.get("https"))
    if driver == "axeos":
        return "axeos", asic.parse_axeos(await asic_client.fetch_axeos(host)), None, False
    if driver == "cgminer":
        payload = await asic_client.fetch_cgminer(host, asic_client.CGMINER_PORT)
        return "cgminer", asic.parse_cgminer(payload, payload, payload), None, False
    raise asic_client.AsicError(f"pilote inconnu : {driver!r}")


@router.get("")
def list_asics(db: Session = Depends(get_db), _user=Depends(require_login)):
    machines = (
        db.query(Machine)
        .filter(Machine.machine_kind == "asic")
        .order_by(Machine.name.asc())
        .all()
    )
    return {"asics": [public(machine, state_of(db, machine)) for machine in machines]}


@router.post("", status_code=201)
async def add_asic(item: AsicIn, db: Session = Depends(get_db), user=Depends(require_login)):
    """Ajout par IP : sonde en lecture seule puis enregistrement (sans SSH)."""
    try:
        host = asic_client.clean_host(item.host)
    except asic_client.AsicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    driver = (item.driver or "auto").strip().lower()
    if driver not in ("auto", *asic.DRIVERS):
        raise HTTPException(status_code=400, detail=f"Pilote inconnu : {driver!r}")
    username, password = item.username.strip(), item.password
    https = False
    try:
        if driver == "auto":
            found = await asic_client.detect(host, username=username, password=password)
            driver = found["driver"]
            state = found["state"]
            cert_sha256 = found.get("cert_sha256")
            https = bool(found.get("https"))
        else:
            driver, state, cert_sha256, https = await _probe_for_add(
                driver, host, username, password
            )
    except asic_client.AsicError as exc:
        raise HTTPException(status_code=400, detail=f"sonde en lecture seule : {exc}")
    machine = Machine(
        name=item.name.strip(),
        host=host,
        machine_kind="asic",
        ssh_port=0,
        ssh_user="",
        usage=UsageEnum.none,
        enabled=True,
        asic_driver=driver,
        asic_model=(item.model or state.get("model") or "")[:80],
        asic_expected_hashrate_hs=item.expected_hashrate_hs or state.get("expected_hashrate_hs"),
        asic_power_w=item.power_w,
        asic_user=username or None,
        asic_password_ref=_encrypt(password) if password else None,
        asic_cert_sha256=cert_sha256 or None,
        asic_scheme="https" if https else "http",
    )
    db.add(machine)
    db.flush()
    _store_state(db, machine, state)
    db.add(
        AuditLog(
            actor=user,
            action="asic-add",
            object_ref=f"asic:{host}"[:255],
            result="ok",
            output=json.dumps(
                {
                    "name": machine.name,
                    "host": host,
                    "driver": driver,
                    # Jamais le mot de passe, jamais l'identifiant en clair.
                    "login": bool(username),
                    "cert_sha256": cert_sha256 or "",
                },
                ensure_ascii=False,
            )[:500],
        )
    )
    db.commit()
    db.refresh(machine)
    return public(machine, state_of(db, machine))


@router.patch("/{machine_id}")
async def update_asic(
    machine_id: int,
    item: AsicPatch,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Édition : identifiant et mot de passe facultatifs, champ vide = inchangé."""
    machine = (
        db.query(Machine)
        .filter(Machine.id == machine_id, Machine.machine_kind == "asic")
        .first()
    )
    if not machine:
        raise HTTPException(status_code=404, detail="ASIC inconnu")
    if item.name is not None and item.name.strip():
        machine.name = item.name.strip()[:100]
    if item.host is not None and item.host.strip():
        try:
            host = asic_client.clean_host(item.host)
        except asic_client.AsicError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if host != machine.host:
            machine.host = host
            # L'empreinte épinglée vaut pour l'ancienne adresse : elle est
            # recalculée à la prochaine sonde, puis affichée.
            machine.asic_cert_sha256 = None
            machine.asic_scheme = "http"
    if item.username is not None:
        machine.asic_user = item.username.strip() or None
    if item.clear_password:
        machine.asic_password_ref = None
    elif item.password:
        machine.asic_password_ref = _encrypt(item.password)
    if item.model is not None:
        machine.asic_model = item.model.strip()[:80]
    if item.expected_hashrate_hs is not None:
        machine.asic_expected_hashrate_hs = item.expected_hashrate_hs
    if item.power_w is not None:
        machine.asic_power_w = item.power_w
    db.add(
        AuditLog(
            actor=user,
            action="asic-update",
            object_ref=f"asic:{machine.host}"[:255],
            result="ok",
            output=json.dumps(
                {
                    "name": machine.name,
                    "login": bool(machine.asic_user),
                    "password_set": bool(machine.asic_password_ref),
                },
                ensure_ascii=False,
            )[:500],
        )
    )
    db.commit()
    db.refresh(machine)
    return public(machine, state_of(db, machine))


@router.post("/{machine_id}/repin")
async def repin_certificate(
    machine_id: int,
    data: dict = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Ré-épingle le certificat présenté, sur confirmation explicite."""
    if not data.get("confirm"):
        raise HTTPException(status_code=400, detail="confirmation requise pour ré-épingler")
    machine = (
        db.query(Machine)
        .filter(Machine.id == machine_id, Machine.machine_kind == "asic")
        .first()
    )
    if not machine:
        raise HTTPException(status_code=404, detail="ASIC inconnu")
    hostname, port = asic_client.split_host(machine.host)
    try:
        digest = await asic_client.certificate_sha256(hostname, port or 443)
    except asic_client.AsicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    machine.asic_cert_sha256 = digest
    machine.asic_scheme = "https"
    db.add(
        AuditLog(
            actor=user,
            action="asic-repin",
            object_ref=f"asic:{machine.host}"[:255],
            result="ok",
            output=json.dumps({"cert_sha256": digest})[:500],
        )
    )
    db.commit()
    db.refresh(machine)
    return {"ok": True, "cert_sha256": digest, "asic": public(machine, state_of(db, machine))}


@router.post("/{machine_id}/probe")
async def probe(machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    machine = db.query(Machine).filter(Machine.id == machine_id, Machine.machine_kind == "asic").first()
    if not machine:
        raise HTTPException(status_code=404, detail="ASIC inconnu")
    state = await probe_machine(machine, db)
    db.commit()
    db.refresh(machine)
    return public(machine, state_of(db, machine))


@router.delete("/{machine_id}")
def delete_asic(machine_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    machine = db.query(Machine).filter(Machine.id == machine_id, Machine.machine_kind == "asic").first()
    if not machine:
        raise HTTPException(status_code=404, detail="ASIC inconnu")
    row = _status_row(db, machine)
    if row:
        db.delete(row)
    db.delete(machine)
    db.add(AuditLog(actor=user, action="asic-delete", object_ref=f"asic:{machine.name}"[:255], result="ok"))
    db.commit()
    return {"ok": True}
