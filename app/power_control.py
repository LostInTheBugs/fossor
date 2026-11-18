"""Coupure électrique à distance (§4.17).

Contrôleurs d'alimentation optionnels, un par machine (ou ASIC) :

* **Shelly Gen1** — API locale ``/relay/0?turn=on|off``, état et puissance dans
  ``/status`` (``relays[].ison``, ``meters[].power``) ;
* **Shelly Gen2** — ``/rpc/Switch.Set`` et ``/rpc/Switch.GetStatus?id=0``
  (``output``, ``apower``) ;
* **Home Assistant** — ``POST /api/services/switch/turn_on|turn_off`` avec un
  jeton long **chiffré**, état ``GET /api/states/<entité>`` et capteur de
  puissance associé ;
* **KVM IP / PicoKVM** — aucune API documentée : la configuration est acceptée
  mais toute action répond « non pris en charge » plutôt que de deviner un
  protocole.

Tous les garde-fous sont ici (machine qui héberge Fossor, quota quotidien, délai
minimal, double confirmation) : un refus ne laisse **aucun** appel sortant.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

KINDS = ("shelly_gen1", "shelly_gen2", "home_assistant", "kvm_ip")
KIND_LABELS = {
    "shelly_gen1": "Shelly Gen1 (API locale)",
    "shelly_gen2": "Shelly Gen2 (RPC)",
    "home_assistant": "Home Assistant (switch)",
    "kvm_ip": "KVM IP / PicoKVM",
}
SUPPORTED = ("shelly_gen1", "shelly_gen2", "home_assistant")
ACTIONS = ("on", "off", "cycle")
ACTION_LABELS = {"on": "allumer", "off": "éteindre", "cycle": "power cycle"}
DEFAULT_MAX_PER_DAY = 3
DEFAULT_MIN_DELAY_S = 60
DEFAULT_CYCLE_WAIT_S = 5
MEASURED_SOURCE = "mesurée à la prise"


class PowerError(Exception):
    """Refus ou échec d'une action d'alimentation (message en français)."""


# ── Configuration et secrets ─────────────────────────────────────────
def normalize_controller(raw: Any) -> dict[str, Any]:
    """Valide la configuration d'un contrôleur ; le jeton reste séparé."""
    if not isinstance(raw, dict):
        raise ValueError("contrôleur : objet attendu")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(
            "type de contrôleur inconnu : shelly_gen1, shelly_gen2, "
            "home_assistant ou kvm_ip attendu"
        )
    url = str(raw.get("url") or "").strip().rstrip("/")
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL : http:// ou https:// attendu")
    entity = str(raw.get("entity") or "").strip()
    power_entity = str(raw.get("power_entity") or "").strip()
    if kind == "home_assistant" and url and not entity:
        raise ValueError("Home Assistant : entité switch obligatoire")
    try:
        max_per_day = int(raw.get("max_per_day", DEFAULT_MAX_PER_DAY))
        min_delay = int(raw.get("min_delay_s", DEFAULT_MIN_DELAY_S))
    except (TypeError, ValueError):
        raise ValueError("quota et délai : entiers attendus")
    if not 1 <= max_per_day <= 50:
        raise ValueError("coupures par jour : entre 1 et 50")
    if not 5 <= min_delay <= 3600:
        raise ValueError("délai minimal : entre 5 et 3600 s")
    token = raw.get("token")
    return {
        "kind": kind,
        "url": url,
        "entity": entity,
        "power_entity": power_entity,
        "enabled": bool(raw.get("enabled", True)),
        "max_per_day": max_per_day,
        "min_delay_s": min_delay,
        "token": str(token).strip() if isinstance(token, str) else "",
        "supported": kind in SUPPORTED,
    }


def encrypt_token(token: str) -> str | None:
    if not token:
        return None
    from app.secrets import encrypt_secret

    return encrypt_secret(token)


def controller_dict(row: Any) -> dict[str, Any]:
    """Vue API : le jeton n'est jamais renvoyé, seule sa présence est dite."""
    return {
        "uuid": getattr(row, "uuid", ""),
        "machine_id": getattr(row, "machine_id", None),
        "kind": getattr(row, "kind", ""),
        "kind_label": KIND_LABELS.get(getattr(row, "kind", ""), getattr(row, "kind", "")),
        "url": getattr(row, "url", ""),
        "entity": getattr(row, "entity", ""),
        "power_entity": getattr(row, "power_entity", ""),
        "enabled": bool(getattr(row, "enabled", False)),
        "max_per_day": getattr(row, "max_per_day", DEFAULT_MAX_PER_DAY),
        "min_delay_s": getattr(row, "min_delay_s", DEFAULT_MIN_DELAY_S),
        "has_token": bool(getattr(row, "token_ref", None)),
        "supported": getattr(row, "kind", "") in SUPPORTED,
    }


def save_controller(db: Session, machine_id: int, raw: Any) -> Any:
    from app.models import PowerController

    config = normalize_controller(raw)
    row = db.query(PowerController).filter(PowerController.machine_id == machine_id).first()
    if row is None:
        import uuid as uuid_lib

        row = PowerController(uuid=str(uuid_lib.uuid4()), machine_id=machine_id)
        db.add(row)
    row.kind = config["kind"]
    row.url = config["url"]
    row.entity = config["entity"]
    row.power_entity = config["power_entity"]
    row.enabled = config["enabled"]
    row.max_per_day = config["max_per_day"]
    row.min_delay_s = config["min_delay_s"]
    if config["token"]:
        row.token_ref = encrypt_token(config["token"])
    row.updated_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    db.refresh(row)
    return row


# ── Requêtes (pures) ─────────────────────────────────────────────────
def build_requests(controller: Any, action: str, *, cycle_wait_s: int = DEFAULT_CYCLE_WAIT_S) -> list[dict[str, Any]]:
    """Requêtes HTTP d'une action, sans les envoyer (simulation == cette liste)."""
    kind = getattr(controller, "kind", "")
    if kind not in SUPPORTED:
        raise PowerError(
            "KVM IP / PicoKVM : non pris en charge (aucune API documentée)"
            if kind == "kvm_ip" else "contrôleur d'alimentation inconnu"
        )
    if action not in ACTIONS:
        raise PowerError("action inconnue : on, off ou cycle attendu")

    on = {"method": "POST" if kind in ("shelly_gen2", "home_assistant") else "GET"}
    requests: list[dict[str, Any]] = []

    def one(state: str) -> dict[str, Any]:
        if kind == "shelly_gen1":
            return {"method": "GET", "path": f"/relay/0?turn={'on' if state == 'on' else 'off'}", "json": None}
        if kind == "shelly_gen2":
            return {"method": "POST", "path": "/rpc/Switch.Set", "json": {"id": 0, "on": state == "on"}}
        return {
            "method": "POST",
            "path": f"/api/services/switch/turn_{'on' if state == 'on' else 'off'}",
            "json": {"entity_id": getattr(controller, "entity", "")},
        }

    if action == "on":
        requests.append(one("on"))
    elif action == "off":
        requests.append(one("off"))
    else:
        requests.append(one("off"))
        requests.append({"method": "WAIT", "path": "", "json": None, "wait_s": int(cycle_wait_s)})
        requests.append(one("on"))
    return requests


def state_request(controller: Any) -> dict[str, Any]:
    kind = getattr(controller, "kind", "")
    if kind == "shelly_gen1":
        return {"method": "GET", "path": "/status", "json": None}
    if kind == "shelly_gen2":
        return {"method": "GET", "path": "/rpc/Switch.GetStatus?id=0", "json": None}
    if kind == "home_assistant":
        return {"method": "GET", "path": f"/api/states/{getattr(controller, 'entity', '')}", "json": None}
    raise PowerError("état indisponible : contrôleur non pris en charge")


def parse_state(kind: str, body: Any) -> dict[str, Any]:
    """État et puissance d'une réponse de contrôleur (fixtures réelles)."""
    state = "unknown"
    power: float | None = None
    if not isinstance(body, dict):
        return {"state": state, "power_w": None}

    if kind == "shelly_gen1":
        relays = body.get("relays") or []
        if relays and isinstance(relays[0], dict):
            state = "on" if relays[0].get("ison") else "off"
        meters = body.get("meters") or []
        if meters and isinstance(meters[0], dict) and meters[0].get("power") is not None:
            try:
                power = float(meters[0]["power"])
            except (TypeError, ValueError):
                power = None
    elif kind == "shelly_gen2":
        if body.get("output") is not None:
            state = "on" if body.get("output") else "off"
        if body.get("apower") is not None:
            try:
                power = float(body["apower"])
            except (TypeError, ValueError):
                power = None
    elif kind == "home_assistant":
        state = str(body.get("state") or "unknown")
        attributes = body.get("attributes") if isinstance(body.get("attributes"), dict) else {}
        for key in ("power", "current_power_w", "power_w"):
            if attributes.get(key) is not None:
                try:
                    power = float(attributes[key])
                except (TypeError, ValueError):
                    power = None
                break
    return {"state": state, "power_w": power}


def _headers(controller: Any) -> dict[str, str]:
    if getattr(controller, "kind", "") != "home_assistant":
        return {}
    token = ""
    if getattr(controller, "token_ref", None):
        from app.secrets import decrypt_secret

        token = decrypt_secret(controller.token_ref)
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _send(client: Any, base: str, request: dict[str, Any], headers: dict[str, str]) -> Any:
    url = base + request["path"]
    if request["method"] == "GET":
        response = await client.get(url, headers=headers)
    else:
        response = await client.post(url, headers=headers, json=request.get("json"))
    if response.status_code >= 400:
        raise PowerError(f"HTTP {response.status_code} sur {request['path']}")
    try:
        return response.json()
    except ValueError:
        return {}


async def perform(
    controller: Any,
    action: str,
    *,
    cycle_wait_s: int = DEFAULT_CYCLE_WAIT_S,
    client: Any = None,
    sleep: Any = None,
) -> dict[str, Any]:
    """Envoie les requêtes d'une action ; renvoie ce qui a été envoyé."""
    requests = build_requests(controller, action, cycle_wait_s=cycle_wait_s)
    sent: list[dict[str, Any]] = []
    owned = client is None
    if owned:
        import httpx

        client = httpx.AsyncClient(timeout=8.0)
    try:
        for request in requests:
            if request["method"] == "WAIT":
                sent.append({"method": "WAIT", "path": "", "wait_s": request["wait_s"]})
                if sleep is not None:
                    await sleep(request["wait_s"])
                else:
                    await asyncio.sleep(request["wait_s"])
                continue
            await _send(client, getattr(controller, "url", ""), request, _headers(controller))
            sent.append({"method": request["method"], "path": request["path"], "json": request.get("json")})
    finally:
        if owned:
            await client.aclose()
    return {"ok": True, "action": action, "sent": sent}


async def read_state(controller: Any, *, client: Any = None) -> dict[str, Any]:
    """État et puissance mesurée ; la source est nommée avec la valeur."""
    import httpx

    kind = getattr(controller, "kind", "")
    headers = _headers(controller)
    base = getattr(controller, "url", "")
    owned = client is None
    http = client or httpx.AsyncClient(timeout=8.0)
    try:
        body = await _send(http, base, state_request(controller), headers)
        parsed = parse_state(kind, body)
        if kind == "home_assistant" and getattr(controller, "power_entity", "") and parsed["power_w"] is None:
            # Capteur de puissance séparé, documenté par Home Assistant.
            body = await _send(
                http, base,
                {"method": "GET", "path": f"/api/states/{controller.power_entity}", "json": None},
                headers,
            )
            try:
                parsed["power_w"] = float(body.get("state"))
            except (TypeError, ValueError, AttributeError):
                parsed["power_w"] = None
    finally:
        if owned:
            await http.aclose()
    parsed["source"] = MEASURED_SOURCE
    parsed["controller"] = KIND_LABELS.get(kind, kind)
    return parsed


# ── Garde-fous et journal ────────────────────────────────────────────
def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def counted_actions(db: Session, machine_id: int, now: datetime, *, window_h: int = 24) -> int:
    """Coupures réelles (off / cycle) dans la fenêtre, hors simulation."""
    from app.models import PowerAction

    since = (_now(now) - timedelta(hours=window_h)).isoformat()
    return (
        db.query(PowerAction)
        .filter(
            PowerAction.machine_id == machine_id,
            PowerAction.at >= since,
            PowerAction.action.in_(("off", "cycle")),
            PowerAction.simulated.is_(False),
            PowerAction.result != "refused",
        )
        .count()
    )


def last_action(db: Session, machine_id: int) -> Any:
    from app.models import PowerAction

    return (
        db.query(PowerAction)
        .filter(
            PowerAction.machine_id == machine_id,
            PowerAction.simulated.is_(False),
            # Un refus n'est pas une action : il ne doit pas armer le délai.
            PowerAction.result != "refused",
        )
        .order_by(PowerAction.at.desc())
        .first()
    )


def check_guards(
    db: Session,
    machine: Any,
    controller: Any,
    action: str,
    *,
    confirm: bool = False,
    online: bool = False,
    now: datetime | None = None,
) -> None:
    """Lève ``PowerError`` avant tout appel ; aucun effet de bord."""
    if action not in ACTIONS:
        raise PowerError("action inconnue : on, off ou cycle attendu")
    if getattr(machine, "fossor_host", False):
        raise PowerError(
            "coupure interdite : cette machine héberge Fossor (marquée « machine Fossor »)"
        )
    if controller is None:
        raise PowerError("aucun contrôleur d'alimentation configuré pour cette machine")
    if not getattr(controller, "enabled", False):
        raise PowerError("contrôleur d'alimentation désactivé")
    if getattr(controller, "kind", "") not in SUPPORTED:
        raise PowerError(
            "KVM IP / PicoKVM : non pris en charge (aucune API documentée)"
            if getattr(controller, "kind", "") == "kvm_ip" else "contrôleur non pris en charge"
        )
    moment = _now(now)
    if action in ("off", "cycle"):
        quota = int(getattr(controller, "max_per_day", DEFAULT_MAX_PER_DAY))
        used = counted_actions(db, machine.id, moment)
        if used >= quota:
            raise PowerError(
                f"quota atteint : {used} coupure(s) sur {quota} autorisées en 24 h pour cette machine"
            )
    last = last_action(db, machine.id)
    if last is not None:
        try:
            previous = datetime.fromisoformat(str(last.at))
        except ValueError:
            previous = None
        if previous is not None:
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            delta = (moment - previous).total_seconds()
            delay = int(getattr(controller, "min_delay_s", DEFAULT_MIN_DELAY_S))
            if delta < delay:
                raise PowerError(
                    f"délai minimal non écoulé : {int(delta)} s depuis la dernière action "
                    f"({delay} s requis)"
                )
    if action == "off" and online and not confirm:
        raise PowerError(
            "double confirmation requise : la machine est en ligne, "
            "renvoyer l'action avec confirm=true"
        )


def journal(
    db: Session,
    machine_id: int,
    action: str,
    *,
    result: str,
    detail: str,
    simulated: bool,
    actor: str = "system",
    now: datetime | None = None,
) -> Any:
    from app.models import PowerAction

    row = PowerAction(
        machine_id=machine_id,
        action=action,
        result=result,
        detail=detail,
        simulated=simulated,
        actor=actor,
        at=_now(now).isoformat(),
    )
    db.add(row)
    db.commit()
    return row


def store_sample(
    db: Session, machine_id: int, power_w: float, source: str, controller_kind: str,
    *, now: datetime | None = None,
) -> Any:
    """Enregistre une mesure à la prise (lue par le coût §4.15)."""
    from app.models import PowerSample

    row = PowerSample(
        machine_id=machine_id,
        ts=_now(now).isoformat(),
        power_w=float(power_w),
        source=source,
        controller_kind=controller_kind,
    )
    db.add(row)
    db.commit()
    return row
