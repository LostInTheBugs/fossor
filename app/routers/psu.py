"""Alimentations, inventaire des composants et consommation (§4.2 / §4.2 bis).

Lecture seule côté machines : la détection passe par la commande en liste
blanche ``inventory`` (dmidecode/lsblk/lspci/HiveOS), jamais par une commande
ad hoc.  Tous les calculs sont dans :mod:`app.psu`.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai_config import get_setting, set_setting
from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, Device, Machine, MachineComponent, Metric, Psu
from app.psu import CERTIFICATIONS, PSU_PRESETS, compute_power, normalize_certification

router = APIRouter(prefix="/api", tags=["power"])

HIDE_INTEGRATED_KEY = "hide_integrated_gpus"
COMPONENT_KINDS = ("ram", "storage", "pcie_card", "fan", "riser", "pump", "motherboard", "other")
COMPONENT_LABELS = {
    "ram": "RAM", "storage": "Stockage", "pcie_card": "Carte PCIe",
    "fan": "Ventilateur", "riser": "Riser", "pump": "Pompe/AIO",
    "motherboard": "Carte mère", "other": "Autre",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _machine(db: Session, machine_id: int) -> Machine:
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine introuvable")
    return machine


def _psu_or_404(db: Session, psu_id: int) -> Psu:
    psu = db.query(Psu).filter(Psu.id == psu_id).first()
    if psu is None:
        raise HTTPException(status_code=404, detail="Alimentation introuvable")
    return psu


def psu_to_dict(psu: Psu) -> dict[str, Any]:
    return {
        "id": psu.id,
        "uuid": psu.uuid,
        "machine_id": psu.machine_id,
        "name": psu.name,
        "rated_w": psu.rated_w,
        "certification": psu.certification,
        "amps_12v": psu.amps_12v,
        "derate_pct": psu.derate_pct,
        "measured_efficiency": psu.measured_efficiency,
        "brand": psu.brand or "",
        "model": psu.model or "",
        "is_default": bool(psu.is_default),
    }


def component_to_dict(component: MachineComponent) -> dict[str, Any]:
    return {
        "id": component.id,
        "uuid": component.uuid,
        "machine_id": component.machine_id,
        "kind": component.kind,
        "kind_label": COMPONENT_LABELS.get(component.kind, component.kind),
        "label": component.label,
        "watts": component.watts,
        "quantity": component.quantity,
        "measured": bool(component.measured),
        "source": component.source,
        "psu_id": component.psu_id,
    }


def _validate_psu_body(db: Session, machine: Machine, data: dict, partial: bool = False) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if not partial or "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Nom de l'alimentation requis")
        values["name"] = name[:60]
    if not partial or "rated_w" in data:
        try:
            rated = float(data.get("rated_w"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Puissance nominale invalide")
        if not 1 <= rated <= 5000:
            raise HTTPException(status_code=400, detail="Puissance nominale hors limites (1–5000 W)")
        values["rated_w"] = rated
    certification = data.get("certification") if "certification" in data else ("none" if not partial else None)
    if certification is not None:
        try:
            values["certification"] = normalize_certification(certification)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    for field, low, high in (("amps_12v", 0.1, 300.0), ("measured_efficiency", 50.0, 100.0)):
        if field in data:
            if data[field] in (None, ""):
                values[field] = None
                continue
            try:
                value = float(data[field])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{field} invalide")
            if not low <= value <= high:
                raise HTTPException(status_code=400, detail=f"{field} hors limites")
            values[field] = value
    if "derate_pct" in data:
        try:
            derate = float(data["derate_pct"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Déclassement invalide")
        if not 1 <= derate <= 100:
            raise HTTPException(status_code=400, detail="Déclassement hors limites (1–100 %)")
        values["derate_pct"] = derate
    for field in ("brand", "model"):
        if field in data:
            values[field] = str(data.get(field) or "")[:80]
    if data.get("is_default"):
        db.query(Psu).filter(Psu.machine_id == machine.id).update({Psu.is_default: False})
        values["is_default"] = True
    elif "is_default" in data:
        values["is_default"] = False
    return values


# ── Alimentations ─────────────────────────────────────────────────────
@router.get("/machines/{machine_id}/psus")
def list_psus(machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    machine = _machine(db, machine_id)
    rows = db.query(Psu).filter(Psu.machine_id == machine.id).order_by(Psu.id.asc()).all()
    return [psu_to_dict(row) for row in rows]


@router.post("/machines/{machine_id}/psus", status_code=201)
def create_psu(
    machine_id: int, data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    machine = _machine(db, machine_id)
    values = _validate_psu_body(db, machine, data)
    existing = db.query(Psu).filter(Psu.machine_id == machine.id).count()
    psu = Psu(
        uuid=str(uuid_lib.uuid4()),
        machine_id=machine.id,
        created_at=_now(),
        updated_at=_now(),
        **{"is_default": existing == 0, **values},
    )
    db.add(psu)
    db.add(AuditLog(actor="fred", action="psu-create", machine_id=machine.id,
                    object_ref=values["name"], result="ok"))
    db.commit()
    db.refresh(psu)
    return psu_to_dict(psu)


@router.patch("/psus/{psu_id}")
def update_psu(
    psu_id: int, data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    psu = _psu_or_404(db, psu_id)
    machine = _machine(db, psu.machine_id)
    for key, value in _validate_psu_body(db, machine, data, partial=True).items():
        setattr(psu, key, value)
    psu.updated_at = _now()
    db.commit()
    db.refresh(psu)
    return psu_to_dict(psu)


@router.delete("/psus/{psu_id}")
def delete_psu(psu_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    psu = _psu_or_404(db, psu_id)
    db.query(Device).filter(Device.psu_id == psu.id).update({Device.psu_id: None})
    db.query(Device).filter(Device.psu2_id == psu.id).update({Device.psu2_id: None})
    db.query(MachineComponent).filter(MachineComponent.psu_id == psu.id).update(
        {MachineComponent.psu_id: None}
    )
    db.delete(psu)
    db.commit()
    return {"deleted": psu_id}


# ── Composants ────────────────────────────────────────────────────────
def ensure_default_components(db: Session, machine: Machine) -> None:
    """Create the spec defaults once (board, fans, risers, pump)."""
    from app.inventory import default_components

    existing = db.query(MachineComponent).filter(
        MachineComponent.machine_id == machine.id
    ).count()
    if existing:
        return
    for row in default_components():
        db.add(MachineComponent(
            uuid=str(uuid_lib.uuid4()), machine_id=machine.id,
            created_at=_now(), updated_at=_now(), **row,
        ))
    db.commit()


@router.get("/machines/{machine_id}/components")
def list_components(machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)):
    machine = _machine(db, machine_id)
    ensure_default_components(db, machine)
    rows = db.query(MachineComponent).filter(
        MachineComponent.machine_id == machine.id
    ).order_by(MachineComponent.kind.asc(), MachineComponent.id.asc()).all()
    return [component_to_dict(row) for row in rows]


def _component_values(db: Session, machine: Machine, data: dict) -> dict[str, Any]:
    kind = str(data.get("kind") or "").strip()
    if kind not in COMPONENT_KINDS:
        raise HTTPException(status_code=400, detail="Type de composant inconnu")
    label = str(data.get("label") or "").strip() or COMPONENT_LABELS[kind]
    try:
        watts = float(data.get("watts"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Puissance du composant invalide")
    if not 0 <= watts <= 2000:
        raise HTTPException(status_code=400, detail="Puissance du composant hors limites")
    try:
        quantity = int(data.get("quantity", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Quantité invalide")
    if not 0 <= quantity <= 64:
        raise HTTPException(status_code=400, detail="Quantité hors limites (0–64)")
    psu_id = data.get("psu_id")
    if psu_id is not None:
        psu = _psu_or_404(db, int(psu_id))
        if psu.machine_id != machine.id:
            raise HTTPException(status_code=400, detail="Cette alimentation n'appartient pas à la machine")
    return {
        "kind": kind, "label": label[:120], "watts": watts, "quantity": quantity,
        "measured": bool(data.get("measured")), "psu_id": psu_id,
        "source": str(data.get("source") or "manual")[:10],
    }


@router.post("/machines/{machine_id}/components", status_code=201)
def create_component(
    machine_id: int, data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    machine = _machine(db, machine_id)
    values = _component_values(db, machine, data)
    component = MachineComponent(
        uuid=str(uuid_lib.uuid4()), machine_id=machine.id,
        created_at=_now(), updated_at=_now(), **values,
    )
    db.add(component)
    db.commit()
    db.refresh(component)
    return component_to_dict(component)


@router.patch("/components/{component_id}")
def update_component(
    component_id: int, data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    component = db.query(MachineComponent).filter(MachineComponent.id == component_id).first()
    if component is None:
        raise HTTPException(status_code=404, detail="Composant introuvable")
    machine = _machine(db, component.machine_id)
    current = component_to_dict(component)
    current.update({key: value for key, value in data.items() if value is not None})
    for key, value in _component_values(db, machine, current).items():
        setattr(component, key, value)
    component.updated_at = _now()
    db.commit()
    db.refresh(component)
    return component_to_dict(component)


@router.delete("/components/{component_id}")
def delete_component(
    component_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    component = db.query(MachineComponent).filter(MachineComponent.id == component_id).first()
    if component is None:
        raise HTTPException(status_code=404, detail="Composant introuvable")
    db.delete(component)
    db.commit()
    return {"deleted": component_id}


# ── Affectation des devices ───────────────────────────────────────────
@router.put("/devices/{device_id}/psu")
def assign_device_psu(
    device_id: int, data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if device is None:
        raise HTTPException(status_code=404, detail="Device introuvable")
    machine = _machine(db, device.machine_id)
    resolved: list[int | None] = []
    for field in ("psu_id", "psu2_id"):
        value = data.get(field)
        if value in (None, "", 0):
            resolved.append(None)
            continue
        psu = _psu_or_404(db, int(value))
        if psu.machine_id != machine.id:
            raise HTTPException(status_code=400, detail="Cette alimentation n'appartient pas à la machine")
        resolved.append(psu.id)
    target_psu, second_psu = resolved
    # 2026.11.004-c5 : la répartition slot/riser exige les deux PSU — une
    # seconde alimentation seule est un choix incomplet, pas une répartition.
    if second_psu is not None and target_psu is None:
        raise HTTPException(
            status_code=422,
            detail="psu2_id exige psu_id : la part slot/riser se prend sur la PSU 1",
        )
    if target_psu is not None and target_psu == second_psu:
        raise HTTPException(status_code=422, detail="psu_id et psu2_id doivent être différents")
    device.psu_id = target_psu
    device.psu2_id = second_psu
    if second_psu is None:
        # Pas de seconde PSU, pas de répartition : ``split_w`` perd son sens.
        device.split_w = None
    elif "split_w" in data:
        if data["split_w"] in (None, ""):
            device.split_w = None
        else:
            try:
                split = float(data["split_w"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Part slot/riser invalide")
            if not 0 <= split <= 600:
                raise HTTPException(status_code=400, detail="Part slot/riser hors limites")
            device.split_w = split
    device.updated_at = _now()
    db.commit()
    db.refresh(device)
    result = evaluate_machine_power(db, machine)
    return {
        "device_id": device.id,
        "psu_id": device.psu_id,
        "psu2_id": device.psu2_id,
        "split_w": device.split_w,
        "power": result,
    }


# ── Détection de l'inventaire ─────────────────────────────────────────
@router.post("/machines/{machine_id}/inventory/detect")
async def detect_inventory(
    machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    from app.inventory import parse_inventory
    from app.ssh_client import ssh_collect_inventory

    machine = _machine(db, machine_id)
    raw = await ssh_collect_inventory(machine)
    if not raw.get("ok"):
        return {"ok": False, "error": raw.get("error") or "détection impossible"}
    detected = parse_inventory(raw.get("stdout") or "")
    # Detected rows are replaced, manual rows are kept (§4.2 bis).
    db.query(MachineComponent).filter(
        MachineComponent.machine_id == machine.id,
        MachineComponent.source == "detected",
    ).delete()
    for row in detected["components"]:
        db.add(MachineComponent(
            uuid=str(uuid_lib.uuid4()), machine_id=machine.id,
            created_at=_now(), updated_at=_now(), **row,
        ))
    db.commit()
    ensure_default_components(db, machine)
    rows = db.query(MachineComponent).filter(
        MachineComponent.machine_id == machine.id
    ).order_by(MachineComponent.kind.asc(), MachineComponent.id.asc()).all()
    return {"ok": True, "detected": detected, "components": [component_to_dict(row) for row in rows]}


# ── Puissance et réglages ─────────────────────────────────────────────
def _latest_power(db: Session, machine: Machine) -> dict[str, float]:
    if not machine.uuid:
        return {}
    rows = (
        db.query(Metric)
        .filter(Metric.machine_uuid == machine.uuid, Metric.power_w.isnot(None))
        .order_by(Metric.ts.desc())
        .limit(200)
        .all()
    )
    latest: dict[str, float] = {}
    for row in rows:
        if row.device_uuid not in latest:
            latest[row.device_uuid] = float(row.power_w)
    return latest


def evaluate_machine_power(db: Session, machine: Machine, notify: bool = False) -> dict[str, Any]:
    """Charge, puissance à la prise et alertes d'une machine (§4.2 / §4.2 bis)."""
    from app.devices import integrated_hidden

    psus = db.query(Psu).filter(Psu.machine_id == machine.id).order_by(Psu.id.asc()).all()
    components = db.query(MachineComponent).filter(
        MachineComponent.machine_id == machine.id
    ).all()
    devices = db.query(Device).filter(
        Device.machine_id == machine.id, Device.present.is_(True)  # noqa: E712
    ).order_by(Device.kind.desc(), Device.index.asc()).all()
    measured = _latest_power(db, machine)
    hide = integrated_hidden(db)
    device_rows = []
    for device in devices:
        if device.kind == "cpu":
            label = f"CPU {device.model or ''}".strip()
        else:
            label = f"GPU {device.index if device.index is not None else '?'} — {device.model or ''}".strip()
        device_rows.append({
            "uuid": device.uuid,
            "kind": device.kind,
            "label": label,
            "model": device.model,
            "power_w": measured.get(device.uuid),
            "psu_id": device.psu_id,
            "psu2_id": device.psu2_id,
            "split_w": device.split_w,
            "enabled": bool(device.enabled_for_mining) and not (device.integrated and hide),
            "integrated": bool(device.integrated),
        })
    # 2026.11.004-c5 : la puissance CPU suit l'ordre mesure > saisie > TDP du
    # modèle > 65 W générique, et la réponse dit laquelle a servi.
    from app.cpu_stats import estimate_cpu_w

    cpu_row = next((row for row in device_rows if row["kind"] == "cpu"), None)
    cpu_uuid = (cpu_row or {}).get("uuid") or ""
    cpu_model = (cpu_row or {}).get("model")
    measured_cpu = measured.get(cpu_uuid)
    cpu_power_w: float
    if measured_cpu:
        cpu_power_w, cpu_source, cpu_estimated = float(measured_cpu), "mesure (dernière collecte)", False
    elif machine.cpu_power_w:
        cpu_power_w, cpu_source, cpu_estimated = float(machine.cpu_power_w), "saisie (machines.cpu_power_w)", False
    else:
        cpu_power_w, cpu_source = estimate_cpu_w(cpu_model)
        cpu_estimated = True
    result = compute_power(
        [psu_to_dict(psu) for psu in psus],
        [component_to_dict(row) for row in components],
        device_rows,
        machine_power_w=cpu_power_w,
    )
    result["machine_id"] = machine.id
    result["machine"] = machine.name
    result["cpu"] = {
        "model": cpu_model,
        "power_w": round(float(cpu_power_w), 1),
        "power_source": cpu_source,
        "estimated": cpu_estimated,
    }
    if notify:
        emit_overload_alerts(db, machine, result)
    return result


def emit_overload_alerts(db: Session, machine: Machine, result: dict[str, Any]) -> list[int]:
    """One notification per PSU over 90 %, at most every 6 h (§4.14)."""
    from app import notifications

    emitted: list[int] = []
    for psu in result.get("psus") or []:
        if psu.get("status") != "red":
            continue
        key = f"psu:{machine.uuid}:{psu['id']}"
        state = notifications.get_state(db, key)
        last = state.get("ts")
        if last:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
                if age < 6 * 3600:
                    continue
            except ValueError:
                pass
        message = (
            f"L'alimentation {psu['name']} de {machine.name} est chargée à "
            f"{psu['load_pct']} % ({psu['load_dc_w']} W sur {psu['available_dc_w']} W disponibles)."
        )
        emitted += notifications.enqueue(db, "psu_overloaded", message)
        notifications.set_state(db, key, {"ts": _now()})
    return emitted


@router.get("/machines/{machine_id}/power")
def machine_power(
    machine_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    machine = _machine(db, machine_id)
    ensure_default_components(db, machine)
    return evaluate_machine_power(db, machine)


@router.get("/power/settings")
def read_power_settings(db: Session = Depends(get_db), _user=Depends(require_login)):
    return {
        "hide_integrated_gpus": integrated_hidden_read(db),
        "certifications": list(CERTIFICATIONS),
        "presets": list(PSU_PRESETS),
    }


@router.put("/power/settings")
def write_power_settings(
    data: dict, db: Session = Depends(get_db), _user=Depends(require_login)
):
    if "hide_integrated_gpus" in data:
        set_setting(db, HIDE_INTEGRATED_KEY, bool(data["hide_integrated_gpus"]))
        db.commit()
    return read_power_settings(db)


def integrated_hidden_read(db: Session) -> bool:
    return bool(get_setting(db, HIDE_INTEGRATED_KEY, True))
