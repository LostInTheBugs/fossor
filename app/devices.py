"""Detection results -> Device rows (idempotent re-detection).

Pure database logic, no SSH: :func:`apply_detection` receives the machine and
the dictionary produced by :func:`app.hw_parser.parse_detect_hw`.

Rules
-----
* GPUs are matched by ``pci_bus`` (normalised), falling back to the NVIDIA
  UUID when the PCI bus is unknown;
* a machine has at most one CPU device, matched by ``kind='cpu'``;
* a device that disappears is flagged ``present=False`` and never deleted;
* ``enabled_for_mining`` (and the device ``uuid``) survive re-detection.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.hw_parser import norm_bus
from app.models import Device, Machine


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_gpu(db: Session, machine_id: int, gpu: dict[str, Any]) -> Device | None:
    bus = norm_bus(gpu.get("pci_bus"))
    if bus:
        found = (
            db.query(Device)
            .filter(Device.machine_id == machine_id, Device.kind == "gpu")
            .all()
        )
        for device in found:
            if norm_bus(device.pci_bus) == bus:
                return device
    nv_uuid = (gpu.get("nv_uuid") or "").strip()
    if nv_uuid:
        return (
            db.query(Device)
            .filter(
                Device.machine_id == machine_id,
                Device.kind == "gpu",
                Device.nv_uuid == nv_uuid,
            )
            .first()
        )
    return None


def _apply(db: Session, device: Device, values: dict[str, Any]) -> None:
    for key, value in values.items():
        setattr(device, key, value)
    device.present = True
    device.updated_at = _now()


def apply_detection(db: Session, machine: Machine, detected: dict[str, Any]) -> dict[str, Any]:
    """Create/update/soft-remove devices for *machine*; commit and report."""
    seen: set[int] = set()
    created = 0
    updated = 0

    cpu = detected.get("cpu")
    if cpu:
        values = {
            "vendor": "Intel" if "intel" in (cpu.get("model") or "").lower() else
                      "AMD" if "amd" in (cpu.get("model") or "").lower() else "",
            "model": cpu.get("model") or "",
            "cores": cpu.get("cores"),
            "threads": cpu.get("threads"),
            "ram_mb": cpu.get("ram_mb"),
            "flags": cpu.get("flags") or "",
            "index": 0,
        }
        device = (
            db.query(Device)
            .filter(Device.machine_id == machine.id, Device.kind == "cpu")
            .first()
        )
        if device is None:
            device = Device(
                uuid=str(uuid_lib.uuid4()),
                machine_id=machine.id,
                kind="cpu",
                enabled_for_mining=True,
                created_at=_now(),
            )
            db.add(device)
            created += 1
        else:
            updated += 1
        _apply(db, device, values)
        db.flush()
        seen.add(device.id)

    for position, gpu in enumerate(detected.get("gpus") or []):
        values = {
            "vendor": gpu.get("vendor") or "",
            "model": gpu.get("model") or "",
            "index": gpu.get("index") if gpu.get("index") is not None else position,
            "vram_mb": gpu.get("vram_mb"),
            "pci_bus": norm_bus(gpu.get("pci_bus")) or None,
            "driver": gpu.get("driver") or None,
            "nv_uuid": gpu.get("nv_uuid") or None,
            "integrated": bool(gpu.get("integrated")),
            "cores": None,
            "threads": None,
            "ram_mb": None,
            "flags": "",
        }
        device = _find_gpu(db, machine.id, gpu)
        if device is None:
            device = Device(
                uuid=str(uuid_lib.uuid4()),
                machine_id=machine.id,
                kind="gpu",
                # An integrated GPU is recorded but never mined unless asked.
                enabled_for_mining=not bool(gpu.get("integrated")),
                created_at=_now(),
            )
            db.add(device)
            created += 1
        else:
            updated += 1
        _apply(db, device, values)
        db.flush()
        seen.add(device.id)

    missing = 0
    for device in db.query(Device).filter(Device.machine_id == machine.id).all():
        if device.id not in seen and device.present:
            device.present = False
            device.updated_at = _now()
            missing += 1

    virt = detected.get("virt") or {}
    machine.vm_host = bool(virt.get("kvm") or virt.get("pveversion"))
    os_kind = detected.get("os_kind")
    if os_kind:
        machine.os_kind = os_kind
    machine.updated_at = _now()
    machine.usage = compute_usage(db, machine)

    db.commit()
    return {
        "created": created,
        "updated": updated,
        "missing": missing,
        "device_count": len(seen),
    }


def compute_usage(db: Session, machine: Machine) -> str:
    """Machine usage from its *present* devices (§4.2).

    cpu + >=1 gpu enabled = ``both``; cpu only = ``cpu``; gpu only = ``gpu``;
    nothing enabled = ``none``.  Absent devices never count.
    """
    db.flush()  # pending PATCH must be visible (session is autoflush=False)
    rows = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present.is_(True))
        .all()
    )
    cpu = any(device.kind == "cpu" and device.enabled_for_mining for device in rows)
    gpu = any(device.kind == "gpu" and device.enabled_for_mining for device in rows)
    if cpu and gpu:
        return "both"
    if cpu:
        return "cpu"
    if gpu:
        return "gpu"
    return "none"


def recompute_usage(db: Session, machine: Machine) -> str:
    """Recompute and store ``machine.usage`` (called on every device PATCH and detect)."""
    machine.usage = compute_usage(db, machine)
    machine.updated_at = _now()
    return machine.usage


def device_to_dict(device: Device) -> dict[str, Any]:
    return {
        "id": device.id,
        "uuid": device.uuid,
        "machine_id": device.machine_id,
        "kind": device.kind,
        "index": device.index,
        "vendor": device.vendor,
        "model": device.model,
        "vram_mb": device.vram_mb,
        "pci_bus": device.pci_bus,
        "driver": device.driver,
        "cores": device.cores,
        "threads": device.threads,
        "ram_mb": device.ram_mb,
        "flags": device.flags,
        "present": device.present,
        "enabled_for_mining": device.enabled_for_mining,
        "integrated": device.integrated,
        "created_at": device.created_at,
        "updated_at": device.updated_at,
    }
