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

    # §4.2 : occupation recalculée à chaque détection.  Depuis la 023-bis, une
    # unité lancée par Fossor n'est plus « externe » : seule reste bloquante
    # l'occupation par un processus hors unités Fossor.
    if gpu_compute is not None:
        mark_external_devices(db, machine, gpu_compute)
        set_external_miners(machine, gpu_compute)

    missing = 0
    for device in db.query(Device).filter(Device.machine_id == machine.id).all():
        if device.id not in seen and device.present:
            device.present = False
            device.updated_at = _now()
            missing += 1

    virt = detected.get("virt") or {}
    machine.vm_host = bool(virt.get("vm_capable", virt.get("kvm") or virt.get("pveversion")))
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


OCCUPIED_EXTERNAL = "external"
OCCUPIED_FOSSOR_DEFAULT = "fossor_default"
OCCUPIED_FOSSOR_CAMPAIGN = "fossor_campaign"
OCCUPIED_HIVEOS = "hiveos"

OCCUPANCY_LABELS = {
    OCCUPIED_EXTERNAL: "Mineur externe (hors Fossor)",
    OCCUPIED_FOSSOR_DEFAULT: "Token par défaut (Fossor)",
    OCCUPIED_HIVEOS: "HiveOS (flight sheet)",
}


def _fossor_markers() -> tuple[str, ...]:
    """Chemins par lesquels une unité lancée par Fossor se reconnaît dans ``ps``."""
    from app import miners as M

    return (f"{M.INSTALL_ROOT}/", f"{M.LOG_DIR}/", f"{M.RUN_DIR}/", "fossor-miner-")


def is_fossor_process(entry: dict[str, Any]) -> bool:
    """La ligne ``ps`` appartient-elle à une unité lancée par Fossor ?

    Le binaire installé (``/opt/fossor/miners``), le journal, le fichier PID et
    le nom d'unité sont les seules signatures propres à Fossor.  Le **compte**
    ne dit rien : depuis la 021 les unités tournent sous ``fossor``.
    """
    text = " ".join(
        str(entry.get(key) or "") for key in ("cmd", "binary", "process_name", "name")
    )
    return any(marker in text for marker in _fossor_markers())


def _binary_markers(profile: Any) -> tuple[str, ...]:
    """Chemins qui désignent le binaire du profil dans une ligne ``ps``."""
    from app import miners as M

    full = M.binary_path(
        {
            "software": profile.software,
            "version": profile.version,
            "binary_path": profile.binary_path,
        }
    )
    rel = str(getattr(profile, "binary_path", "") or "")
    return tuple(dict.fromkeys(value for value in (full, rel) if value))


def _profiles_here(db: Session, machine: Machine) -> list[Any]:
    """Profils qui possèdent des devices **sur cette machine** (règle 023).

    Rattachés par ``devices.default_token_id``, jamais par le chemin du binaire :
    deux profils Pearl partagent le même exécutable.
    """
    from app.models import MinerProfile

    ids: set[int] = set()
    if getattr(machine, "default_miner_profile_id", None):
        ids.add(int(machine.default_miner_profile_id))
    rows = (
        db.query(Device)
        .filter(
            Device.machine_id == machine.id,
            Device.present.is_(True),  # noqa: E712
            Device.default_token_id.isnot(None),
        )
        .all()
    )
    ids.update(int(row.default_token_id) for row in rows if row.default_token_id)
    found: list[Any] = []
    for profile_id in sorted(ids):
        profile = db.get(MinerProfile, profile_id)
        if profile is None or not profile.enabled:
            continue
        found.append(profile)
    return found


def profile_for_entry(db: Session, machine: Machine, entry: dict[str, Any]) -> Any:
    """Profil Fossor dont l'unité a produit cette ligne ``ps``, sinon ``None``."""
    text = str(entry.get("cmd") or "")
    port = entry.get("api_port")
    matches = [
        profile
        for profile in _profiles_here(db, machine)
        if any(marker in text for marker in _binary_markers(profile))
    ]
    if not matches:
        return None
    if port:
        for profile in matches:
            if profile.api_port and int(profile.api_port) == int(port):
                return profile
    return matches[0]


def running_campaign_for(db: Session, device_uuid: str) -> int | None:
    """Campagne en cours qui tient ce device (pour l'étiquette « Campagne #N »)."""
    from app.models import Campaign, CampaignDevice

    row = (
        db.query(CampaignDevice)
        .join(Campaign, Campaign.id == CampaignDevice.campaign_id)
        .filter(
            CampaignDevice.device_uuid == device_uuid,
            Campaign.status.in_(["running", "stopping"]),
        )
        .order_by(Campaign.id.desc())
        .first()
    )
    if row is None:
        return None
    value = getattr(row, "campaign_id", None)
    return int(value) if value is not None else None


def occupancy(device: Device) -> dict[str, Any]:
    """Occupation d'un device, telle que l'API et les écrans la lisent."""
    by = getattr(device, "occupied_by", None)
    if not by:
        by = OCCUPIED_EXTERNAL if getattr(device, "occupied_external", False) else None
    campaign_id = getattr(device, "occupied_campaign_id", None)
    if by == OCCUPIED_FOSSOR_CAMPAIGN:
        label = (
            f"Campagne #{campaign_id} (Fossor)" if campaign_id else "Campagne (Fossor)"
        )
    else:
        label = OCCUPANCY_LABELS.get(by or "", "")
    return {
        "occupied_by": by,
        "occupied_label": label,
        "occupied_campaign_id": campaign_id,
        "occupied_external": by == OCCUPIED_EXTERNAL,
        "releasable": by in (
            OCCUPIED_FOSSOR_DEFAULT,
            OCCUPIED_FOSSOR_CAMPAIGN,
            OCCUPIED_HIVEOS,
        ),
    }


def filter_external_miners(
    db: Session, machine: Machine, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mineurs réellement externes : jamais une unité Fossor.

    Le filtre s'applique aussi à la **lecture** : une valeur stockée par une
    collecte antérieure, ou un ``ps`` qui mélange le nœud BigCoin et l'unité
    Pearl, ne peut pas faire réapparaître un mineur Fossor comme « externe ».
    """
    ports: set[int] = set()
    softwares: set[str] = set()
    for profile in _profiles_here(db, machine):
        try:
            if profile.api_port:
                ports.add(int(profile.api_port))
        except (TypeError, ValueError):
            pass
        software = str(getattr(profile, "software", "") or "").strip().lower()
        if software:
            softwares.add(software)
    kept: list[dict[str, Any]] = []
    for entry in entries or []:
        if is_fossor_process(entry):
            continue
        try:
            port = int(entry.get("api_port")) if entry.get("api_port") else None
        except (TypeError, ValueError):
            port = None
        # 2026.11.026 : le port ne suffit pas. Un nœud solo n'est pas l'unité
        # du profil ; seul un processus qui porte le logiciel du profil est
        # écarté par ce port. BigCoin garde son entrée, sans pool ni API Pearl.
        text = " ".join(
            str(entry.get(key) or "")
            for key in ("name", "miner_name", "label", "binary", "process_name", "cmd")
        ).lower()
        is_profile_unit = (
            any(software in text for software in softwares) if softwares else True
        )
        # 2026.11.026 : une entrée solo contaminée (nom du logiciel Fossor,
        # pool ou algorithme d'un autre processus, port du profil) est le
        # résidu de l'ancienne fusion ; elle n'est pas un vrai nœud BigCoin.
        if str(entry.get("kind") or "") == "solo":
            contaminated = bool(entry.get("pool") or entry.get("algorithm"))
            if is_profile_unit and (contaminated or (port is not None and port in ports)):
                continue
            kept.append(entry)
            continue
        if port is not None and port in ports and is_profile_unit:
            continue
        kept.append(entry)
    return kept


def set_external_miners(
    machine: Machine, entries: list[dict[str, Any]], db: Session | None = None
) -> None:
    """Persist the last **external** miners seen on *machine*.

    Une unité Fossor n'est pas un mineur externe (023-bis) ; sur HiveOS, le
    mineur du flight sheet est piloté par ``miner stop/start`` et ne l'est pas
    non plus.
    """
    if db is not None:
        entries = filter_external_miners(db, machine, entries)
    hiveos = str(getattr(machine, "os_kind", "") or "") == "hiveos"
    kept: list[dict[str, Any]] = []
    for entry in entries or []:
        if is_fossor_process(entry):
            continue
        if hiveos and str(entry.get("kind") or "gpu") == "gpu":
            continue  # flight sheet : piloté par miner stop/start
        kept.append(entry)
    machine.external_miners = _external_miners_json(kept)


def mark_external_devices(db: Session, machine: Machine, entries: list[dict[str, Any]]) -> None:
    """Étiquette l'occupation de chaque device présent (à chaque collecte).

    - unité Fossor du token par défaut du device → ``fossor_default`` ;
    - unité Fossor d'une campagne en cours → ``fossor_campaign`` (+ n°) ;
    - mineur du flight sheet HiveOS → ``hiveos`` ;
    - tout le reste → ``external``, seul cas qui bloque Fossor.

    Une occupation externe prime : un GPU tenu à la fois par une unité Fossor et
    par un mineur hors Fossor reste bloqué.
    """
    rows = list(entries or [])
    hiveos = str(getattr(machine, "os_kind", "") or "") == "hiveos"
    devices = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present.is_(True))  # noqa: E712
        .all()
    )
    claims: dict[int, tuple[str, int | None]] = {}

    def claim(device: Device, by: str, campaign_id: int | None = None) -> None:
        current = claims.get(device.id)
        if current is not None:
            if current[0] == OCCUPIED_EXTERNAL:
                return  # une occupation externe prime : elle bloque
            if by != OCCUPIED_EXTERNAL:
                return  # première occupation libérable retenue
        claims[device.id] = (by, campaign_id)

    def gpus(entry: dict[str, Any]) -> list[Device]:
        gpu_id = entry.get("gpu_id")
        return [
            device
            for device in devices
            if device.kind == "gpu"
            and not device.integrated
            and (gpu_id is None or device.index == gpu_id)
        ]

    for entry in rows:
        kind = str(entry.get("kind") or "gpu")
        cpu_entry = kind in ("cpu", "solo")
        if hiveos:
            # Le flight sheet ne couvre que les GPU : `miner stop/start` les
            # libère.  Un mineur CPU (wam-miner, nœud) reste hors Fossor.
            targets = [d for d in devices if d.kind == "cpu"] if cpu_entry else gpus(entry)
            for device in targets:
                claim(device, OCCUPIED_EXTERNAL if cpu_entry else OCCUPIED_HIVEOS)
            continue
        if is_fossor_process(entry):
            profile = profile_for_entry(db, machine, entry)
            if profile is not None:
                owned = [
                    device
                    for device in devices
                    if getattr(device, "default_token_id", None) == profile.id
                    and (device.kind == "cpu") == cpu_entry
                    and (cpu_entry or not device.integrated)
                ]
            else:
                # Unité Fossor sans profil rattaché : on ne bloque pas le device
                # qu'elle utilise, on le dit « token par défaut ».
                owned = [d for d in devices if d.kind == "cpu"] if cpu_entry else gpus(entry)
            for device in owned:
                campaign_id = running_campaign_for(db, device.uuid)
                claim(
                    device,
                    OCCUPIED_FOSSOR_CAMPAIGN if campaign_id else OCCUPIED_FOSSOR_DEFAULT,
                    campaign_id,
                )
            continue
        targets = [d for d in devices if d.kind == "cpu"] if cpu_entry else gpus(entry)
        for device in targets:
            claim(device, OCCUPIED_EXTERNAL)

    for device in devices:
        by, campaign_id = claims.get(device.id, (None, None))
        device.occupied_by = by
        device.occupied_campaign_id = campaign_id
        device.occupied_external = by == OCCUPIED_EXTERNAL
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
        # 2026.11.023-bis : origine de l'occupation et libellé pour l'écran.
        **occupancy(device),
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
