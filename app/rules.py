"""Règles d'analyse des tokens (§4.8) et validation des propositions (§5.4).

Une règle = ``id`` stable, un texte, un interrupteur, un ``scope`` et des
``params`` **validés par un schéma pydantic** (:class:`~pydantic.BaseModel`).
Une valeur invalide est refusée avant toute écriture, avec le champ fautif.

Les propositions de Hermes ne sont jamais lancées : Fossor les valide contre
ces règles (rôle des devices §4.19, profil, fenêtre, fraîcheur, part réseau,
parallélisme) puis les dépose au statut ``proposed`` — Fred approuve.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.campaigns import CampaignError, iso, parse_iso, validate_window
from app.pilotage import asic_refusal, is_asic
from app.models import (
    Campaign,
    CampaignDevice,
    Coin,
    Device,
    Machine,
    MinerProfile,
    Rule,
)

SCOPES = ("selection", "launch", "duration", "stop", "pool", "notify")
ACTIVE_STATUSES = ("approved", "running", "stopping")


class RuleError(ValueError):
    """Paramètres ou proposition invalides — message affichable tel quel."""


# ── Schémas de paramètres, un par règle ───────────────────────────────
class FreshnessParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_hours: int = Field(0, ge=0, le=720, description="Âge minimal d'un token (heures)")
    max_hours: int = Field(72, ge=1, le=720, description="Fenêtre de fraîcheur (heures)")
    watch_days: int = Field(7, ge=1, le=30, description="Surveillance au-delà (jours)")

    @model_validator(mode="after")
    def _ordered(self) -> "FreshnessParams":
        if self.max_hours < self.min_hours:
            raise ValueError("max_hours doit être supérieur ou égal à min_hours")
        return self


class ExcludeAsicOnlyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude: bool = Field(True, description="Écarter un token minable uniquement en ASIC")


class CpuOnlyVmParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: bool = Field(True, description="Autoriser le spec mining CPU-only en VM")
    min_vcpus: int = Field(1, ge=1, le=64, description="vCPU minimum pour une VM de spec CPU")


class AutoLaunchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gpu: bool = Field(False, description="Lancer une campagne GPU dès qu'un token est retenu")
    cpu: bool = Field(False, description="Lancer une campagne CPU dès qu'un token est retenu")


class GpuDurationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: int = Field(96, ge=1, le=720, description="Durée d'une spec GPU (heures)")
    extend_hours: int = Field(96, ge=1, le=720, description="Prolongation si « la sauce prend »")
    adaptive: bool = Field(True, description="Fin adaptative (prolonger / arrêter avant terme)")


class CpuDurationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unlimited: bool = Field(True, description="Durée spec CPU sans limite")
    hours: int | None = Field(None, ge=1, le=8760, description="Durée si non illimitée (heures)")

    @model_validator(mode="after")
    def _coherent(self) -> "CpuDurationParams":
        if not self.unlimited and self.hours is None:
            raise ValueError("hours est requis quand unlimited est faux")
        return self


class ParallelCpuGpuParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: bool = Field(True, description="Spec CPU et spec GPU en parallèle")


class SoloToPoolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_solo_days: float = Field(14, gt=0, le=365, description="Espérance solo au-delà de laquelle passer en pool (jours)")
    require_known_pool: bool = Field(True, description="Exiger un pool public reconnu")


class MaxNetworkShareParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_pct: float = Field(10, gt=0, le=100, description="Part réseau maximale (%)")


class ElectricityCostParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eur_per_kwh: float = Field(0.20, ge=0, le=5, description="Coût de l'électricité (€/kWh)")
    link_tariff: bool = Field(True, description="Suivre le tarif configuré (§4.15) s'il existe")


class NotificationsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_switch: bool = Field(True, description="Notifier une bascule de token")
    on_extend: bool = Field(True, description="Notifier une prolongation")
    on_end: bool = Field(True, description="Notifier la fin d'une campagne")


class WatchCadenceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: int = Field(6, ge=1, le=168, description="Cadence de la veille (heures)")


#: Les règles par défaut, reprises du tableau §4.8 (skill ``spec-mining``).
RULE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "freshness",
        "name": "Fenêtre de fraîcheur",
        "description": "0 à 72 h ; « surveillance » jusqu'à 7 jours si des signaux existent.",
        "scope": "selection",
        "enabled": True,
        "model": FreshnessParams,
    },
    {
        "id": "exclude_asic_only",
        "name": "Exclure ASIC-only",
        "description": "Un token minable uniquement en ASIC n'est pas candidat.",
        "scope": "selection",
        "enabled": True,
        "model": ExcludeAsicOnlyParams,
    },
    {
        "id": "cpu_only_vm",
        "name": "CPU-only minable (VM)",
        "description": "Un token CPU-only reste minable dans une VM jetable.",
        "scope": "selection",
        "enabled": True,
        "model": CpuOnlyVmParams,
    },
    {
        "id": "auto_launch",
        "name": "Lancement auto si « retenu »",
        "description": "GPU et CPU séparément, désactivés par défaut. Actif : une proposition "
        "validée est lancée sans approbation sur des devices autorisés, sauf machine en simulation.",
        "scope": "launch",
        "enabled": False,
        "model": AutoLaunchParams,
    },
    {
        "id": "gpu_duration",
        "name": "Durée spec GPU",
        "description": "96 h, fin adaptative : +96 h si « la sauce prend », arrêt anticipé sinon.",
        "scope": "duration",
        "enabled": True,
        "model": GpuDurationParams,
    },
    {
        "id": "cpu_duration",
        "name": "Durée spec CPU",
        "description": "Sans limite par défaut.",
        "scope": "duration",
        "enabled": True,
        "model": CpuDurationParams,
    },
    {
        "id": "parallel_cpu_gpu",
        "name": "CPU et GPU en parallèle",
        "description": "Une spec CPU et une spec GPU peuvent tourner en même temps.",
        "scope": "launch",
        "enabled": True,
        "model": ParallelCpuGpuParams,
    },
    {
        "id": "solo_to_pool",
        "name": "Solo → pool",
        "description": "Proposer le pool si l'espérance solo dépasse 14 jours et qu'un pool public est reconnu.",
        "scope": "pool",
        "enabled": True,
        "model": SoloToPoolParams,
    },
    {
        "id": "max_network_share",
        "name": "Part réseau max",
        "description": "Au-delà de 10 %, le réseau est considéré comme mort : proposition refusée.",
        "scope": "pool",
        "enabled": True,
        "model": MaxNetworkShareParams,
    },
    {
        "id": "electricity_cost",
        "name": "Coût élec",
        "description": "0,20 €/kWh, ou le tarif configuré en §4.15 s'il existe.",
        "scope": "selection",
        "enabled": True,
        "model": ElectricityCostParams,
    },
    {
        "id": "notifications",
        "name": "Notifications",
        "description": "Bascule, prolongation, fin de campagne.",
        "scope": "notify",
        "enabled": True,
        "model": NotificationsParams,
    },
    {
        "id": "watch_cadence",
        "name": "Cadence de la veille",
        "description": "Toutes les 6 h.",
        "scope": "notify",
        "enabled": True,
        "model": WatchCadenceParams,
    },
)

RULE_BY_ID = {spec["id"]: spec for spec in RULE_SPECS}


def default_params(rule_id: str) -> dict[str, Any]:
    spec = RULE_BY_ID.get(rule_id)
    if spec is None:
        raise RuleError(f"règle inconnue : {rule_id!r}")
    return spec["model"]().model_dump()


def seed_rules(db: Session) -> int:
    """Crée les règles manquantes ; ne touche jamais à une règle existante."""
    created = 0
    for order, spec in enumerate(RULE_SPECS):
        if db.get(Rule, spec["id"]) is not None:
            continue
        db.add(
            Rule(
                id=spec["id"],
                name=spec["name"],
                description=spec["description"],
                enabled=bool(spec["enabled"]),
                scope=spec["scope"],
                params=default_params(spec["id"]),
                sort_order=order,
            )
        )
        created += 1
    if created:
        db.commit()
    return created


def validate_params(rule_id: str, raw: Any) -> dict[str, Any]:
    """Valide ``params`` contre le schéma de la règle ; lève :class:`RuleError`."""
    spec = RULE_BY_ID.get(rule_id)
    if spec is None:
        raise RuleError(f"règle inconnue : {rule_id!r}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuleError(f"params de la règle {rule_id!r} : objet JSON attendu")
    try:
        model = spec["model"](**raw)
    except ValidationError as exc:
        details = []
        for err in exc.errors():
            field = ".".join(str(part) for part in err.get("loc", ())) or "params"
            details.append(f"{field} : {err.get('msg', 'valeur invalide')}")
        raise RuleError(
            f"params invalides pour la règle {rule_id!r} — " + "; ".join(details)
        ) from exc
    return model.model_dump()


def schema_of(rule_id: str) -> dict[str, Any]:
    spec = RULE_BY_ID.get(rule_id)
    if spec is None:
        raise RuleError(f"règle inconnue : {rule_id!r}")
    return spec["model"].model_json_schema()


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "enabled": bool(rule.enabled),
        "scope": rule.scope,
        "params": rule.params or {},
        "sort_order": rule.sort_order,
        "updated_at": rule.updated_at,
        "schema": schema_of(rule.id),
        "stage": "4b" if rule.id == "auto_launch" else None,
    }


def plan_auto_launch(db: Session, kind: str, simulated: bool) -> dict[str, Any]:
    """Décide d'un lancement automatique, **séparément** pour GPU et CPU.

    * règle désactivée ou drapeau du type absent → aucun lancement ;
    * simulation active → lancement simulé, jamais exécuté ;
    * sinon → lancement autorisé (l'appelant approuve et démarre).
    """
    rule = db.get(Rule, "auto_launch")
    if rule is None or not rule.enabled:
        return {"allowed": False, "launch": False, "simulated": bool(simulated), "reason": "règle « auto_launch » désactivée"}
    params = rule.params or {}
    flag = bool(params.get("gpu" if kind == "gpu" else "cpu", False)) if kind in ("gpu", "cpu") else False
    if not flag:
        return {"allowed": False, "launch": False, "simulated": bool(simulated), "reason": f"lancement auto {kind or 'inconnu'} désactivé"}
    if simulated:
        return {"allowed": True, "launch": False, "simulated": True, "reason": "simulation activée : lancement simulé"}
    return {"allowed": True, "launch": True, "simulated": False, "reason": ""}


def enabled_rules(db: Session) -> dict[str, Rule]:
    return {rule.id: rule for rule in db.query(Rule).filter(Rule.enabled.is_(True)).all()}


def _param(rules: dict[str, Rule], rule_id: str, key: str, fallback: Any) -> Any:
    rule = rules.get(rule_id)
    if rule is None:
        return fallback
    return (rule.params or {}).get(key, fallback)


def _reason(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


# ── Proposition de campagne (§5.4) ────────────────────────────────────
class ProposalCandidate(BaseModel):
    """Faits que Hermes avance sur le candidat ; Fossor les confronte aux règles."""

    model_config = ConfigDict(extra="forbid")

    age_hours: float | None = Field(None, ge=0, le=100000)
    network_share_pct: float | None = Field(None, ge=0, le=100)
    kind: Literal["cpu", "gpu", "both"] | None = None


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: int | None = None
    coin: str = Field(min_length=1, max_length=20)
    profile_id: int
    device_uuids: list[str] = Field(min_length=1, max_length=64)
    start_at: str
    end_at: str | None = None
    execution_mode: Literal["host", "vm", "hybrid"] = "host"
    note: str = Field("", max_length=2000)
    candidate: ProposalCandidate = Field(default_factory=lambda: ProposalCandidate())


def _device_label(device: Device) -> str:
    parts = [part for part in (device.vendor, device.model) if part]
    return " ".join(parts) or f"{device.kind} #{device.index if device.index is not None else '?'}"


def _busy_uuids(db: Session) -> set[str]:
    rows = (
        db.query(CampaignDevice.device_uuid)
        .join(Campaign, Campaign.id == CampaignDevice.campaign_id)
        .filter(Campaign.status.in_(("proposed",) + ACTIVE_STATUSES))
        .all()
    )
    busy: set[str] = set()
    for row in rows:
        if row[0]:
            busy.add(str(row[0]))
    return busy


def free_capacity(db: Session) -> dict[str, Any]:
    """Devices autorisés au spec mining **et** libres, CPU/GPU (§4.19)."""
    busy = _busy_uuids(db)
    devices = (
        db.query(Device)
        .filter(
            Device.present.is_(True),
            Device.enabled_for_mining.is_(True),
            Device.role == "default_and_spec",
        )
        .order_by(Device.kind, Device.machine_id, Device.index)
        .all()
    )
    machines = {
        machine.id: machine
        for machine in db.query(Machine).all()
        if not is_asic(machine)
    }
    out: dict[str, list[dict[str, Any]]] = {"cpu": [], "gpu": []}
    for device in devices:
        if device.uuid in busy:
            continue
        machine = machines.get(device.machine_id)
        if machine is None:
            continue
        out.setdefault(device.kind, []).append(
            {
                "uuid": device.uuid,
                "kind": device.kind,
                "label": _device_label(device),
                "machine": machine.name if machine else "",
                "threads": device.threads,
                "vram_mb": device.vram_mb,
            }
        )
    return {
        "cpu": out.get("cpu", []),
        "gpu": out.get("gpu", []),
        "counts": {"cpu": len(out.get("cpu", [])), "gpu": len(out.get("gpu", []))},
    }


def validate_proposal(db: Session, data: ProposalIn) -> dict[str, Any]:
    """Confronte une proposition aux règles actives ; renvoie ``{ok, reasons}``."""
    rules = enabled_rules(db)
    reasons: list[dict[str, str]] = []

    coin = data.coin.strip().upper()
    profile = db.get(MinerProfile, data.profile_id)
    if profile is None:
        reasons.append(_reason("profile_missing", f"Profil #{data.profile_id} inexistant"))
    elif profile.coin.strip().upper() != coin:
        reasons.append(
            _reason(
                "profile_coin_mismatch",
                f"Le profil #{profile.id} « {profile.name} » mine {profile.coin}, pas {coin}",
            )
        )
    elif not profile.enabled:
        reasons.append(_reason("profile_disabled", f"Profil #{profile.id} désactivé"))

    devices: list[Device] = []
    for device_uuid in data.device_uuids:
        device = db.query(Device).filter(Device.uuid == device_uuid).first()
        if device is None:
            reasons.append(_reason("device_missing", f"Device {device_uuid} inconnu"))
            continue
        devices.append(device)
        asic_machine = db.query(Machine).filter(Machine.id == device.machine_id).first()
        if asic_machine is not None and is_asic(asic_machine):
            reasons.append(_reason("asic_not_pilotable", asic_refusal(asic_machine)))
        if device.role != "default_and_spec":
            reasons.append(
                _reason(
                    "device_role_not_allowed",
                    f"Device {_device_label(device)} : rôle « {device.role} » — "
                    "seuls les devices « token par défaut + spec mining » sont autorisés (§4.19)",
                )
            )
        if not device.enabled_for_mining or not device.present:
            reasons.append(
                _reason("device_unavailable", f"Device {_device_label(device)} désactivé ou absent")
            )

    busy = _busy_uuids(db)
    for device in devices:
        if device.uuid in busy:
            reasons.append(
                _reason("device_busy", f"Device {_device_label(device)} déjà engagé dans une campagne")
            )

    kinds = {device.kind for device in devices}
    if "cpu" in kinds:
        if "cpu_only_vm" not in rules or not _param(rules, "cpu_only_vm", "allow", False):
            reasons.append(
                _reason("cpu_spec_disabled", "Spec mining CPU désactivé par la règle « CPU-only minable (VM) »")
            )
    if kinds == {"cpu", "gpu"} and not _param(rules, "parallel_cpu_gpu", "allow", False):
        reasons.append(
            _reason("parallel_disabled", "CPU et GPU ensemble alors que la règle « parallèle » est désactivée")
        )
    if "parallel_cpu_gpu" not in rules:
        kinds_active = {
            device.kind
            for device in db.query(Device).join(CampaignDevice, CampaignDevice.device_uuid == Device.uuid)
            .join(Campaign, Campaign.id == CampaignDevice.campaign_id)
            .filter(Campaign.status.in_(ACTIVE_STATUSES))
            .all()
        }
        if kinds_active and kinds and kinds_active != kinds:
            reasons.append(
                _reason("parallel_disabled", "Une campagne de l'autre type tourne et le parallélisme est désactivé")
            )

    candidate = data.candidate
    if candidate.age_hours is not None and "freshness" in rules:
        max_hours = int(_param(rules, "freshness", "max_hours", 72))
        if candidate.age_hours > max_hours:
            reasons.append(
                _reason(
                    "candidate_stale",
                    f"Candidat âgé de {candidate.age_hours:g} h — fenêtre de fraîcheur {max_hours} h",
                )
            )
    if candidate.network_share_pct is not None and "max_network_share" in rules:
        max_pct = float(_param(rules, "max_network_share", "max_pct", 10))
        if candidate.network_share_pct > max_pct:
            reasons.append(
                _reason(
                    "network_share_too_high",
                    f"Part réseau {candidate.network_share_pct:g} % > maximum {max_pct:g} %",
                )
            )

    start_at = end_at = None
    try:
        start_at, end_at = validate_window(data.start_at, data.end_at)
    except CampaignError as exc:
        reasons.append(_reason("invalid_window", str(exc)))

    if start_at and end_at and devices and kinds == {"gpu"} and "gpu_duration" in rules:
        start_moment = parse_iso(start_at)
        end_moment = parse_iso(end_at)
        if start_moment is not None and end_moment is not None:
            hours = (end_moment - start_moment).total_seconds() / 3600.0
            max_hours = int(_param(rules, "gpu_duration", "hours", 96))
            if hours > max_hours:
                reasons.append(
                    _reason(
                        "gpu_duration_too_long",
                        f"Durée {hours:.1f} h > durée spec GPU {max_hours} h",
                    )
                )
    if start_at and end_at and kinds == {"cpu"} and "cpu_duration" in rules:
        if bool(_param(rules, "cpu_duration", "unlimited", True)):
            pass  # sans limite : aucune contrainte de fin

    if data.analysis_id is not None:
        from app.models import Analysis

        if db.get(Analysis, data.analysis_id) is None:
            reasons.append(_reason("analysis_missing", f"Analyse #{data.analysis_id} inexistante"))

    payload = data.model_dump()
    payload["coin"] = coin
    payload["start_at"] = start_at
    payload["end_at"] = end_at
    return {"ok": not reasons, "reasons": reasons, "payload": payload}


def is_active(campaign: Campaign) -> bool:
    return campaign.status in ACTIVE_STATUSES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
