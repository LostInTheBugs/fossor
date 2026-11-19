"""Planificateur des campagnes (§4.7) — une boucle, reprise après redémarrage.

Le planificateur fait avancer une campagne selon l'heure : arrêt du token par
défaut, démarrage du profil de campagne, arrêt à la fin, **retour vérifié** au
token par défaut.  Trois propriétés portent la sûreté :

* **idempotence par journal** — chaque étape franchie écrit un
  :class:`~app.models.CampaignEvent` de résultat ``ok`` ; une étape déjà écrite
  n'est jamais rejouée, y compris après un redémarrage du conteneur (l'état et
  l'étape en cours se relisent en base, rien n'est gardé en mémoire) ;
* **une seule boucle** — même processus, verrou unique : deux ticks ne peuvent
  pas lancer la même action deux fois ;
* **simulation par défaut** — le backend réel n'est appelé que si la simulation
  est désactivée ; en simulation, les commandes qui *seraient* lancées sont
  journalisées et le retour vérifié est marqué comme simulé.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app import campaigns as C

logger = logging.getLogger("fossor.campaigns")

LOOP_INTERVAL = 10.0
RESTORE_TIMEOUT_KEY = "campaign_restore_timeout_s"
ACTIVE_STATUSES = ("approved", "running", "stopping")

_LOCK: asyncio.Lock | None = None
_task: asyncio.Task | None = None


# ── Cibles ────────────────────────────────────────────────────────────
@dataclass
class Target:
    """Une machine engagée par la campagne, et les devices qu'elle y engage."""

    machine: Any
    device_uuids: list[str] = field(default_factory=list)
    gpus: list[int] = field(default_factory=list)
    threads: int = 1
    labels: list[str] = field(default_factory=list)


def campaign_targets(db: Any, campaign: Any) -> list[Target]:
    from app.models import CampaignDevice, Device, Machine

    rows = (
        db.query(CampaignDevice)
        .filter(CampaignDevice.campaign_id == campaign.id)
        .order_by(CampaignDevice.id.asc())
        .all()
    )
    by_machine: dict[int, Target] = {}
    for row in rows:
        target = by_machine.get(row.machine_id)
        if target is None:
            machine = db.query(Machine).filter(Machine.id == row.machine_id).first()
            if machine is None:
                continue
            target = Target(machine=machine)
            by_machine[row.machine_id] = target
        if row.device_uuid:
            target.device_uuids.append(row.device_uuid)
        device = None
        if row.device_uuid:
            device = db.query(Device).filter(Device.uuid == row.device_uuid).first()
        if device is not None:
            if device.kind == "gpu" and device.index is not None and device.present:
                target.gpus.append(int(device.index))
            if device.kind == "cpu":
                target.threads = int(device.threads or 1)
        target.labels.append(row.label or (row.device_uuid or "device"))
    for target in by_machine.values():
        target.gpus = sorted(set(target.gpus))
    return list(by_machine.values())


def _profile_dict(profile: Any) -> dict[str, Any]:
    from app.routers.miners import profile_to_dict

    return profile_to_dict(profile)


def render_argv(profile: dict[str, Any], machine: Any, target: Target,
                vm_node: str | None = None) -> list[str]:
    from app import miners as M

    values = M.build_values(
        profile,
        worker=M.worker_name(getattr(machine, "name", "")),
        gpus=target.gpus,
        threads=target.threads,
        api_port=profile.get("api_port") or None,
        vm_node=vm_node,
    )
    return M.render_argv(profile, values)


#: Adresse de simulation (TEST-NET-1) : jamais routable, jamais une vraie VM.
SIMULATION_IP = "192.0.2.10"


def campaign_mode(db: Any, campaign: Any, profile: dict[str, Any] | None) -> str:
    """Mode effectif de la campagne : colonne, sinon mode du profil, sinon host."""
    from app import spec_vm as V

    raw = getattr(campaign, "execution_mode", "") or (profile or {}).get("execution_mode") or ""
    try:
        return V.normalize_mode(raw, "host")
    except V.VmError:
        return "host"


def _spec_host(db: Any, model: dict[str, Any]):
    from app.models import Machine

    try:
        machine_id = int(model.get("host_id") or 0)
    except (TypeError, ValueError):
        return None
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if machine is None or not machine.vm_host:
        return None
    return machine


def _spec_vm_row(db: Any, campaign: Any):
    from app.models import SpecVm

    return (
        db.query(SpecVm)
        .filter(SpecVm.campaign_id == campaign.id)
        .order_by(SpecVm.id.desc())
        .first()
    )


def _register_wallet(db: Any, coin: str, address: str, vm_row: Any, backup_path: str) -> None:
    """§4.4 : seule l'adresse et le chemin de sauvegarde sont enregistrés."""
    from app.models import Wallet

    address = (address or "").strip()
    if not address:
        return
    wallet = (
        db.query(Wallet)
        .filter(Wallet.coin_ticker == coin.upper(), Wallet.address == address)
        .first()
    )
    if wallet is None:
        wallet = Wallet(coin_ticker=coin.upper(), address=address)
        db.add(wallet)
    wallet.location = f"VM {vm_row.name}"
    wallet.backup_path = backup_path
    db.commit()


def _spec_context(db: Any, backend: Any):
    """Hôte, modèle et backend de la VM — **le** point de décision (§4.6).

    Le backend du planificateur peut porter la fabrique (``spec_backend``) :
    c'est par là que passent les tests, qui remplacent ensuite le pilote
    d'exécution sans changer la décision Proxmox/libvirt.
    """
    from app import vm_backend as VB

    factory = getattr(backend, "spec_backend", None)
    try:
        if callable(factory):
            return factory(db), None
        return VB.backend_for(db), None
    except VB.VmBackendError as exc:
        return None, str(exc)


async def _spec_start_steps(
    db: Any,
    campaign: Any,
    moment: datetime,
    backend: Any,
    simulated: bool,
    mode: str,
    profile: dict[str, Any],
    targets: list[Target],
) -> dict[str, Any] | None:
    """Création → adresse → provisionnement → mineur. ``None`` si tout va bien."""
    import uuid as uuid_lib

    from app import spec_vm as V
    from app.routers.vms import record_vm

    spec, error = _spec_context(db, backend)
    if spec is None:
        return _fail(db, campaign, "vm-model-missing", error or "modèle de VM absent",
                     simulated)
    host, model, vm = spec.host, spec.model, spec.backend

    row = _spec_vm_row(db, campaign)
    if row is None:
        name = V.vm_name(campaign.id, campaign.coin, uuid_lib.uuid4().hex[:6])
        row = record_vm(
            db,
            campaign_id=campaign.id,
            host_machine_id=host.id,
            name=name,
            mode=mode,
            model=model,
            wallet_address=str(profile.get("wallet") or ""),
            hypervisor=vm.kind,
        )
    db.commit()

    # 1. Création et démarrage de la VM (qm pour Proxmox, virt-install sinon).
    if not _step_done(db, campaign, "vm-created"):
        try:
            result = await vm.create(host, model, row, simulated)
        except Exception as exc:
            row.state = "failed"
            row.note = str(exc)[:800]
            db.commit()
            log_event(db, campaign, "vm-created", str(exc)[:800], result="fail", simulated=simulated)
            return _fail(db, campaign, "vm-create-failed", f"Création de la VM impossible : {exc}",
                         simulated)
        if not simulated:
            row.state = "running"
            if result.get("vmid"):
                row.vmid = int(result["vmid"])
        db.commit()
        log_event(
            db, campaign, "vm-created",
            f"VM {row.name} créée et démarrée sur {host.name} ({vm.kind})",
            simulated=simulated,
            detail={"commands": result.get("commands") or [], "vm": row.name, "mode": mode,
                    "hypervisor": vm.kind, "vmid": getattr(row, "vmid", None)},
        )
        db.commit()

    # 2. Adresse de la VM (agent invité / bail ARP, puis clé d'hôte épinglée).
    if not _step_done(db, campaign, "vm-addressed"):
        ip = SIMULATION_IP if simulated else ""
        if not simulated:
            try:
                result = await vm.wait_address(host, model, row, simulated=False)
                ip = str(result.get("ip") or "")
                if ip:
                    try:
                        await vm.pin_host_key(host, model, row, ip, False)
                    except Exception as exc:  # clé d'hôte indisponible : on continue, noté
                        log_event(db, campaign, "vm-pin-failed", str(exc)[:300], result="warn",
                                  simulated=simulated)
            except Exception as exc:
                log_event(db, campaign, "vm-addressed", str(exc)[:400], result="fail",
                          simulated=simulated)
                return _fail(db, campaign, "vm-no-address", f"Adresse de la VM introuvable : {exc}",
                             simulated)
        row.ip = ip
        db.commit()
        log_event(db, campaign, "vm-addressed", f"adresse de la VM : {ip or 'inconnue'}",
                  simulated=simulated, detail={"ip": ip})

    # 3. Provisionnement dans la VM (binaires vérifiés par SHA256 dans la VM).
    if not _step_done(db, campaign, "vm-provisioned"):
        try:
            result = await vm.provision(
                host, model, profile, row,
                coin=campaign.coin,
                expected_address=str(profile.get("wallet") or ""),
                simulated=simulated,
            )
        except Exception as exc:
            log_event(db, campaign, "vm-provisioned", str(exc)[:800], result="fail",
                      simulated=simulated)
            return _fail(db, campaign, "vm-provision-failed", f"Provisionnement impossible : {exc}",
                         simulated)
        address = str(result.get("address") or profile.get("wallet") or "")
        if not simulated and address:
            row.wallet_address = address
        db.commit()
        if not simulated:
            _register_wallet(db, campaign.coin, address, row, str(model.get("wallet_backup_path") or ""))
        log_event(
            db, campaign, "vm-provisioned",
            "nœud, wallet et mineur installés dans la VM (adresse seule remontée)",
            simulated=simulated,
            detail={"commands": result.get("commands") or [], "address": address},
        )
        db.commit()

    # 4. Mineur : dans la VM (mode vm) ou sur l'hôte (mode hybrid).
    if not _step_done(db, campaign, "campaign-started"):
        commands: list[str] = []
        try:
            if mode == "vm":
                target = targets[0] if targets else Target(machine=host)
                argv = render_argv(profile, host, target)
                result = await vm.start_miner(host, model, profile, argv, row, simulated)
                commands.extend(result.get("commands") or [])
            else:
                node = V.node_address(row.ip, row.node_port)
                for target in targets:
                    argv = render_argv(profile, target.machine, target, vm_node=node)
                    result = await backend.start(target.machine, profile, argv, simulated)
                    commands.extend(result.get("commands") or [])
        except Exception as exc:
            log_event(db, campaign, "campaign-started", str(exc)[:800], result="fail",
                      simulated=simulated)
            return _fail(db, campaign, "campaign-start-failed", f"Démarrage impossible : {exc}",
                         simulated)
        where = "dans la VM" if mode == "vm" else f"sur l'hôte (nœud {row.ip}:{row.node_port})"
        log_event(
            db, campaign, "campaign-started",
            f"profil « {profile['name']} » démarré {where}",
            simulated=simulated,
            detail={"commands": commands, "mode": mode, "vm": row.name, "hypervisor": vm.kind},
        )
        db.commit()
    return None


async def _spec_stop_steps(
    db: Any,
    campaign: Any,
    backend: Any,
    simulated: bool,
    mode: str,
    profile: dict[str, Any] | None,
    targets: list[Target],
) -> None:
    """Arrêt du mineur, puis **VM éteinte et conservée** (destruction séparée)."""
    row = _spec_vm_row(db, campaign)
    spec, _error = _spec_context(db, backend)
    if row is None or spec is None or profile is None:
        log_event(db, campaign, "campaign-stopped",
                  "VM de campagne introuvable : arrêt du mineur non nécessaire",
                  simulated=simulated)
        return
    host, model, vm = spec.host, spec.model, spec.backend

    commands: list[str] = []
    try:
        if mode == "vm":
            result = await vm.stop_miner(host, model, profile, row, simulated)
            commands.extend(result.get("commands") or [])
        else:
            for target in targets:
                result = await backend.stop(target.machine, profile, simulated)
                commands.extend(result.get("commands") or [])
    except Exception as exc:
        log_event(db, campaign, "campaign-stopped", str(exc)[:400], result="fail",
                  simulated=simulated)
    log_event(
        db, campaign, "campaign-stopped",
        f"mineur de campagne « {profile['name']} » arrêté ({mode})",
        simulated=simulated, detail={"commands": commands},
    )
    db.commit()

    if not _step_done(db, campaign, "vm-stopped"):
        try:
            await vm.shutdown(host, model, row, simulated)
            if not simulated:
                row.state = "shut off"
            db.commit()
        except Exception as exc:
            log_event(db, campaign, "vm-stopped", str(exc)[:400], result="fail", simulated=simulated)
        log_event(
            db, campaign, "vm-stopped",
            f"VM {row.name} éteinte et conservée (le wallet y est) — "
            "sa destruction est une action séparée, après contrôle de sauvegarde",
            simulated=simulated,
            detail={"vm": row.name, "kept": True},
        )
        db.commit()


def default_profile_for(db: Any, machine: Any):
    from app.models import MinerProfile
    from app.routers.miners import default_profile_id

    profile_id = getattr(machine, "default_miner_profile_id", None) or default_profile_id(db)
    if not profile_id:
        return None
    return db.query(MinerProfile).filter(MinerProfile.id == profile_id).first()


# ── Backend d'exécution ───────────────────────────────────────────────
class MinerBackend:
    """Appels réels : ``miner_remote`` (SSH épinglé, mode host)."""

    async def start(self, machine, profile: dict, argv: list[str], simulated: bool) -> dict:
        from app import miner_remote as R

        return await R.start(machine, profile, argv, simulated=simulated, user=R.service_user(machine))

    async def stop(self, machine, profile: dict, simulated: bool) -> dict:
        from app import miner_remote as R

        return await R.stop(machine, profile, simulated=simulated, user=R.service_user(machine))

    async def hashrate(self, machine, profile: dict) -> float | None:
        from app import miner_remote as R

        try:
            result = await R.status(machine, profile, simulated=False, user=R.service_user(machine))
        except Exception:  # machine muette : la vérification échouera par échéance
            return None
        return (result.get("status") or {}).get("hashrate_hs")

    # ── Modes `vm` et `hybrid` (§4.6) ─────────────────────────────────
    def spec_backend(self, db: Any):
        """Fabrique : hôte Proxmox → pilote Proxmox, sinon libvirt."""
        from app import vm_backend as VB

        return VB.backend_for(db)


# ── Réglages et journal ───────────────────────────────────────────────
def simulation_enabled(db: Any) -> bool:
    from app.routers.miners import simulation_enabled as _sim

    return _sim(db)


def restore_timeout(db: Any) -> int:
    from app.ai_config import get_setting

    value = get_setting(db, RESTORE_TIMEOUT_KEY, None)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return C.DEFAULT_RESTORE_TIMEOUT_S
    return number if number > 0 else C.DEFAULT_RESTORE_TIMEOUT_S


def hiveos_ack(db: Any) -> bool:
    """Option explicite « arrêter le watchdog HiveOS » (§3e), validée par Fred."""
    from app.ai_config import get_setting

    return bool(get_setting(db, "hiveos_watchdog_ack", False))


async def hiveos_probe_data(db: Any, machine: Any, gpus: list[int], simulated: bool) -> dict:
    """Sonde HiveOS : enregistrée, sinon réelle (lecture seule), simulée sans I/O."""
    import json

    from app import hiveos as HV
    from app import hiveos_remote as HR

    raw = getattr(machine, "hiveos_json", None)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("detected"):
                return data
        except (TypeError, ValueError):
            pass
    if simulated:
        return {
            "detected": True,
            "simulated": True,
            "miner": "",
            "miner_bin": "/hive/bin/miner",
            "gpu_total": len(gpus),
            "hashrate_hs": None,
            "rig": {"watchdog": False, "maintenance": False, "keys": [], "values": {}},
        }
    data = await HR.probe(machine)
    machine.hiveos_json = json.dumps(HV.mask_probe(data), ensure_ascii=False)
    db.commit()
    return data


def log_event(
    db: Any,
    campaign: Any,
    kind: str,
    message: str = "",
    *,
    actor: str = "scheduler",
    result: str = "ok",
    simulated: bool = False,
    detail: Any = None,
) -> Any:
    from app.models import CampaignEvent

    row = CampaignEvent(
        campaign_id=campaign.id,
        actor=actor,
        kind=kind,
        message=message[:2000],
        result=result,
        simulated=bool(simulated),
        detail=None
        if detail is None
        else (detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)),
    )
    db.add(row)
    db.flush()
    return row


def _step_done(db: Any, campaign: Any, kind: str) -> bool:
    from app.models import CampaignEvent

    return (
        db.query(CampaignEvent)
        .filter(
            CampaignEvent.campaign_id == campaign.id,
            CampaignEvent.kind == kind,
            CampaignEvent.result == "ok",
        )
        .first()
        is not None
    )


def _event_exists(db: Any, campaign: Any, kind: str) -> bool:
    from app.models import CampaignEvent

    return (
        db.query(CampaignEvent)
        .filter(CampaignEvent.campaign_id == campaign.id, CampaignEvent.kind == kind)
        .first()
        is not None
    )


def notify(db: Any, event: str, message: str) -> None:
    from app import notifications

    try:
        notifications.enqueue(db, event, message)
    except Exception:  # une file en panne ne bloque jamais le pilotage
        logger.warning("notification %s non mise en file", event, exc_info=True)


def audit(db: Any, campaign: Any, action: str, result: str, output: str = "") -> None:
    from app.models import AuditLog

    db.add(
        AuditLog(
            actor="scheduler",
            action=action,
            object_ref=f"campagne #{campaign.id} {campaign.coin}",
            result=result,
            output=output[:500],
        )
    )
    db.commit()


# ── Lecture des faits ─────────────────────────────────────────────────
def busy_devices(db: Any, campaign: Any) -> list[str]:
    """Devices déjà engagés ailleurs, et devices tenus par un mineur externe."""
    from app.models import Campaign, CampaignDevice, Device

    labels: list[str] = []
    mine = [
        row.device_uuid
        for row in db.query(CampaignDevice).filter(CampaignDevice.campaign_id == campaign.id)
        if row.device_uuid
    ]
    others = (
        db.query(Campaign)
        .filter(
            Campaign.id != campaign.id,
            Campaign.status.in_(["running", "stopping"]),
        )
        .all()
    )
    other_ids = [row.id for row in others]
    if other_ids:
        taken = (
            db.query(CampaignDevice)
            .filter(CampaignDevice.campaign_id.in_(other_ids))
            .all()
        )
        for row in taken:
            if row.device_uuid and row.device_uuid in mine:
                labels.append(f"{row.label or row.device_uuid} (campagne en cours)")
    for uuid in [value for value in mine if value]:
        device = db.query(Device).filter(Device.uuid == uuid).first()
        if device is not None and device.occupied_external:
            labels.append(f"{device.model or uuid} (mineur externe, hors Fossor)")
    return labels


def machines_online(db: Any, targets: list[Target]) -> bool:
    """Faux seulement si la collecte a explicitement vu la machine hors ligne."""
    from app.models import MachineStatus

    for target in targets:
        row = (
            db.query(MachineStatus)
            .filter(MachineStatus.machine_uuid == target.machine.uuid)
            .first()
        )
        if row is not None and not row.online:
            return False
    return True


def psu_worst_pct(db: Any, targets: list[Target]) -> float | None:
    from app.routers.psu import evaluate_machine_power

    worst: float | None = None
    for target in targets:
        try:
            value = float(evaluate_machine_power(db, target.machine).get("worst_load_pct") or 0.0)
        except Exception:
            continue
        worst = value if worst is None else max(worst, value)
    return worst


def read_balance(db: Any, campaign: Any) -> dict[str, Any]:
    """Solde de pool du coin, s'il est lisible (§4.16) ; jamais inventé."""
    from app.models import PoolBalance

    rows = (
        db.query(PoolBalance)
        .filter(PoolBalance.coin_ticker == str(campaign.coin).upper())
        .all()
    )
    if not rows:
        return {"total": None, "unit": str(campaign.coin).upper(), "source": "solde de pool non lisible"}
    total = 0.0
    for row in rows:
        total += float(row.paid or 0.0) + float(row.unpaid or 0.0) + float(row.immature or 0.0)
    return {"total": round(total, 10), "unit": str(campaign.coin).upper(), "source": "pool (dernier relevé)"}


def wall_power(db: Any, campaign: Any, targets: list[Target], start: datetime, end: datetime) -> float | None:
    """Puissance moyenne **à la prise** des devices de la campagne (§4.15)."""
    from app.models import Metric
    from app.routers.psu import evaluate_machine_power

    total = 0.0
    seen = False
    for target in targets:
        try:
            wall_factor = float(evaluate_machine_power(db, target.machine).get("wall_factor") or 1.0)
        except Exception:
            wall_factor = 1.0
        query = db.query(Metric).filter(
            Metric.machine_uuid == target.machine.uuid,
            Metric.ts >= C.iso(start),
            Metric.ts <= C.iso(end),
        )
        if target.device_uuids:
            query = query.filter(Metric.device_uuid.in_(target.device_uuids))
        values = [float(row.power_w) for row in query.all() if row.power_w is not None]
        if values:
            total += sum(values) / len(values) * wall_factor
            seen = True
    return round(total, 2) if seen else None


async def measure_hashrate(
    db: Any, targets: list[Target], default_profile: dict | None, backend: Any
) -> tuple[float | None, str]:
    """Hashrate du token par défaut : d'abord **la collecte**, puis l'API du mineur."""
    from app.models import MachineStatus

    collected = 0.0
    seen = False
    for target in targets:
        row = (
            db.query(MachineStatus)
            .filter(MachineStatus.machine_uuid == target.machine.uuid)
            .first()
        )
        if row is not None and row.total_hashrate_hs:
            collected = max(collected, float(row.total_hashrate_hs))
            seen = True
    if seen and collected > 0:
        return collected, "collecte (machine_status)"
    if default_profile is None:
        return (collected if seen else None), "collecte (machine_status)"
    for target in targets:
        value = await backend.hashrate(target.machine, default_profile)
        if value and value > 0:
            return float(value), "API du mineur (relevé SSH)"
    return (collected if seen else None), "API du mineur (relevé SSH)"


# ── Étapes ────────────────────────────────────────────────────────────
async def _start(db: Any, campaign: Any, moment: datetime, backend: Any, simulated: bool) -> dict[str, Any]:
    from app import miners as M
    from app.models import MinerProfile

    targets = campaign_targets(db, campaign)
    profile_row = db.query(MinerProfile).filter(MinerProfile.id == campaign.profile_id).first()
    if profile_row is None:
        return _fail(db, campaign, "profile-missing", "Profil de la campagne introuvable", simulated)
    profile = _profile_dict(profile_row)

    profile_error = None
    try:
        M.validate_profile(profile)
    except M.ProfileError as exc:
        profile_error = str(exc)

    verdict = C.preflight(
        machine_online=machines_online(db, targets),
        profile_error=profile_error,
        busy_devices=busy_devices(db, campaign),
        psu_worst_pct=psu_worst_pct(db, targets),
        confirm_overload=bool(campaign.confirm_overload),
    )
    if not verdict["ok"]:
        messages = [item["message"] for item in verdict["blocking"]]
        for item in verdict["blocking"]:
            log_event(db, campaign, "preflight-refused", item["message"], result="fail", detail=item["code"])
        campaign.note = " ; ".join(messages)[:1000]
        campaign.status = "failed"
        campaign.ended_at = C.iso(moment)
        db.commit()
        audit(db, campaign, "campaign-refused", "fail", campaign.note)
        notify(db, "campaign_failed", f"{C.summary_line(campaign)} : {campaign.note}")
        return {"campaign": campaign.id, "action": "refused", "reasons": messages}

    # 0. Rig HiveOS : arrêt du mineur de la flight sheet, vérifié par `ps`.
    from app import hiveos as HV
    from app import hiveos_remote as HR

    if not _step_done(db, campaign, "hiveos-stopped"):
        for target in targets:
            if not HV.is_hiveos(target.machine):
                continue
            probe_data = await hiveos_probe_data(db, target.machine, target.gpus, simulated)
            reason = HV.partial_refusal(target.gpus, int(probe_data.get("gpu_total") or 0))
            if reason:
                log_event(db, campaign, "hiveos-refused", reason, result="fail", simulated=simulated)
                return _fail(db, campaign, "hiveos-partial", reason, simulated)
            try:
                verdict_h = await HR.stop(
                    target.machine, probe_data, ack_watchdog=hiveos_ack(db), simulated=simulated
                )
            except HV.HiveosError as exc:
                log_event(db, campaign, "hiveos-refused", str(exc)[:600], result="fail", simulated=simulated)
                return _fail(db, campaign, "hiveos-watchdog", str(exc), simulated)
            log_event(
                db,
                campaign,
                "hiveos-stopped",
                f"{target.machine.name} : mineur HiveOS arrêté (miner stop)"
                + (", watchdog HiveOS arrêté (wd stop)" if hiveos_ack(db) else "")
                + (" (simulé)" if simulated else ""),
                simulated=simulated,
                detail={"commands": verdict_h.get("commands") or []},
            )
            db.commit()

    # 1. Arrêt de ce que **Fossor** fait tourner sur ces devices.
    if not _step_done(db, campaign, "default-stopped"):
        commands: list[str] = []
        stopped: list[str] = []
        for target in targets:
            if HV.is_hiveos(target.machine):
                continue  # le minage de ce rig est tenu par HiveOS, pas par un profil Fossor
            default_row = default_profile_for(db, target.machine)
            if default_row is None:
                continue
            result = await backend.stop(target.machine, _profile_dict(default_row), simulated)
            commands.extend(result.get("commands") or [])
            stopped.append(f"{target.machine.name} : {default_row.name}")
        log_event(
            db,
            campaign,
            "default-stopped",
            "token par défaut arrêté" if stopped else "aucun token par défaut à arrêter",
            simulated=simulated,
            detail={"machines": stopped, "commands": commands},
        )
        db.commit()

    # 2. Démarrage du profil de la campagne (§4.6 : vm / hybrid / host).
    mode = campaign_mode(db, campaign, profile)
    if mode in ("vm", "hybrid"):
        failure = await _spec_start_steps(db, campaign, moment, backend, simulated, mode, profile, targets)
        if failure is not None:
            return failure
    elif not _step_done(db, campaign, "campaign-started"):
        commands = []
        try:
            for target in targets:
                argv = render_argv(profile, target.machine, target)
                result = await backend.start(target.machine, profile, argv, simulated)
                commands.extend(result.get("commands") or [])
        except Exception as exc:
            log_event(db, campaign, "campaign-started", str(exc)[:800], result="fail", simulated=simulated)
            return _fail(db, campaign, "campaign-start-failed", f"Démarrage impossible : {exc}", simulated)
        log_event(
            db,
            campaign,
            "campaign-started",
            f"profil « {profile['name']} » démarré sur {len(targets)} machine(s)",
            simulated=simulated,
            detail={"argv": commands},
        )
        db.commit()

    balance = read_balance(db, campaign)
    # Le délai de retour vient du réglage (§4.7 : 5 min par défaut), pas de la
    # seule valeur de colonne : une campagne créée sans réglage explicite doit
    # obéir au réglage courant.
    campaign.restore_timeout_s = restore_timeout(db)
    campaign.start_balance = balance["total"]
    campaign.balance_unit = balance["unit"]
    campaign.balance_source = balance["source"]
    C.apply_transition(campaign, "running")
    campaign.started_at = C.iso(moment)
    campaign.attempts = 0
    campaign.restore_deadline = None
    campaign.updated_at = C.iso(moment)
    log_event(db, campaign, "running", "campagne en cours" + (" (simulation)" if simulated else ""), simulated=simulated)
    db.commit()
    audit(db, campaign, "campaign-start", "simulated" if simulated else "ok", profile["name"])
    notify(db, "campaign_started", f"{C.summary_line(campaign)} — profil {profile['name']}")
    return {"campaign": campaign.id, "action": "started", "simulated": simulated}


async def _stop(db: Any, campaign: Any, moment: datetime, backend: Any, simulated: bool) -> dict[str, Any]:
    from app.models import MinerProfile

    targets = campaign_targets(db, campaign)
    profile_row = db.query(MinerProfile).filter(MinerProfile.id == campaign.profile_id).first()
    profile = _profile_dict(profile_row) if profile_row is not None else None

    mode = campaign_mode(db, campaign, profile) if profile is not None else "host"
    if mode in ("vm", "hybrid"):
        if not _step_done(db, campaign, "campaign-stopped"):
            await _spec_stop_steps(db, campaign, backend, simulated, mode, profile, targets)
    elif not _step_done(db, campaign, "campaign-stopped"):
        commands: list[str] = []
        if profile is not None:
            for target in targets:
                try:
                    result = await backend.stop(target.machine, profile, simulated)
                    commands.extend(result.get("commands") or [])
                except Exception as exc:
                    log_event(db, campaign, "campaign-stopped", str(exc)[:400], result="fail", simulated=simulated)
        log_event(
            db,
            campaign,
            "campaign-stopped",
            f"mineur de campagne « {profile['name'] if profile else '?'} » arrêté",
            simulated=simulated,
            detail={"commands": commands},
        )
        db.commit()

    C.apply_transition(campaign, "stopping")
    campaign.updated_at = C.iso(moment)
    log_event(db, campaign, "stopping", "arrêt du mineur de campagne, retour au token par défaut", simulated=simulated)
    db.commit()
    audit(db, campaign, "campaign-stop", "simulated" if simulated else "ok")
    return await _restore(db, campaign, moment, backend, simulated)


async def _restore(db: Any, campaign: Any, moment: datetime, backend: Any, simulated: bool) -> dict[str, Any]:
    targets = campaign_targets(db, campaign)
    timeout = int(campaign.restore_timeout_s or C.DEFAULT_RESTORE_TIMEOUT_S)
    defaults = {id(t.machine): default_profile_for(db, t.machine) for t in targets}
    attempt = int(campaign.attempts or 0)
    start_key = f"default-started:{attempt}"
    failed_key = f"default-start-failed:{attempt}"
    from app import hiveos as HV
    from app import hiveos_remote as HR

    hiveos_targets = [t for t in targets if HV.is_hiveos(t.machine)]
    if not any(defaults.values()) and not hiveos_targets:
        log_event(
            db,
            campaign,
            "default-none",
            "aucun token par défaut configuré : retour non vérifiable",
            simulated=simulated,
        )
        return _finish(db, campaign, moment, None, None, "aucun token par défaut", simulated)

    hiveos_verdict: dict[str, Any] | None = None
    if not _step_done(db, campaign, start_key) and not _event_exists(db, campaign, failed_key):
        commands: list[str] = []
        errors: list[str] = []
        resumed_hiveos: list[str] = []
        for target in targets:
            if HV.is_hiveos(target.machine):
                probe_data = await hiveos_probe_data(db, target.machine, target.gpus, simulated)
                try:
                    resumed = await HR.resume(
                        target.machine, probe_data, ack_watchdog=hiveos_ack(db), simulated=simulated
                    )
                except HV.HiveosError as exc:
                    errors.append(f"{target.machine.name} : {exc}")
                    continue
                commands.extend(resumed.get("commands") or [])
                resumed_hiveos.append(target.machine.name)
                if resumed.get("verified") and resumed.get("hashrate_hs"):
                    hiveos_verdict = {
                        "hashrate_hs": resumed["hashrate_hs"],
                        "source": resumed.get("source") or "last_stat.json",
                    }
                log_event(
                    db,
                    campaign,
                    f"hiveos-resumed:{attempt}",
                    f"{target.machine.name} : reprise par HiveOS (miner start)"
                    + (" (simulé)" if simulated else ""),
                    simulated=simulated,
                    detail={"commands": resumed.get("commands") or []},
                )
                continue
            default_row = defaults[id(target.machine)]
            if default_row is None:
                continue
            profile = _profile_dict(default_row)
            try:
                if simulated:
                    result = await backend.start(target.machine, profile, render_argv(profile, target.machine, target), True)
                else:
                    result = await backend.start(target.machine, profile, render_argv(profile, target.machine, target), False)
                commands.extend(result.get("commands") or [])
            except Exception as exc:
                errors.append(f"{target.machine.name} : {exc}")
        if errors:
            log_event(db, campaign, failed_key, " ; ".join(errors)[:800], result="fail", simulated=simulated)
        else:
            log_event(
                db,
                campaign,
                start_key,
                (
                    "reprise par HiveOS (miner start) : "
                    + ", ".join(resumed_hiveos)
                    if resumed_hiveos
                    else f"token par défaut relancé (tentative {attempt + 1})"
                ),
                simulated=simulated,
                detail={"commands": commands},
            )
        campaign.restore_deadline = C.iso(moment + _delta(timeout))
        campaign.updated_at = C.iso(moment)
        db.commit()
        if errors:
            return {"campaign": campaign.id, "action": "default-start-failed", "attempt": attempt, "errors": errors}

    # §3e : sur un rig HiveOS, la reprise est vérifiée sur last_stat.json —
    # inutile d'attendre une collecte Fossor qui ne voit que les mineurs lancés ici.
    if hiveos_verdict is not None:
        log_event(
            db,
            campaign,
            "restore-verified",
            f"hashrate revenu : {hiveos_verdict['hashrate_hs']:.0f} H/s "
            f"({hiveos_verdict['source']}) — reprise par HiveOS (miner start)",
            detail=hiveos_verdict,
        )
        return _finish(db, campaign, moment, True, hiveos_verdict["hashrate_hs"],
                       hiveos_verdict["source"], simulated)

    if simulated:
        log_event(
            db,
            campaign,
            "restore-verified",
            "retour au token par défaut vérifié (simulé) — hashrate non lu, simulation active",
            simulated=True,
        )
        return _finish(db, campaign, moment, True, None, "simulation", simulated)

    default_profile = next((value for value in defaults.values() if value is not None), None)
    profile = _profile_dict(default_profile) if default_profile is not None else None
    hashrate, source = await measure_hashrate(db, targets, profile, backend)
    if hashrate and hashrate > 0:
        log_event(
            db,
            campaign,
            "restore-verified",
            f"hashrate revenu : {hashrate:.0f} H/s ({source})",
            detail={"hashrate_hs": hashrate, "source": source},
        )
        return _finish(db, campaign, moment, True, hashrate, source, simulated)

    deadline = C.parse_iso(campaign.restore_deadline)
    if deadline is None or moment < deadline:
        db.commit()
        return {"campaign": campaign.id, "action": "awaiting-hashrate", "attempt": attempt}

    campaign.attempts = attempt + 1
    campaign.restore_deadline = None
    campaign.updated_at = C.iso(moment)
    log_event(
        db,
        campaign,
        f"restore-missed:{attempt}",
        f"hashrate non revenu après {timeout} s (tentative {attempt + 1})",
        result="fail",
    )
    db.commit()
    if campaign.attempts > C.MAX_RESTORE_ATTEMPTS:
        log_event(
            db,
            campaign,
            "restore-failed",
            f"retour au token par défaut échoué après {campaign.attempts} essais",
            result="fail",
        )
        campaign.status = "failed"
        campaign.verified = False
        campaign.ended_at = C.iso(moment)
        campaign.note = "retour au token par défaut non vérifié"
        db.commit()
        audit(db, campaign, "campaign-restore", "fail", campaign.note)
        notify(
            db,
            "default_miner_restore_failed",
            f"{C.summary_line(campaign)} : le token par défaut n'est pas revenu — intervention requise",
        )
        notify(db, "campaign_failed", f"{C.summary_line(campaign)} : {campaign.note}")
        return {"campaign": campaign.id, "action": "failed", "attempts": campaign.attempts}

    notify(
        db,
        "default_miner_restored",
        f"{C.summary_line(campaign)} : nouvelle tentative de retour au token par défaut "
        f"({campaign.attempts}/{C.MAX_RESTORE_ATTEMPTS})",
    )
    return {"campaign": campaign.id, "action": "retry", "attempt": campaign.attempts}


def _delta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def _finish(
    db: Any,
    campaign: Any,
    moment: datetime,
    verified: bool | None,
    hashrate: float | None,
    source: str,
    simulated: bool = False,
) -> dict[str, Any]:
    from app import energy

    targets = campaign_targets(db, campaign)
    balance = read_balance(db, campaign)
    campaign.end_balance = balance["total"]
    campaign.gain = C.campaign_gain(campaign.start_balance, campaign.end_balance)
    started = C.parse_iso(campaign.started_at) or C.parse_iso(campaign.start_at) or moment
    hours = C.hours_between(started, moment)
    power = wall_power(db, campaign, targets, started, moment)
    try:
        tariff = energy.price_at(moment, energy.get_tariff(db))
    except Exception:
        tariff = None
    campaign.power_w = power
    campaign.tariff_eur_kwh = round(float(tariff), 4) if tariff is not None else None
    campaign.cost_eur = C.campaign_cost_eur(power, hours, tariff)
    campaign.verified = verified
    campaign.ended_at = C.iso(moment)
    campaign.status = "done"
    campaign.note = (
        f"retour vérifié ({source})" if verified else
        ("retour non vérifiable" if verified is None else "retour non vérifié")
    )
    log_event(
        db,
        campaign,
        "done",
        (
            f"campagne terminée — gain {campaign.gain if campaign.gain is not None else 'inconnu'}"
            f" {campaign.balance_unit}, coût "
            f"{campaign.cost_eur if campaign.cost_eur is not None else 'inconnu'} €"
            f"{' (simulé)' if simulated else ''}"
        ),
        simulated=simulated,
        detail={"hours": round(hours, 4), "power_w": power, "tariff_eur_kwh": campaign.tariff_eur_kwh,
                "hashrate_hs": hashrate, "source": source},
    )
    db.commit()
    audit(db, campaign, "campaign-done", "simulated" if simulated else "ok", campaign.note or "")
    notify(db, "campaign_finished", f"{C.summary_line(campaign)} — {campaign.note}")
    return {"campaign": campaign.id, "action": "done", "verified": verified, "hashrate_hs": hashrate}


def _fail(db: Any, campaign: Any, kind: str, message: str, simulated: bool) -> dict[str, Any]:
    log_event(db, campaign, kind, message, result="fail", simulated=simulated)
    campaign.status = "failed"
    campaign.note = message[:1000]
    campaign.ended_at = C.iso(C.now_utc())
    db.commit()
    audit(db, campaign, "campaign-failed", "fail", message)
    notify(db, "campaign_failed", f"{C.summary_line(campaign)} : {message}")
    return {"campaign": campaign.id, "action": "failed", "message": message}


# ── Boucle et ticks ───────────────────────────────────────────────────
def due_campaigns(db: Any, moment: datetime) -> list[Any]:
    from app.models import Campaign

    rows = (
        db.query(Campaign)
        .filter(Campaign.status.in_(list(ACTIVE_STATUSES)))
        .order_by(Campaign.id.asc())
        .all()
    )
    due: list[Any] = []
    for campaign in rows:
        if campaign.status == "approved":
            start = C.parse_iso(campaign.start_at)
            if start is not None and start <= moment:
                due.append(campaign)
        elif campaign.status == "running":
            if campaign.stop_requested:
                due.append(campaign)
                continue
            end = C.parse_iso(campaign.end_at)
            if end is not None and end <= moment:
                due.append(campaign)
        elif campaign.status == "stopping":
            due.append(campaign)
    return due


async def tick(
    db: Any,
    *,
    now: datetime | None = None,
    backend: Any = None,
    simulated: bool | None = None,
    timeout_s: int | None = None,
) -> list[dict[str, Any]]:
    """Avance toutes les campagnes dues ; rend la liste des actions menées."""
    global _LOCK

    moment = now or C.now_utc()
    backend = backend or MinerBackend()
    sim = simulation_enabled(db) if simulated is None else bool(simulated)
    results: list[dict[str, Any]] = []
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    async with _LOCK:
        for campaign in due_campaigns(db, moment):
            if timeout_s:
                campaign.restore_timeout_s = int(timeout_s)
            try:
                if campaign.status == "approved":
                    results.append(await _start(db, campaign, moment, backend, sim))
                elif campaign.status == "running":
                    results.append(await _stop(db, campaign, moment, backend, sim))
                else:
                    results.append(await _restore(db, campaign, moment, backend, sim))
            except Exception as exc:  # une campagne en échec ne bloque pas les autres
                db.rollback()
                logger.warning("campagne #%s : %s", campaign.id, exc, exc_info=True)
                results.append({"campaign": campaign.id, "action": "error", "message": str(exc)})
    return results


# ── Boucle de fond (même processus) ───────────────────────────────────
async def _loop() -> None:
    from app.database import SessionLocal

    while True:
        db = SessionLocal()
        try:
            await tick(db)
        except Exception:  # pragma: no cover - défensif
            logger.warning("boucle des campagnes en échec", exc_info=True)
        finally:
            db.close()
        await asyncio.sleep(LOOP_INTERVAL)


def start() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.create_task(_loop())
    except RuntimeError:  # pragma: no cover - aucun boucle au moment de l'import
        _task = None


def stop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
