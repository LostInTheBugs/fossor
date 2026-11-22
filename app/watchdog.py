"""§10.1 — Watchdog configurable du parc.

Surveille **par machine et par device**, à chaque collecte :

* hashrate sous ``pct`` % de la référence (médiane glissante 24 h) pendant N min ;
* aucune share acceptée pendant N min ;
* température GPU/CPU au-dessus du seuil pendant N min ;
* mineur attendu absent (unité ``fossor-miner-*`` ou processus introuvable) ;
* machine injoignable (collecte en échec) pendant N min.

Actions graduées, chacune activable : relancer le mineur, relancer la VM,
redémarrer la machine, power cycle (§4.17), notifier.  Une étape n'est franchie
qu'après ``escalation_minutes`` sans rétablissement.

Garde-fous (tous testés) : grâce de 10 min, 3 relances par heure et par cible,
jamais d'action sur un mineur externe ni sur un ASIC, jamais pendant une
campagne en bascule, simulation → action simulée, reboot et power cycle
subordonnés à un mot ``VALIDER`` par machine.

Le module est volontairement séparé du collecteur : la décision et les
garde-fous sont purs ou injectables, l'exécution SSH est le seul effet de bord.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

SETTING_KEY = "watchdog_config"
CONFIRM_WORD = "VALIDER"

#: Un nom de VM est un identifiant, jamais du texte libre : ``virsh``
#: reçoit ce nom tel quel dans une ligne de commande SSH.
_SAFE_VM_NAME = re.compile(r"^fossor-[A-Za-z0-9._-]+$")

RULE_NAMES = ("hashrate_low", "no_shares", "temperature", "miner_absent", "unreachable")
ACTION_NAMES = ("notify", "restart_miner", "restart_vm", "reboot", "power_cycle")

#: Actions qui comptent dans la limite horaire (notifier n'est pas une relance).
COUNTED_ACTIONS = ("restart_miner", "restart_vm", "reboot", "power_cycle")

#: Valeurs par défaut du brief §10.1, livrées avec « notifier » + « relancer
#: le mineur » seulement ; reboot et power cycle sont désactivés.
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "rules": {
        "hashrate_low": {"enabled": True, "pct": 50.0, "minutes": 10},
        "no_shares": {"enabled": True, "minutes": 20},
        "temperature": {"enabled": True, "gpu_c": 85.0, "cpu_c": 90.0, "minutes": 5},
        "miner_absent": {"enabled": True},
        "unreachable": {"enabled": True, "minutes": 10},
    },
    "actions": {
        "notify": True,
        "restart_miner": True,
        "restart_vm": True,
        "reboot": False,
        "power_cycle": False,
    },
    "escalation_minutes": 5,
    "grace_minutes": 10,
    "max_per_hour": 3,
    #: Surcharge par machine : {"<id>": {"rules": {...}, "actions": {...}, ...}}
    "machines": {},
}


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _number(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(minimum, number)


def _merge(base: dict[str, Any], override: Any) -> dict[str, Any]:
    """Fusion récursive d'une surcharge dans les valeurs par défaut."""
    result = _copy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if key not in result:
            continue
        if key == "machines" and isinstance(value, dict):
            # Surcharges indexées par identifiant de machine : la fusion
            # récursive les écarterait (clés absentes des défauts).
            result[key] = _copy(value)
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        elif isinstance(result[key], bool):
            result[key] = bool(value)
        elif isinstance(result[key], (int, float)) and not isinstance(value, bool):
            result[key] = _number(value, result[key], 0.0)
        else:
            result[key] = value
    return result


def normalize(raw: Any) -> dict[str, Any]:
    """Config complète et sûre : les clés inconnues sont ignorées."""
    config = _merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})
    config["enabled"] = bool(config["enabled"])
    for rule in RULE_NAMES:
        entry = config["rules"][rule]
        entry["enabled"] = bool(entry.get("enabled", True))
        for key in ("minutes",):
            if key in entry:
                entry[key] = int(_number(entry[key], DEFAULT_CONFIG["rules"][rule].get(key, 0)))
        for key in ("pct", "gpu_c", "cpu_c"):
            if key in entry:
                entry[key] = _number(entry[key], DEFAULT_CONFIG["rules"][rule][key], 1.0)
    for action in ACTION_NAMES:
        config["actions"][action] = bool(config["actions"].get(action, False))
    config["escalation_minutes"] = int(_number(config["escalation_minutes"], 5))
    config["grace_minutes"] = int(_number(config["grace_minutes"], 10))
    config["max_per_hour"] = int(_number(config["max_per_hour"], 3, 1.0))
    machines = config.get("machines")
    if not isinstance(machines, dict):
        machines = {}
    config["machines"] = {str(key): value for key, value in machines.items() if isinstance(value, dict)}
    return config


def get_config(db: Any) -> dict[str, Any]:
    from app.ai_config import get_setting

    return normalize(get_setting(db, SETTING_KEY, None))


def save_config(db: Any, payload: Any) -> dict[str, Any]:
    """Enregistre la config globale ; les surcharges existantes sont conservées."""
    from app.ai_config import get_setting, set_setting

    current = get_config(db)
    incoming = payload if isinstance(payload, dict) else {}
    merged = _merge(current, incoming)
    if "machines" not in incoming:
        merged["machines"] = current["machines"]
    config = normalize(merged)
    set_setting(db, SETTING_KEY, config)
    db.commit()
    return config


def save_machine_override(db: Any, machine_id: int, payload: Any) -> dict[str, Any]:
    """Surcharge partielle d'une machine ; ``null``/``{}`` la supprime."""
    from app.ai_config import set_setting

    config = get_config(db)
    key = str(int(machine_id))
    if not payload:
        config["machines"].pop(key, None)
    else:
        config["machines"][key] = payload
    config = normalize(config)
    set_setting(db, SETTING_KEY, config)
    db.commit()
    return config


def machine_config(config: dict[str, Any], machine: Any) -> dict[str, Any]:
    """Config effective d'une machine : globale + surcharge."""
    override = config.get("machines", {}).get(str(getattr(machine, "id", "")))
    if not override:
        return config
    merged = _merge(config, override)
    merged["machines"] = config["machines"]
    return merged


def median(values: list[float]) -> float | None:
    """Médiane sans dépendance : None sur liste vide."""
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return None
    middle = len(clean) // 2
    if len(clean) % 2:
        return clean[middle]
    return (clean[middle - 1] + clean[middle]) / 2.0


def reference_hashrate(db: Any, device_uuid: str | None, now: datetime, *, minimum: int = 6) -> float | None:
    """Médiane glissante 24 h du même device, ``None`` si l'historique est court.

    La référence est **figée** tant qu'il n'y a pas assez d'échantillons : dans
    ce cas l'appelant enregistre la mesure courante et ne déclenche rien.
    """
    from app.models import Metric

    if not device_uuid:
        return None
    # §10.3 : pendant un boost, la référence est celle du coin boosté.
    from app import boost_runner as BR

    boost_ref = BR.reference_hashrate(db, device_uuid, now, minimum=minimum)
    if boost_ref is not None:
        return boost_ref
    since = (now - timedelta(hours=24)).isoformat()
    rows = (
        db.query(Metric.hashrate_hs)
        .filter(
            Metric.device_uuid == device_uuid,
            Metric.ts >= since,
            Metric.hashrate_hs.isnot(None),
            Metric.hashrate_hs > 0,
        )
        .all()
    )
    values = [row[0] for row in rows if row[0]]
    if len(values) < minimum:
        return None
    return median(values)


# ── Horloge et états ──────────────────────────────────────────────────
def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _state(db: Any, machine_id: int, device_id: int, rule: str) -> Any:
    from app.models import WatchdogState

    return (
        db.query(WatchdogState)
        .filter(
            WatchdogState.machine_id == machine_id,
            WatchdogState.device_id == int(device_id or 0),
            WatchdogState.rule == rule,
        )
        .first()
    )


def _drop(db: Any, state: Any) -> None:
    if state is not None:
        db.delete(state)
        db.flush()


def _journal(db: Any, machine: Any, device: Any, rule: str, measure: str,
             action: str, result: str, simulated: bool, detail: str,
             moment: datetime) -> Any:
    from app.models import WatchdogEvent

    label = ""
    if device is not None:
        label = f"{getattr(device, 'kind', '')} {getattr(device, 'model', '') or ''}".strip()
    row = WatchdogEvent(
        ts=_iso(moment),
        machine_id=getattr(machine, "id", 0),
        machine_name=getattr(machine, "name", "") or "",
        device_id=int(getattr(device, "id", 0) or 0),
        device_label=label,
        rule=rule,
        measure=measure[:300],
        action=action,
        result=result[:40],
        simulated=bool(simulated),
        detail=detail[:1000],
    )
    db.add(row)
    db.flush()
    return row


def counted_actions(db: Any, machine_id: int, device_id: int, moment: datetime, *, hours: int = 1) -> int:
    """Relances **réelles** dans l'heure, par cible (machine + device)."""
    from app.models import WatchdogEvent

    since = (moment - timedelta(hours=hours)).isoformat()
    return (
        db.query(WatchdogEvent)
        .filter(
            WatchdogEvent.machine_id == int(machine_id),
            WatchdogEvent.device_id == int(device_id or 0),
            WatchdogEvent.ts >= since,
            WatchdogEvent.action.in_(COUNTED_ACTIONS),
            WatchdogEvent.simulated.is_(False),
            WatchdogEvent.result == "ok",
        )
        .count()
    )


# ── Cibles et garde-fous ──────────────────────────────────────────────
def expected_profiles(db: Any, machine: Any, stdout: str = "") -> list[Any]:
    from app.collector import active_profiles

    return active_profiles(db, machine, stdout)


def profile_unit(profile: Any) -> str:
    from app import miners as M

    return M.unit_name({"name": getattr(profile, "name", "")})


def unit_for_device(db: Any, machine: Any, device: Any, stdout: str = "") -> str | None:
    """Unité ``fossor-miner-*`` responsable du device (profil du device)."""
    from app.collector import profile_devices

    profiles = expected_profiles(db, machine, stdout)
    if device is not None:
        for profile in profiles:
            if any(item.id == getattr(device, "id", None) for item in profile_devices(db, machine, profile)):
                return profile_unit(profile)
    return profile_unit(profiles[0]) if profiles else None


def external_target(db: Any, machine: Any, device: Any, stdout: str = "") -> str:
    """Raison pour laquelle la cible ne doit **jamais** être pilotée, sinon ``""``."""
    if str(getattr(machine, "machine_kind", "")) == "asic":
        return "ASIC : lecture seule, notification seulement"
    if device is not None and bool(getattr(device, "occupied_external", False)):
        return "device occupé par un mineur externe : notification seulement"
    if str(getattr(machine, "os_kind", "")) == "hiveos":
        return ""  # HiveOS est pilotable par « miner restart ».
    if not expected_profiles(db, machine, stdout):
        raw = getattr(machine, "external_miners", None) or ""
        if raw.strip() and raw.strip() not in ("[]", "null"):
            return "mineur externe (aucune unité Fossor) : notification seulement"
    return ""


def campaign_switching(db: Any, machine: Any) -> str:
    """Campagne en cours de bascule sur cette machine (verrou), sinon ``""``."""
    from app.models import Campaign, CampaignDevice

    # §10.3 : entrée ou sortie de boost = bascule, le watchdog se tait.
    from app import boost_runner as BR

    boost_reason = BR.switching(db, machine)
    if boost_reason:
        return boost_reason
    row = (
        db.query(Campaign)
        .join(CampaignDevice, CampaignDevice.campaign_id == Campaign.id)
        .filter(
            CampaignDevice.machine_id == machine.id,
            Campaign.status.in_(("approved", "stopping")),
        )
        .first()
    )
    if row is None:
        return ""
    return f"campagne #{row.id} en bascule ({row.status}) : aucune action"


def in_grace(machine: Any, state: Any, config: dict[str, Any], moment: datetime) -> str:
    """Délai de grâce après démarrage d'une machine ou d'une relance."""
    grace = int(config.get("grace_minutes", 10))
    last = _parse(getattr(state, "last_action_at", None)) if state is not None else None
    if last is not None and (moment - last).total_seconds() < grace * 60:
        return f"grâce de {grace} min après la dernière relance"
    return ""


def campaign_mode(db: Any, machine: Any) -> str:
    from app.models import Campaign, CampaignDevice

    row = (
        db.query(Campaign)
        .join(CampaignDevice, CampaignDevice.campaign_id == Campaign.id)
        .filter(
            CampaignDevice.machine_id == machine.id,
            Campaign.status == "running",
            Campaign.execution_mode.in_(("auto", "vm", "hybrid")),
        )
        .first()
    )
    return getattr(row, "execution_mode", "") if row is not None else ""


def choose_action(db: Any, machine: Any, device: Any, state: Any, config: dict[str, Any],
                  moment: datetime, *, stdout: str = "") -> tuple[str, str]:
    """Action suivante selon le palier atteint et les garde-fous.

    Renvoie ``(action, détail)`` ; ``notify`` est le palier terminal.
    """
    actions = config["actions"]
    reason = external_target(db, machine, device, stdout)
    if reason:
        return "notify", reason
    reason = campaign_switching(db, machine)
    if reason:
        return "notify", reason
    stage = int(getattr(state, "stage", 0) or 0)
    if stage <= 0:
        if actions.get("restart_miner", True):
            unit = unit_for_device(db, machine, device, stdout)
            if unit:
                return "restart_miner", unit
            return "notify", "aucune unité fossor-miner-* à relancer"
        return "notify", "relance du mineur désactivée"
    if stage == 1:
        if actions.get("restart_vm", False) and campaign_mode(db, machine):
            return "restart_vm", "campagne vm/hybrid en cours"
        return "notify", "relance de la VM désactivée ou sans campagne vm/hybrid"
    if stage == 2:
        if actions.get("reboot", False) and getattr(machine, "watchdog_reboot_ok", False):
            return "reboot", CONFIRM_WORD
        return "notify", "redémarrage désactivé ou non validé par machine"
    if stage == 3:
        from app.models import PowerController

        controller = (
            db.query(PowerController)
            .filter(PowerController.machine_id == machine.id, PowerController.enabled.is_(True))
            .first()
        )
        if actions.get("power_cycle", False) and getattr(machine, "watchdog_power_ok", False) and controller:
            return "power_cycle", f"contrôleur {controller.kind} #{controller.id}"
        return "notify", "power cycle désactivé, non validé ou sans contrôleur"
    return "notify", "toutes les actions ont été tentées"


# ── Exécution des actions ─────────────────────────────────────────────
async def _restart_miner(db: Any, machine: Any, device: Any, unit: str, *, simulated: bool,
                         runner: Any = None) -> dict[str, Any]:
    if str(getattr(machine, "os_kind", "")) == "hiveos":
        from app import hiveos as H
        from app import hiveos_remote

        step = H.Step("redémarrage du mineur HiveOS", ("miner", "restart"), timeout=90)
        if simulated:
            return {"ok": True, "detail": "miner restart (simulé)", "simulated": True}
        executed = await hiveos_remote._execute(machine, [step], simulated=False) if runner is None else None
        if executed is None:
            step_result = await runner(machine, ("miner", "restart"))
            if not step_result.get("ok"):
                return {"ok": False, "detail": (step_result.get("stderr") or "miner restart refusé")[:300]}
        return {"ok": True, "detail": "miner restart"}

    from app.ssh_client import ssh_run_command

    if simulated:
        return {"ok": True, "detail": f"systemctl restart {unit} (simulé)", "simulated": True}
    command = f"systemctl restart {unit}"
    if str(getattr(machine, "ssh_user", "")) != "root":
        command = f"sudo -n {command}"
    raw = await ssh_run_command(machine, command, audit_kind="watchdog-restart")
    if raw.get("ok"):
        return {"ok": True, "detail": f"systemctl restart {unit}"}
    return {"ok": False, "detail": str(raw.get("error") or "relance refusée")[:300]}


async def _restart_vm(db: Any, machine: Any, *, simulated: bool, runner: Any = None) -> dict[str, Any]:
    from app.models import Campaign, CampaignDevice, Machine, SpecVm

    campaign = (
        db.query(Campaign)
        .join(CampaignDevice, CampaignDevice.campaign_id == Campaign.id)
        .filter(
            CampaignDevice.machine_id == machine.id,
            Campaign.status == "running",
            Campaign.execution_mode.in_(("auto", "vm", "hybrid")),
        )
        .first()
    )
    if campaign is None:
        return {"ok": False, "detail": "aucune campagne vm/hybrid en cours"}
    spec = (
        db.query(SpecVm)
        .filter(SpecVm.campaign_id == campaign.id, SpecVm.destroyed_at.is_(None))
        .order_by(SpecVm.id.desc())
        .first()
    )
    if spec is None:
        return {"ok": False, "detail": "VM de la campagne introuvable"}
    host = db.get(Machine, spec.host_machine_id)
    if host is None:
        return {"ok": False, "detail": "hôte de la VM introuvable"}
    if not str(spec.name).startswith("fossor-"):
        return {"ok": False, "detail": "VM non possédée par Fossor : refus"}
    if not _SAFE_VM_NAME.fullmatch(str(spec.name)):
        return {"ok": False, "detail": "nom de VM non conforme : refus"}
    if spec.hypervisor == "pve" and spec.vmid:
        command = f"qm reboot {int(spec.vmid)}"
    else:
        command = f"virsh reboot {spec.name}"
    if simulated:
        return {"ok": True, "detail": f"{command} (simulé)", "simulated": True}
    from app.ssh_client import ssh_run_command

    raw = await ssh_run_command(host, command, audit_kind="watchdog-vm-restart")
    if raw.get("ok"):
        return {"ok": True, "detail": command}
    return {"ok": False, "detail": str(raw.get("error") or "redémarrage de la VM refusé")[:300]}


async def _reboot(machine: Any, *, simulated: bool) -> dict[str, Any]:
    if not getattr(machine, "watchdog_reboot_ok", False):
        return {"ok": False, "detail": "redémarrage non validé (mot VALIDER requis)"}
    if str(getattr(machine, "machine_kind", "")) == "asic":
        return {"ok": False, "detail": "ASIC : redémarrage interdit"}
    if simulated:
        return {"ok": True, "detail": "systemctl reboot (simulé)", "simulated": True}
    from app.ssh_client import ssh_run_command

    command = "systemctl reboot"
    if str(getattr(machine, "ssh_user", "")) != "root":
        command = f"sudo -n {command}"
    raw = await ssh_run_command(machine, command, audit_kind="watchdog-reboot")
    return {"ok": bool(raw.get("ok")), "detail": str(raw.get("error") or command)[:300]}


async def _power_cycle(db: Any, machine: Any, *, simulated: bool, now: datetime) -> dict[str, Any]:
    from app import power_control
    from app.models import PowerController

    if not getattr(machine, "watchdog_power_ok", False):
        return {"ok": False, "detail": "power cycle non validé (mot VALIDER requis)"}
    controller = (
        db.query(PowerController)
        .filter(PowerController.machine_id == machine.id, PowerController.enabled.is_(True))
        .first()
    )
    if controller is None:
        return {"ok": False, "detail": "aucun contrôleur d'alimentation configuré"}
    try:
        power_control.check_guards(db, machine, controller, "cycle", now=now)
    except power_control.PowerError as exc:
        return {"ok": False, "detail": str(exc)}
    if simulated:
        return {"ok": True, "detail": "power cycle (simulé)", "simulated": True}
    try:
        await power_control.perform(controller, "cycle")
    except Exception as exc:  # réseau du contrôleur : jamais bloquant
        return {"ok": False, "detail": f"power cycle en échec : {exc}"[:300]}
    power_control.journal(db, machine.id, "cycle", result="ok", detail="watchdog", simulated=False)
    return {"ok": True, "detail": f"power cycle via {controller.kind}"}


async def perform_action(db: Any, machine: Any, device: Any, action: str, detail: str, *,
                         simulated: bool, now: datetime, runner: Any = None) -> dict[str, Any]:
    """Exécute **une** action ; ne lève jamais : renvoie ``{ok, detail}``."""
    try:
        if action == "restart_miner":
            return await _restart_miner(db, machine, device, detail, simulated=simulated, runner=runner)
        if action == "restart_vm":
            return await _restart_vm(db, machine, simulated=simulated, runner=runner)
        if action == "reboot":
            return await _reboot(machine, simulated=simulated)
        if action == "power_cycle":
            return await _power_cycle(db, machine, simulated=simulated, now=now)
    except Exception as exc:  # une action ne casse jamais la collecte
        logger.warning("watchdog : action %s en échec: %s", action, exc)
        return {"ok": False, "detail": str(exc)[:300]}
    return {"ok": True, "detail": "notification seule", "notify_only": True}


# ── Boucle d'évaluation ───────────────────────────────────────────────
def _online_from_db(db: Any, machine: Any, moment: datetime) -> tuple[bool, str]:
    from app.models import MachineStatus

    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    ) if machine.uuid else None
    if status is None:
        return False, "aucune collecte enregistrée"
    age = (moment - _parse(status.ts)).total_seconds() if _parse(status.ts) else None
    if not status.online:
        return False, status.error or "collecte en échec"
    if age is not None and age > 3600:
        return False, f"dernière collecte il y a {int(age // 60)} min"
    return True, ""


def _devices_from_stats(db: Any, machine: Any, stats: dict[str, Any]) -> list[dict[str, Any]]:
    from app.models import Device
    from app.hw_parser import norm_bus

    devices = db.query(Device).filter(Device.machine_id == machine.id, Device.present.is_(True)).all()
    cpu = next((item for item in devices if item.kind == "cpu"), None)
    by_bus = {norm_bus(item.pci_bus): item for item in devices if item.kind == "gpu" and item.pci_bus}
    rows: list[dict[str, Any]] = []
    for item in stats.get("devices") or []:
        device = cpu if item.get("kind") == "cpu" else by_bus.get(norm_bus(item.get("pci_bus") or ""))
        if device is None:
            continue
        rows.append({
            "device": device,
            "hashrate_hs": item.get("hashrate_hs"),
            "temp_c": item.get("temp_c"),
        })
    return rows


def _devices_from_db(db: Any, machine: Any, moment: datetime) -> list[dict[str, Any]]:
    from app.models import Device, Metric

    devices = db.query(Device).filter(Device.machine_id == machine.id, Device.present.is_(True)).all()
    rows: list[dict[str, Any]] = []
    for device in devices:
        metric = (
            db.query(Metric)
            .filter(Metric.device_uuid == device.uuid)
            .order_by(Metric.id.desc())
            .first()
        )
        if metric is None:
            continue
        fresh = (_parse(metric.ts) and (moment - _parse(metric.ts)).total_seconds() <= 3600)
        if not fresh:
            continue
        rows.append({"device": device, "hashrate_hs": metric.hashrate_hs, "temp_c": metric.temp_c})
    return rows


def _notify(db: Any, machine: Any, device: Any, rule: str, measure: str, action: str,
            result: str, simulated: bool, detail: str) -> None:
    from app import notifications

    label = getattr(machine, "name", "") or f"machine {getattr(machine, 'id', '?')}"
    where = f" / {device.kind} {device.model or ''}".rstrip() if device is not None else ""
    message = (
        f"Watchdog — {label}{where} : règle {rule} ({measure}). "
        f"Action : {action} — {result}{' [simulé]' if simulated else ''}. {detail}"
    )
    try:
        notifications.enqueue(db, "watchdog_action", message[:2000],
                              level="critique" if action in ("reboot", "power_cycle") else "alerte")
    except Exception:  # un canal absent ne bloque jamais l'action
        logger.warning("watchdog : notification en échec", exc_info=True)


def _is_simulated(db: Any, machine: Any) -> bool:
    from app.pilotage import simulated_for

    return simulated_for(db, machine)


async def _apply_rule(db: Any, machine: Any, device: Any, rule: str, *, active: bool,
                      measure: str, minutes: int, config: dict[str, Any], moment: datetime,
                      stdout: str, runner: Any, events: list[dict[str, Any]]) -> None:
    """Suit une règle dans le temps et déclenche l'action au bon moment."""
    device_id = int(getattr(device, "id", 0) or 0)
    state = _state(db, machine.id, device_id, rule)
    if not active:
        _drop(db, state)
        return
    if state is None:
        from app.models import WatchdogState

        state = WatchdogState(
            machine_id=machine.id, device_id=device_id, rule=rule,
            since=_iso(moment), stage=0, last_action_at=None,
        )
        db.add(state)
        db.flush()
    since = _parse(state.since) or moment
    if (moment - since).total_seconds() < minutes * 60:
        return
    last_action = _parse(state.last_action_at)
    if last_action is not None:
        if (moment - last_action).total_seconds() < int(config["escalation_minutes"]) * 60:
            return
    grace = in_grace(machine, state, config, moment)
    if grace:
        _journal(db, machine, device, rule, measure, "none", "grace", False, grace, moment)
        return
    action, detail = choose_action(db, machine, device, state, config, moment, stdout=stdout)
    simulated = _is_simulated(db, machine)
    if _is_simulated(db, machine) and action != "notify":
        simulated = True
    blocked = counted_actions(db, machine.id, device_id, moment)
    if action in COUNTED_ACTIONS and blocked >= int(config["max_per_hour"]):
        _journal(db, machine, device, rule, measure, "notify", "blocked", simulated,
                 f"{blocked} relance(s) dans l'heure : notification seule", moment)
        _notify(db, machine, device, rule, measure, "notify", "blocked", simulated,
                "watchdog bloqué : limite de relances horaire atteinte")
        try:
            from app import notifications

            notifications.enqueue(
                db, "watchdog_blocked",
                f"Watchdog bloqué — {machine.name} : {blocked} relance(s) dans l'heure, "
                f"règle {rule} toujours en défaut ({measure}). Notification seule.",
                level="critique",
            )
        except Exception:
            logger.warning("watchdog : notification « bloqué » en échec", exc_info=True)
        state.last_action_at = _iso(moment)
        db.commit()
        events.append({"rule": rule, "action": "notify", "result": "blocked", "machine_id": machine.id})
        return
    outcome = await perform_action(db, machine, device, action, detail, simulated=simulated,
                                   now=moment, runner=runner)
    result = "ok" if outcome.get("ok") else "failed"
    _journal(db, machine, device, rule, measure, action, result, simulated,
             f"{detail} — {outcome.get('detail', '')}", moment)
    _notify(db, machine, device, rule, measure, action, result, simulated,
            str(outcome.get("detail", "")))
    if action in COUNTED_ACTIONS:
        state.stage = int(state.stage or 0) + 1
    if action in COUNTED_ACTIONS or action == "notify":
        # Une notification aussi est espacée du délai d'escalade : sans
        # cela, un mineur externe en défaut notifie à chaque collecte.
        state.last_action_at = _iso(moment)
    db.commit()
    events.append({"rule": rule, "action": action, "result": result, "machine_id": machine.id})


async def evaluate_machine(db: Any, machine: Any, *, now: datetime | None = None,
                           stats: dict[str, Any] | None = None, offline: str | None = None,
                           raw_stdout: str | None = None,
                           runner: Any = None) -> list[dict[str, Any]]:
    """Évalue toutes les règles d'une machine ; renvoie les déclenchements."""
    from app.models import MachineStatus

    moment = _now(now)
    config = machine_config(get_config(db), machine)
    if not config["enabled"]:
        return []
    events: list[dict[str, Any]] = []
    stdout = raw_stdout if raw_stdout is not None else ""
    if stats is not None:
        online, error = True, ""
    elif offline is not None:
        online, error = False, offline
    else:
        online, error = _online_from_db(db, machine, moment)

    if not online:
        if config["rules"]["unreachable"]["enabled"]:
            await _apply_rule(db, machine, None, "unreachable", active=True, measure=error,
                              minutes=int(config["rules"]["unreachable"]["minutes"]),
                              config=config, moment=moment, stdout=stdout, runner=runner,
                              events=events)
        db.commit()
        return events
    # Machine de nouveau joignable : le délai « injoignable » repart de zéro.
    _drop(db, _state(db, machine.id, 0, "unreachable"))

    # Shares : compteur cumulé de la machine, lu dans machine_status.
    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    ) if machine.uuid else None
    accepted = getattr(status, "accepted", None) if status is not None else None
    if not config["rules"]["no_shares"]["enabled"]:
        accepted = None
    state = _state(db, machine.id, 0, "no_shares")
    if accepted is None:
        _drop(db, state)
    else:
        if state is None:
            from app.models import WatchdogState

            state = WatchdogState(machine_id=machine.id, device_id=0, rule="no_shares",
                                  since=_iso(moment), stage=0, counter=int(accepted),
                                  counter_at=_iso(moment))
            db.add(state)
            db.flush()
        elif state.counter is None or int(accepted) < int(state.counter):
            state.counter = int(accepted)
            state.counter_at = _iso(moment)
            state.since = _iso(moment)
        elif int(accepted) > int(state.counter):
            state.counter = int(accepted)
            state.counter_at = _iso(moment)
            state.since = _iso(moment)
            state.stage = 0
            state.last_action_at = None
    if accepted is not None and state is not None and state.counter is not None:
        stuck_since = _parse(state.counter_at) or moment
        active = (moment - stuck_since).total_seconds() >= int(config["rules"]["no_shares"]["minutes"]) * 60
        if active:
            await _apply_rule(db, machine, None, "no_shares", active=True,
                              measure=f"0 share acceptée depuis {int((moment - stuck_since).total_seconds() // 60)} min",
                              minutes=0, config=config, moment=moment, stdout=stdout,
                              runner=runner, events=events)

    # Devices : hashrate et température.
    if stats is not None:
        observations = _devices_from_stats(db, machine, stats)
    else:
        observations = _devices_from_db(db, machine, moment)
    for item in observations:
        device = item["device"]
        hashrate = item.get("hashrate_hs")
        if config["rules"]["hashrate_low"]["enabled"]:
            reference = reference_hashrate(db, device.uuid, moment)
            state = _state(db, machine.id, device.id, "hashrate_low")
            if reference is None:
                # Historique trop court : la mesure courante devient la
                # référence figée, et aucune alerte n'est émise.
                if state is None:
                    from app.models import WatchdogState

                    db.add(WatchdogState(
                        machine_id=machine.id, device_id=device.id, rule="hashrate_low",
                        since=_iso(moment), stage=0,
                        reference_hs=float(hashrate) if hashrate else None,
                    ))
                    db.flush()
                continue
            if state is not None and state.reference_hs is not None:
                state.reference_hs = None
            limit = reference * float(config["rules"]["hashrate_low"]["pct"]) / 100.0
            active = hashrate is not None and float(hashrate) < limit
            measure = f"{hashrate or 0:.0f} H/s < {limit:.0f} H/s ({config['rules']['hashrate_low']['pct']:.0f} % de {reference:.0f})"
            await _apply_rule(db, machine, device, "hashrate_low", active=active, measure=measure,
                              minutes=int(config["rules"]["hashrate_low"]["minutes"]),
                              config=config, moment=moment, stdout=stdout, runner=runner,
                              events=events)
        if config["rules"]["temperature"]["enabled"] and item.get("temp_c") is not None:
            threshold = (config["rules"]["temperature"]["cpu_c"] if device.kind == "cpu"
                         else config["rules"]["temperature"]["gpu_c"])
            active = float(item["temp_c"]) > float(threshold)
            await _apply_rule(db, machine, device, "temperature", active=active,
                              measure=f"{float(item['temp_c']):.0f} °C > {float(threshold):.0f} °C",
                              minutes=int(config["rules"]["temperature"]["minutes"]),
                              config=config, moment=moment, stdout=stdout, runner=runner,
                              events=events)

    # Mineur attendu absent : seulement quand la collecte a réellement parlé.
    if config["rules"]["miner_absent"]["enabled"] and stdout:
        from app.collector import profile_devices, profile_unit_seen

        for profile in expected_profiles(db, machine, stdout):
            seen = profile_unit_seen(profile, stdout)
            owned = profile_devices(db, machine, profile)
            device = owned[0] if owned else None
            await _apply_rule(db, machine, device, "miner_absent", active=not seen,
                              measure=f"unité {profile_unit(profile)} absente" if not seen else "",
                              minutes=0, config=config, moment=moment, stdout=stdout,
                              runner=runner, events=events)
    db.commit()
    return events
