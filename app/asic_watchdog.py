"""§6.8 — section « ASIC » du watchdog (§10.1).

Un ASIC n'a pas de domaine de hachage à relancer comme une unité systemd : la
seule action possible est un redémarrage par son API.  Cette section surveille
donc deux défauts, pendant N minutes consécutives :

* des domaines de hachage inactifs (``3/4`` — moins que le total) ;
* un hashrate sous X % du nominal de la machine.

Action : « notifier » par défaut.  Le redémarrage est une option explicite
(``asic.restart``) ; il est plafonné à ``max_per_day`` (1 à 5, 3 par défaut)
sur 24 h, espacé de ``min_delay_minutes`` (15 min), et refusé pendant un boost
ou une campagne qui mobilise l'ASIC (les ASIC sont déjà exclus des campagnes :
c'est une défense en profondeur).  Une fois la limite atteinte, plus aucune
action : notification « intervention manuelle nécessaire » et ASIC marqué en
rouge.

Endpoint de redémarrage AxeOS, **documenté dans le code source officiel** :
ESP-Miner **v2.12.2**, ``main/http_server/http_server.c`` lignes 1318-1323 —

    httpd_uri_t system_restart_uri = {
        .uri = "/api/system/restart", .method = HTTP_POST,
        .handler = POST_restart,
    };
    httpd_register_uri_handler(server, &system_restart_uri);

et ``POST_restart`` (lignes 731-755) répond « System will restart shortly. »
puis appelle ``esp_restart()``.  Aucune autre URL n'est supposée.

IceRiver et cgminer n'ont pas d'endpoint de redémarrage documenté : pour ces
pilotes la section se limite à une notification (``UNSUPPORTED``).

Les identifiants d'ASIC (chiffrés en base) ne sont jamais lus, journalisés ni
renvoyés ici : le module ne touche qu'à l'état public de la sonde.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

RULE_DOMAINS = "asic_domains"
RULE_HASHRATE = "asic_hashrate"
RULE_VERIFY = "asic_verify"
RULE_MANUAL = "asic_manual"
RULE_NAMES = (RULE_DOMAINS, RULE_HASHRATE)

RESTART_ACTION = "restart_asic"
MANUAL_TEXT = "intervention manuelle nécessaire"
UNSUPPORTED = "pilote sans endpoint de redémarrage documenté : notification seule"
CONFIRM_WORD = "VALIDER"

#: Réponse du firmware AxeOS au POST de redémarrage (ESP-Miner v2.12.2).
AXEOS_RESTART_PATH = "/api/system/restart"

#: Intervalle d'attente entre deux lectures pendant la coupure puis le retour.
POLL_SECONDS = 5


class RestartRefused(RuntimeError):
    """Redémarrage refusé par un garde-fou (limite, pilote, cible)."""


class RestartLimited(RestartRefused):
    """La limite de redémarrages sur 24 h est atteinte."""


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Bloc ``asic`` d'une config déjà normalisée par :mod:`app.watchdog`."""
    block = (config or {}).get("asic")
    return block if isinstance(block, dict) else {}


def observed(state: dict[str, Any] | None) -> dict[str, Any]:
    """Valeurs utiles de l'état public d'un ASIC (aucun identifiant)."""
    state = state or {}
    return {
        "online": bool(state.get("online", True)),
        "domains_total": int(state.get("domains_total") or 0),
        "domains_inactive": int(state.get("domains_inactive") or 0),
        "hashrate_hs": state.get("hashrate_hs"),
    }


def defects(state: dict[str, Any] | None, cfg: dict[str, Any],
            nominal_hs: float | None) -> list[tuple[str, str, int]]:
    """Défauts actifs : ``(règle, mesure, minutes)``, domaine d'abord."""
    values = observed(state)
    found: list[tuple[str, str, int]] = []
    total, inactive = values["domains_total"], values["domains_inactive"]
    if total > 0 and inactive > 0:
        found.append((RULE_DOMAINS,
                      f"{total - inactive}/{total} domaines de hachage actifs",
                      int(cfg.get("domains_minutes", 10))))
    pct = float(cfg.get("hashrate_pct", 70.0))
    hashrate = values["hashrate_hs"]
    nominal = float(nominal_hs or 0)
    if nominal > 0 and hashrate is not None and float(hashrate) < nominal * pct / 100.0:
        found.append((RULE_HASHRATE,
                      f"{float(hashrate):.0f} H/s < {nominal * pct / 100.0:.0f} H/s "
                      f"({pct:.0f} % de {nominal:.0f})",
                      int(cfg.get("hashrate_minutes", 10))))
    return found


def restored(state: dict[str, Any] | None, cfg: dict[str, Any],
             nominal_hs: float | None) -> bool:
    """Rétabli = tous les domaines actifs **et** hashrate au-dessus du seuil."""
    values = observed(state)
    if not values["online"]:
        return False
    if values["domains_total"] <= 0 or values["domains_inactive"] > 0:
        return False
    nominal = float(nominal_hs or 0)
    hashrate = values["hashrate_hs"]
    if nominal > 0 and (hashrate is None or float(hashrate) < nominal * float(cfg.get("hashrate_pct", 70.0)) / 100.0):
        return False
    return True


def _state_row(db: Any, machine_id: int, rule: str) -> Any:
    from app.models import WatchdogState

    return (
        db.query(WatchdogState)
        .filter(WatchdogState.machine_id == machine_id,
                WatchdogState.device_id == 0,
                WatchdogState.rule == rule)
        .first()
    )


def _drop(db: Any, row: Any) -> None:
    if row is not None:
        db.delete(row)


def restarts_in_window(db: Any, machine_id: int, moment: datetime, *, hours: int = 24,
                       simulated: bool = False) -> int:
    """Redémarrages du même mode sur la fenêtre : un simulé ne compte jamais comme un réel."""
    from app.models import WatchdogEvent

    limit = _iso(moment - timedelta(hours=hours))
    return (
        db.query(WatchdogEvent)
        .filter(WatchdogEvent.machine_id == machine_id,
                WatchdogEvent.action == RESTART_ACTION,
                WatchdogEvent.simulated.is_(bool(simulated)),
                WatchdogEvent.ts >= limit)
        .count()
    )


def last_restart(db: Any, machine_id: int, moment: datetime, *, hours: int = 24,
                 simulated: bool = False) -> datetime | None:
    from app.models import WatchdogEvent

    limit = _iso(moment - timedelta(hours=hours))
    row = (
        db.query(WatchdogEvent)
        .filter(WatchdogEvent.machine_id == machine_id,
                WatchdogEvent.action == RESTART_ACTION,
                WatchdogEvent.simulated.is_(bool(simulated)),
                WatchdogEvent.ts >= limit)
        .order_by(WatchdogEvent.ts.desc())
        .first()
    )
    return _parse(row.ts) if row is not None else None


def is_red(db: Any, machine_id: int) -> bool:
    row = _state_row(db, machine_id, RULE_MANUAL)
    return bool(row is not None and int(row.stage or 0) >= 1)


def _mark_red(db: Any, machine: Any, moment: datetime, detail: str) -> None:
    row = _state_row(db, machine.id, RULE_MANUAL)
    if row is None:
        from app.models import WatchdogState

        row = WatchdogState(machine_id=machine.id, device_id=0, rule=RULE_MANUAL,
                            since=_iso(moment), stage=1, counter=0)
        db.add(row)
    row.stage = 1
    row.last_action_at = _iso(moment)
    row.counter = (int(row.counter or 0)) + 1
    db.flush()


def _clear(db: Any, machine: Any) -> None:
    for rule in (RULE_MANUAL, *RULE_NAMES):
        _drop(db, _state_row(db, machine.id, rule))


def _journal(db: Any, machine: Any, rule: str, measure: str, action: str,
             result: str, simulated: bool, detail: str, moment: datetime) -> None:
    from app.models import WatchdogEvent

    db.add(WatchdogEvent(
        ts=_iso(moment), machine_id=machine.id, machine_name=machine.name or "",
        device_id=0, device_label="ASIC", rule=rule, measure=measure[:300],
        action=action, result=result, simulated=bool(simulated), detail=detail[:2000],
    ))
    db.flush()


def _notify(db: Any, machine: Any, rule: str, measure: str, action: str,
            result: str, detail: str, simulated: bool) -> None:
    from app import notifications

    label = machine.name or f"ASIC {machine.id}"
    message = (
        f"Watchdog ASIC — {label} : {rule} ({measure}). Action : {action} — {result}"
        f"{' [simulé]' if simulated else ''}. {detail}"
    )
    level = "critique" if action in (RESTART_ACTION, "notify") and result in (
        "manual", "failed", "unsupported") else "alerte"
    try:
        notifications.enqueue(db, "watchdog_action", message[:2000], level=level)
    except Exception:  # un canal absent ne bloque jamais la décision
        logger.warning("watchdog ASIC : notification en échec", exc_info=True)


def blocker(db: Any, machine: Any) -> str:
    """Raison de ne pas toucher à cet ASIC maintenant ('' = libre).

    Les ASIC sont exclus des campagnes par construction ; ce contrôle est une
    défense en profondeur (benchmark, campagne en bascule, boost qui nomme
    l'ASIC).
    """
    from app import watchdog as wd

    try:
        from app import benchmark_runner

        if benchmark_runner.machine_muted(db, machine.id):
            return "benchmark en cours sur la machine"
    except Exception:
        logger.warning("watchdog ASIC : contrôle benchmark en échec", exc_info=True)
    try:
        if wd.campaign_switching(db, machine):
            return "campagne en bascule"
    except Exception:
        logger.warning("watchdog ASIC : contrôle campagne en échec", exc_info=True)
    try:
        from app.models import Boost

        for boost in db.query(Boost).filter(Boost.status.in_(("running", "starting"))).all():
            if str(machine.id) in [part.strip() for part in str(boost.machine_ids or "").split(",")]:
                return "boost en cours sur la machine"
    except Exception:
        logger.warning("watchdog ASIC : contrôle boost en échec", exc_info=True)
    return ""


async def _default_probe(db: Any, machine: Any) -> dict[str, Any]:
    from app.routers.asic import probe_machine

    return await probe_machine(machine, db)


async def _verify(db: Any, machine: Any, cfg: dict[str, Any], moment: datetime, *,
                  probe: Any = None, sleep: Any = None,
                  simulated: bool = False) -> tuple[bool, dict[str, Any] | None, str]:
    """Preuve réelle : coupure, retour, uptime bas, puis domaines et hashrate.

    En simulation rien n'a redémarré : une seule lecture, sans attente et sans
    preuve d'uptime.
    """
    probe = probe or _default_probe
    if simulated:
        last = await _read(probe, db, machine)
        if last and restored(last, cfg, machine.asic_expected_hashrate_hs):
            return True, last, "état lu en simulation"
        return False, last, "ASIC non rétabli (simulation) : une seule lecture"

    cycled, last = await _wait_cycle(db, machine, cfg, probe, sleep)
    proven, proof = reboot_proven(last, cfg)
    if not proven:
        prefix = "" if cycled else "coupure non observée ; "
        return False, last, prefix + proof
    attempts = max(1, int(cfg.get("verify_seconds", 90)) // 15)
    for index in range(attempts):
        if index:
            await _pause(sleep, 15)
            last = await _read(probe, db, machine)
        if last and restored(last, cfg, machine.asic_expected_hashrate_hs):
            return True, last, proof
    return False, last, f"redémarrage prouvé ({proof}) mais ASIC non rétabli"


async def _pause(sleep: Any, seconds: float) -> None:
    import asyncio

    if sleep is not None:
        await sleep(seconds)
    else:
        await asyncio.sleep(seconds)


async def _read(probe: Any, db: Any, machine: Any) -> dict[str, Any] | None:
    try:
        return await probe(db, machine)
    except Exception:
        return None


async def _wait_cycle(db: Any, machine: Any, cfg: dict[str, Any],
                      probe: Any, sleep: Any) -> tuple[bool, dict[str, Any] | None]:
    """Attend la coupure puis le retour, dans un délai borné par la config."""
    wait = int(cfg.get("restart_wait_seconds", 180))
    down = False
    last: dict[str, Any] | None = None
    for _ in range(max(1, wait // POLL_SECONDS)):
        await _pause(sleep, POLL_SECONDS)
        state = await _read(probe, db, machine)
        if state is None or state.get("online") is False:
            down = True
            continue
        last = state
        if down:
            return True, last
    return False, last


def reboot_proven(state: dict[str, Any] | None, cfg: dict[str, Any]) -> tuple[bool, str]:
    """``uptime_s`` faible : preuve que l'ASIC est reparti de zéro."""
    if not state or state.get("online") is False:
        return False, "ASIC injoignable après redémarrage"
    uptime = state.get("uptime_s")
    if uptime is None:
        return False, "uptime_s illisible : redémarrage non prouvé"
    maximum = int(cfg.get("uptime_max_s", 300))
    if float(uptime) < 0 or float(uptime) > maximum:
        return False, f"uptime_s={float(uptime):.0f} > {maximum} : redémarrage non prouvé"
    return True, f"uptime_s={float(uptime):.0f}"


async def _do_restart(db: Any, machine: Any, cfg: dict[str, Any], moment: datetime, *,
                      rule: str, measure: str, reason: str, restart: Any = None,
                      probe: Any = None, sleep: Any = None, simulated: bool) -> dict[str, Any]:
    from app import asic_client

    if simulated:
        detail = "action simulée (mode simulation) : aucun appel réseau"
    elif (machine.asic_driver or "").strip().lower() != "axeos":
        _journal(db, machine, rule, measure, "notify", "unsupported", False,
                 UNSUPPORTED, moment)
        _notify(db, machine, rule, measure, "notify", "unsupported", UNSUPPORTED, False)
        db.commit()
        return {"action": "notify", "result": "unsupported", "detail": UNSUPPORTED}
    else:
        try:
            if restart is not None:
                await restart(machine)
            else:
                await asic_client.restart_asic(machine.asic_driver or "", machine.host)
            detail = f"POST {AXEOS_RESTART_PATH} accepté"
        except asic_client.AsicUnsupported:
            _journal(db, machine, rule, measure, "notify", "unsupported", False,
                     UNSUPPORTED, moment)
            _notify(db, machine, rule, measure, "notify", "unsupported", UNSUPPORTED, False)
            db.commit()
            return {"action": "notify", "result": "unsupported", "detail": UNSUPPORTED}
        except Exception as exc:
            detail = f"échec du redémarrage : {exc}"
            _journal(db, machine, rule, measure, RESTART_ACTION, "failed", False, detail, moment)
            _notify(db, machine, rule, measure, RESTART_ACTION, "failed", detail, False)
            db.commit()
            return {"action": RESTART_ACTION, "result": "failed", "detail": detail}
    _journal(db, machine, rule, measure, RESTART_ACTION, "ok", simulated,
             f"{reason} — {detail}", moment)
    _notify(db, machine, rule, measure, RESTART_ACTION, "ok", detail, simulated)
    row = _state_row(db, machine.id, rule)
    if row is not None:
        row.stage = int(row.stage or 0) + 1
        row.last_action_at = _iso(moment)
    db.commit()
    ok, seen, why = await _verify(db, machine, cfg, moment, probe=probe, sleep=sleep,
                                  simulated=simulated)
    if ok:
        _journal(db, machine, RULE_VERIFY, measure, "verify", "restored", simulated,
                 f"ASIC rétabli : tous les domaines actifs et hashrate au-dessus du seuil ({why})",
                 moment)
        _notify(db, machine, RULE_VERIFY, measure, "verify", "restored",
                f"ASIC rétabli après redémarrage ({why})", simulated)
        _clear(db, machine)
    else:
        _journal(db, machine, RULE_VERIFY, measure, "verify", "failed", simulated,
                 f"ASIC non rétabli après redémarrage ({why}) : la tentative compte", moment)
        _notify(db, machine, RULE_VERIFY, measure, "verify", "failed",
                f"ASIC non rétabli après redémarrage ({why})", simulated)
        maximum = int(cfg.get("max_per_day", 3))
        counted = restarts_in_window(db, machine.id, moment, simulated=simulated)
        if counted >= maximum:
            _mark_red(db, machine, moment, MANUAL_TEXT)
            detail = f"{counted} redémarrage(s) sur 24 h (limite {maximum}) : {MANUAL_TEXT}"
            _journal(db, machine, rule, measure, "notify", "manual", simulated, detail, moment)
            _notify(db, machine, rule, measure, "notify", "manual", detail, simulated)
    db.commit()
    outcome = {"action": RESTART_ACTION, "result": "ok", "verified": ok,
               "detail": detail, "state": seen, "proof": why}
    if simulated:
        outcome["simulated"] = True
    return outcome


async def evaluate(db: Any, machine: Any, state: dict[str, Any] | None, *,
                   now: datetime | None = None, config: dict[str, Any] | None = None,
                   probe: Any = None, restart: Any = None, sleep: Any = None) -> list[dict[str, Any]]:
    """Évalue la section ASIC pour une machine ; renvoie les déclenchements."""
    from app import watchdog as wd

    if getattr(machine, "machine_kind", "") != "asic":
        return []
    moment = _now(now)
    full = config if isinstance(config, dict) else wd.get_config(db)
    cfg = settings(wd.machine_config(full, machine))
    if not cfg.get("enabled", True):
        return []
    if not state:
        return []
    values = observed(state)
    if not values["online"]:
        return []
    found = defects(state, cfg, machine.asic_expected_hashrate_hs)
    events: list[dict[str, Any]] = []
    if not found:
        for rule in RULE_NAMES:
            _drop(db, _state_row(db, machine.id, rule))
        db.commit()
        return events
    active_rule, measure, minutes = found[0]
    for rule in RULE_NAMES:
        if rule != active_rule:
            _drop(db, _state_row(db, machine.id, rule))
    row = _state_row(db, machine.id, active_rule)
    if row is None:
        from app.models import WatchdogState

        row = WatchdogState(machine_id=machine.id, device_id=0, rule=active_rule,
                            since=_iso(moment), stage=0, last_action_at=None)
        db.add(row)
        db.flush()
    since = _parse(row.since) or moment
    if (moment - since).total_seconds() < minutes * 60:
        db.commit()
        return events
    escalation = int(wd.get_config(db).get("escalation_minutes", 5)) * 60
    previous = _parse(row.last_action_at)
    if previous is not None and (moment - previous).total_seconds() < escalation:
        db.commit()
        return events
    simulated = wd._is_simulated(db, machine)
    if not cfg.get("restart", False):
        detail = "action « redémarrer » désactivée : notification seule"
        _journal(db, machine, active_rule, measure, "notify", "notified", simulated, detail, moment)
        _notify(db, machine, active_rule, measure, "notify", "notified", detail, simulated)
        row.last_action_at = _iso(moment)
        db.commit()
        return [{"rule": active_rule, "action": "notify", "result": "notified",
                 "machine_id": machine.id}]
    reason = blocker(db, machine)
    if reason:
        _journal(db, machine, active_rule, measure, "notify", "blocked", simulated,
                 f"garde-fou : {reason}", moment)
        _notify(db, machine, active_rule, measure, "notify", "blocked",
                f"garde-fou : {reason}", simulated)
        row.last_action_at = _iso(moment)
        db.commit()
        return [{"rule": active_rule, "action": "notify", "result": "blocked",
                 "machine_id": machine.id}]
    maximum = int(cfg.get("max_per_day", 3))
    counted = restarts_in_window(db, machine.id, moment, simulated=simulated)
    if counted >= maximum:
        _mark_red(db, machine, moment, MANUAL_TEXT)
        _journal(db, machine, active_rule, measure, "notify", "manual", simulated,
                 f"{counted} redémarrage(s) sur 24 h (limite {maximum}) : {MANUAL_TEXT}", moment)
        _notify(db, machine, active_rule, measure, "notify", "manual",
                f"{counted} redémarrage(s) sur 24 h (limite {maximum}) : {MANUAL_TEXT}", simulated)
        row.last_action_at = _iso(moment)
        db.commit()
        return [{"rule": active_rule, "action": "notify", "result": "manual",
                 "machine_id": machine.id}]
    last = last_restart(db, machine.id, moment, simulated=simulated)
    delay = int(cfg.get("min_delay_minutes", 15)) * 60
    if last is not None and (moment - last).total_seconds() < delay:
        db.commit()
        return events
    outcome = await _do_restart(db, machine, cfg, moment, rule=active_rule, measure=measure,
                                reason=f"condition {measure} depuis {minutes} min",
                                restart=restart, probe=probe, sleep=sleep,
                                simulated=simulated)
    events.append({"rule": active_rule, "action": outcome["action"],
                   "result": outcome["result"], "machine_id": machine.id})
    return events


async def manual_restart(db: Any, machine: Any, *, confirm: str,
                         now: datetime | None = None, restart: Any = None,
                         probe: Any = None, sleep: Any = None) -> dict[str, Any]:
    """Bouton « Redémarrer » : confirmation VALIDER, mêmes garde-fous."""
    from app import watchdog as wd

    if confirm != CONFIRM_WORD:
        raise RestartRefused(f"confirmation requise : saisir {CONFIRM_WORD}")
    if getattr(machine, "machine_kind", "") != "asic":
        raise RestartRefused("cette cible n'est pas un ASIC")
    moment = _now(now)
    full = wd.get_config(db)
    cfg = settings(wd.machine_config(full, machine))
    simulated = wd._is_simulated(db, machine)
    reason = blocker(db, machine)
    if reason and not simulated:
        raise RestartRefused(f"garde-fou : {reason}")
    maximum = int(cfg.get("max_per_day", 3))
    counted = restarts_in_window(db, machine.id, moment, simulated=simulated)
    if counted >= maximum:
        _mark_red(db, machine, moment, MANUAL_TEXT)
        db.commit()
        raise RestartLimited(
            f"limite atteinte : {counted} redémarrage(s) sur 24 h (maximum {maximum})")
    last = last_restart(db, machine.id, moment, simulated=simulated)
    delay = int(cfg.get("min_delay_minutes", 15)) * 60
    if last is not None and (moment - last).total_seconds() < delay:
        raise RestartRefused(
            f"délai minimum entre deux redémarrages : {int(cfg.get('min_delay_minutes', 15))} min")
    outcome = await _do_restart(db, machine, cfg, moment, rule=RULE_MANUAL,
                                measure="redémarrage manuel", reason="bouton Redémarrer",
                                restart=restart, probe=probe, sleep=sleep, simulated=simulated)
    db.commit()
    return outcome


def summary(db: Any, machine: Any, *, now: datetime | None = None,
            config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ligne d'UI : compteur 24 h, dernière action, rouge, config effective."""
    from app import watchdog as wd
    from app.models import WatchdogEvent

    moment = _now(now)
    full = config if isinstance(config, dict) else wd.get_config(db)
    cfg = settings(wd.machine_config(full, machine))
    simulated = wd._is_simulated(db, machine)
    counted = restarts_in_window(db, machine.id, moment, simulated=simulated)
    last = (
        db.query(WatchdogEvent)
        .filter(WatchdogEvent.machine_id == machine.id)
        .order_by(WatchdogEvent.ts.desc(), WatchdogEvent.id.desc())
        .first()
    )
    return {
        "id": machine.id,
        "name": machine.name,
        "driver": machine.asic_driver or "",
        "host": machine.host,
        "restart": bool(cfg.get("restart", False)),
        "restarts_24h": counted,
        "simulated": simulated,
        "max_per_day": int(cfg.get("max_per_day", 3)),
        "red": is_red(db, machine.id) or counted >= int(cfg.get("max_per_day", 3)),
        "last": None if last is None else {
            "ts": last.ts, "rule": last.rule, "action": last.action,
            "result": last.result, "simulated": bool(last.simulated),
            "measure": last.measure,
        },
    }
