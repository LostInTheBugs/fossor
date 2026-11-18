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


def integrated_hidden(db: Session) -> bool:
    """Global « Masquer les GPU intégrés du minage » (§4.2), activé par défaut."""
    from app.ai_config import get_setting

    try:
        return bool(get_setting(db, "hide_integrated_gpus", True))
    except Exception:
        return True


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


def apply_detection(db: Session, machine: Machine, detected: dict[str, Any], gpu_compute: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Create/update/soft-remove devices for *machine*; commit and report.

    *gpu_compute* is a list of external miner GPU entries from nvidia-smi.
    GPUs whose gpu_id matches an external miner are marked occupied_external.
    """
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
        # §4.2: un GPU intégré n'est jamais miné tant que le masquage est actif.
        if device.integrated and integrated_hidden(db):
            device.enabled_for_mining = False
        db.flush()
        seen.add(device.id)

    # Mark GPUs occupied by external miners
    if gpu_compute:
        occupied_indices: set[int] = set()
        for miner in gpu_compute:
            gid = miner.get("gpu_id")
            if gid is not None:
                occupied_indices.add(gid)
        for device in db.query(Device).filter(
            Device.machine_id == machine.id,
            Device.kind == "gpu",
            Device.present.is_(True),
        ).all():
            dev_idx = device.index
            if dev_idx is not None and dev_idx in occupied_indices:
                device.occupied_external = True
            else:
                device.occupied_external = False
            device.updated_at = _now()

    # CPU miners (cpuminer, wam-miner, SRBMiner --cpu-threads) occupy the CPU.
    if gpu_compute is not None:
        cpu_device = (
            db.query(Device)
            .filter(Device.machine_id == machine.id, Device.kind == "cpu")
            .first()
        )
        if cpu_device is not None:
            cpu_device.occupied_external = any(
                str(entry.get("kind") or "") == "cpu" for entry in gpu_compute
            )
            cpu_device.updated_at = _now()

    missing = 0
    for device in db.query(Device).filter(Device.machine_id == machine.id).all():
        if device.id not in seen and device.present:
            device.present = False
            device.updated_at = _now()
            missing += 1

    if gpu_compute is not None:
        machine.external_miners = _external_miners_json(gpu_compute)

    virt = detected.get("virt") or {}
    machine.vm_host = bool(virt.get("kvm") or virt.get("pveversion"))
    os_kind = detected.get("os_kind")
    if os_kind:
        # §4.1 ter : le type choisi fait foi, l'OS réel est conservé à part et
        # l'écart est affiché. Une machine créée sans type (« autre ») prend le
        # type détecté, comme avant.
        machine.os_kind_detected = os_kind
        if not machine.os_kind or str(machine.os_kind) == "other":
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


def _external_miners_json(entries: list[dict[str, Any]]) -> str | None:
    """Serialise the display fields of external miners (already masked)."""
    import json

    if not entries:
        return None
    payload = [
        {
            "name": entry.get("miner_name") or entry.get("label") or entry.get("name") or "",
            # 2026.11.003-c7: l'exécutable réellement lancé (cpuminer-korsh…).
            "binary": entry.get("binary") or "",
            "kind": entry.get("kind") or "",
            "version": entry.get("version") or "",
            "algorithm": entry.get("algorithm") or "",
            "pool": entry.get("pool") or "",
            "coin": entry.get("coin") or "",
            "threads": entry.get("threads") or "",
            "user": entry.get("user") or "",
            "pid": entry.get("pid"),
            "cpu_pct": entry.get("cpu_pct"),
            "gpu_id": entry.get("gpu_id"),
            # 2026.11.003-c4: identical processes are one entry with a count,
            # node solo mining reports its worker processes and the daemon's
            # hashrate, so the dashboard can group by coin.
            "count": entry.get("count") or 1,
            "workers": entry.get("workers"),
            "hashrate_hs": entry.get("hashrate_hs"),
            # 2026.11.003-c7: d'où vient le hashrate CPU (API, log, nœud) et,
            # quand il est inconnu, la raison exacte — jamais un « — » muet.
            "hashrate_source": entry.get("hashrate_source") or "",
            "hashrate_note": entry.get("hashrate_note") or "",
            "node": entry.get("node"),
            "api_port": entry.get("api_port"),
        }
        for entry in entries
    ]
    return json.dumps(payload, ensure_ascii=False)


def set_external_miners(machine: Machine, entries: list[dict[str, Any]]) -> None:
    """Persist the last external miners seen on *machine*."""
    machine.external_miners = _external_miners_json(entries)


def mark_external_devices(db: Session, machine: Machine, entries: list[dict[str, Any]]) -> None:
    """Flag devices occupied by the current external miners (every round).

    GPU entries match by ``gpu_id`` when nvidia-smi reported one; a ps-only
    GPU miner (older driver, AMD) marks the non-integrated GPUs.  A CPU miner
    marks the CPU device.
    """
    gpu_ids = {e.get("gpu_id") for e in entries if e.get("gpu_id") is not None}
    gpu_ps_only = any(
        str(e.get("kind") or "gpu") == "gpu" and e.get("gpu_id") is None
        for e in entries
    )
    cpu_busy = any(str(e.get("kind") or "") == "cpu" for e in entries)
    for device in db.query(Device).filter(
        Device.machine_id == machine.id, Device.present.is_(True)  # noqa: E712
    ).all():
        if device.kind == "cpu":
            device.occupied_external = cpu_busy
        elif device.index is not None and device.index in gpu_ids:
            device.occupied_external = True
        elif gpu_ps_only and not device.integrated:
            device.occupied_external = True
        else:
            device.occupied_external = False
        device.updated_at = _now()


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
    hide = integrated_hidden(db)
    gpu = any(
        device.kind == "gpu" and device.enabled_for_mining
        and not (device.integrated and hide)
        for device in rows
    )
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
        "occupied_external": device.occupied_external,
        # §4.19 : rôle (token par défaut / spec mining), pool de boost, token propre.
        "role": getattr(device, "role", None) or "default_and_spec",
        "boost_pool": bool(getattr(device, "boost_pool", False)),
        "default_token_id": getattr(device, "default_token_id", None),
        # §4.2 bis : PSU qui porte la carte (et seconde PSU si réparti).
        "psu_id": device.psu_id,
        "psu2_id": device.psu2_id,
        "split_w": device.split_w,
        "created_at": device.created_at,
        "updated_at": device.updated_at,
    }
