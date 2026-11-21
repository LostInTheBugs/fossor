"""Veille spec mining pilotée par Fossor (§4.8, §4.12).

Fossor ne décide plus rien dans un skill : il **déclenche** un tick chez Hermes
(provider ``hermes-agent``), Hermes lit ``GET /api/hermes/context``, dépose une
analyse et, si un candidat est retenu, une proposition.  Fossor n'exécute
jamais de commande SSH ici : le tick ne peut ni lancer ni arrêter un mineur.

Deux déclencheurs :

* le bouton « Lancer une veille maintenant » (écran Analyse des tokens) ;
* la cadence de la règle ``watch_cadence`` — elle prend le relais **après** le
  premier tick manuel, pour qu'un déploiement ne déclenche pas d'appel surprise.

Un tick a **un seul identifiant**, de ``POST /veille/run`` jusqu'à l'état
final.  Il est borné par le réglage ``veille_max_minutes`` (30 par défaut) :
au-delà, il est clos en ``timeout``, le verrou est rendu et la requête HTTP
vers le provider est annulée.  Un tick resté ``running`` à l'arrêt de l'app est
clos en ``interrupted`` au démarrage suivant.

L'état du dernier tick (heure, durée, résultat, compteurs, journal des appels
Hermes) vit dans la table ``settings`` (clé ``veille_last_tick``) : il est
exporté avec le reste de la configuration et ne contient aucun secret.
"""

from __future__ import annotations

import os
import threading
import time
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app import ai_config
from app.models import Analysis, Campaign, Rule

STATE_KEY = "veille_last_tick"
MAX_MINUTES_KEY = "veille_max_minutes"
DEFAULT_MAX_MINUTES = 30
MAX_MINUTES_CEILING = 240
#: Borne dure : même si le réglage est modifié pendant un tick, l'appel HTTP
#: ne peut pas dépasser cette valeur (garde-fou de dernier recours).
TICK_TIMEOUT_S = 1800
_PROMPT_MAX = 1200
JOURNAL_MAX = 60

_lock = threading.Lock()
_scheduler_started = False

#: Tick en cours : journal des requêtes Hermes et compteurs de dépôt.
_journal_guard = threading.Lock()
_current_tick: "_Tick | None" = None

#: Clients HTTP vivants, pour annuler l'appel au provider au-delà de la borne.
_clients_guard = threading.Lock()
_live_clients: set[httpx.Client] = set()
_read_timeout_s = float(TICK_TIMEOUT_S)


class _Tick:
    """Contexte d'un tick : identifiant stable, journal et compteurs."""

    def __init__(self, tick_id: str, started: datetime, max_minutes: int):
        self.id = tick_id
        self.started = started
        self.max_minutes = max_minutes
        self.deadline = started + timedelta(minutes=max_minutes)
        self.journal: list[dict] = []
        self.analyses = 0
        self.candidates = 0
        self.proposals = 0

    def remaining_s(self) -> float:
        return max(0.0, (self.deadline - datetime.now(timezone.utc)).total_seconds())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_state(db: Session) -> dict | None:
    value = ai_config.get_setting(db, STATE_KEY, None)
    return value if isinstance(value, dict) else None


def _set_state(db: Session, state: dict) -> None:
    ai_config.set_setting(db, STATE_KEY, state)
    db.commit()


def max_minutes(db: Session) -> int:
    """Durée maximale d'un tick, réglable dans Configuration (30 par défaut)."""
    value = ai_config.get_setting(db, MAX_MINUTES_KEY, DEFAULT_MAX_MINUTES)
    try:
        return max(1, min(MAX_MINUTES_CEILING, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_MINUTES


def build_prompt(fossor_url: str) -> str:
    """Consigne courte et bornée : le skill porte la méthode, Fossor les règles."""
    base = fossor_url.rstrip("/")
    text = (
        f"Veille Fossor ({base}) : charge le skill crypto-mining/spec-mining-fossor "
        "et exécute UN tick de veille maintenant.\n"
        f"1. Lis {base}/api/hermes/context (jeton lu uniquement dans "
        "~/.hermes/fossor/token, URL uniquement dans ~/.hermes/fossor/url).\n"
        "2. Recherche 15 minutes maximum, puis arrête : pas de navigation "
        "interminable.\n"
        f"3. Termine TOUJOURS par POST {base}/api/hermes/analyses, même sans "
        "candidat (détail « rien de nouveau », reason sur chaque candidat). "
        "Contrôle le code HTTP : un 2xx vaut analyse déposée, sinon dis-le "
        "explicitement ; n'affirme jamais un dépôt sans 2xx.\n"
        f"4. POST {base}/api/hermes/proposals seulement si un candidat est "
        "retenu et minable.\n"
        "Aucune commande SSH vers une machine de Fred, aucun lancement. "
        "Réponds [SILENT] si rien n'est actionnable."
    )
    return text[:_PROMPT_MAX]


def provider_ready(db: Session) -> tuple[bool, str]:
    row = ai_config.get_provider(db, "primary")
    if row is None:
        return False, "fournisseur « primary » non configuré"
    if not row.enabled:
        return False, "fournisseur « primary » désactivé"
    if row.provider_type != "hermes-agent":
        return False, "le tick exige un fournisseur de type « hermes-agent »"
    if not (row.base_url or "").strip():
        return False, "adresse (base_url) vide"
    if not row.api_key_ref:
        return False, "clé API absente"
    return True, ""


def cadence_hours(db: Session) -> int:
    rule = db.get(Rule, "watch_cadence")
    params = (rule.params or {}) if rule is not None else {}
    try:
        return max(1, min(168, int(params.get("hours", 6))))
    except (TypeError, ValueError):
        return 6


def due(db: Session) -> bool:
    """La cadence ne prend le relais qu'après un premier tick terminé."""
    rule = db.get(Rule, "watch_cadence")
    if rule is not None and not rule.enabled:
        return False
    state = get_state(db)
    if not state or not state.get("finished_at"):
        return False
    try:
        finished = datetime.fromisoformat(str(state["finished_at"]))
    except ValueError:
        return False
    return _now() >= finished + timedelta(hours=cadence_hours(db))


# --------------------------------------------------------------------------
# Journal du tick : requêtes reçues de Hermes (méthode, chemin, code)
# --------------------------------------------------------------------------


def note_request(method: str, path: str, status: int) -> None:
    """Enregistre un appel de l'API Hermes s'il tombe pendant un tick."""
    tick = _current_tick
    if tick is None:
        return
    with _journal_guard:
        tick.journal.append(
            {"ts": _now().isoformat(), "method": method, "path": path, "status": int(status)}
        )
        del tick.journal[:-JOURNAL_MAX]


def note_analysis(candidates: int) -> None:
    """Une analyse a réellement été reçue (POST 2xx) pendant le tick."""
    tick = _current_tick
    if tick is None:
        return
    with _journal_guard:
        tick.analyses += 1
        tick.candidates += max(0, int(candidates or 0))


def note_proposal() -> None:
    """Une proposition a réellement été reçue (POST 2xx) pendant le tick."""
    tick = _current_tick
    if tick is None:
        return
    with _journal_guard:
        tick.proposals += 1


def current_journal() -> list[dict]:
    tick = _current_tick
    if tick is None:
        return []
    with _journal_guard:
        return [dict(item) for item in tick.journal]


# --------------------------------------------------------------------------
# Appel au provider
# --------------------------------------------------------------------------


def _post_chat(provider: dict, api_key: str | None, prompt: str) -> httpx.Response:
    base_url = (provider.get("base_url") or "").strip().rstrip("/")
    payload = {
        "model": provider.get("model") or "hermes-agent",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    extra = provider.get("extra_body") or {}
    if isinstance(extra, dict):
        payload.update(extra)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = httpx.Client(
        timeout=httpx.Timeout(connect=15.0, read=_read_timeout_s, write=30.0, pool=15.0)
    )
    with _clients_guard:
        _live_clients.add(client)
    try:
        return client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
    finally:
        with _clients_guard:
            _live_clients.discard(client)
        client.close()


def _cancel_http() -> None:
    """Annule l'appel au provider : ferme le client HTTP en vol."""
    with _clients_guard:
        clients = list(_live_clients)
    for client in clients:
        try:
            client.close()
        except Exception:  # pragma: no cover - fermeture best-effort
            pass


def _post_chat_before(provider: dict, api_key: str | None, prompt: str, tick: _Tick):
    """Appelle le provider sans jamais dépasser la borne du tick.

    Le thread d'appel est isolé : au-delà de l'échéance, le client HTTP est
    fermé (annulation) et le tick est clos en ``timeout``, verrou rendu.
    """
    global _read_timeout_s
    remaining = max(1.0, min(float(TICK_TIMEOUT_S), tick.remaining_s()))
    _read_timeout_s = remaining
    box: dict = {}

    def _call() -> None:
        try:
            box["response"] = _post_chat(provider, api_key, prompt)
        except BaseException as exc:  # annulation ou erreur réseau
            box["error"] = exc

    worker = threading.Thread(target=_call, name="fossor-veille-http", daemon=True)
    worker.start()
    worker.join(remaining)
    if worker.is_alive():
        _cancel_http()
        worker.join(5)
        return None, "timeout"
    if "error" in box:
        exc = box["error"]
        if isinstance(exc, httpx.HTTPError):
            return None, f"connexion impossible : {exc}"
        return None, f"appel interrompu : {exc}"
    return box.get("response"), ""


# --------------------------------------------------------------------------
# Compteurs réels
# --------------------------------------------------------------------------


def _db_counts(db: Session, since_iso: str) -> tuple[int, int, int]:
    """Ce que la base a réellement enregistré pendant la fenêtre du tick."""
    rows = db.query(Analysis).filter(Analysis.ts >= since_iso).all()
    candidates = sum(len(row.candidates or []) for row in rows)
    ids = [row.id for row in rows]
    proposals = 0
    if ids:
        proposals = db.query(Campaign).filter(Campaign.analysis_id.in_(ids)).count()
    return len(rows), candidates, proposals


def _counts(db: Session, tick: _Tick | None, started: datetime) -> tuple[int, int, int]:
    """Compteurs : les dépôts reçus (journal) priment sur une lecture en base.

    Un agent peut déposer une analyse avec un ``ts`` antidaté : la lecture en
    base la manquerait alors que Fossor l'a bien reçue.  On prend donc le
    maximum entre le journal du tick et les lignes créées dans la fenêtre.
    """
    db_analyses, db_candidates, db_proposals = _db_counts(db, started.isoformat())
    if tick is None:
        return db_analyses, db_candidates, db_proposals
    with _journal_guard:
        received = (tick.analyses, tick.candidates, tick.proposals)
    return (
        max(received[0], db_analyses),
        max(received[1], db_candidates),
        max(received[2], db_proposals),
    )


# --------------------------------------------------------------------------
# Cycle de vie
# --------------------------------------------------------------------------


def run_tick(
    trigger: str,
    fossor_url: str,
    tick_id: str | None = None,
    started: datetime | None = None,
) -> dict:
    """Exécute un tick **synchrone** (thread séparé côté routeur).

    ``tick_id`` et ``started`` viennent de :func:`trigger` : c'est le même
    identifiant du POST jusqu'à l'état final.
    """
    from app.database import SessionLocal

    global _current_tick
    db = SessionLocal()
    started = started or _now()
    tick_id = tick_id or str(uuid_lib.uuid4())
    tick: _Tick | None = None
    try:
        limit = max_minutes(db)
        _current_tick = tick = _Tick(tick_id, started, limit)

        ok, reason = provider_ready(db)
        if not ok:
            state = {
                "id": tick_id,
                "started_at": started.isoformat(),
                "finished_at": _now().isoformat(),
                "duration_s": 0,
                "status": "error",
                "trigger": trigger,
                "result": reason,
                "analyses": 0,
                "candidates": 0,
                "proposals": 0,
                "max_minutes": limit,
                "journal": [],
            }
            _set_state(db, state)
            return state

        row = ai_config.get_provider(db, "primary")
        assert row is not None  # provider_ready() vient de le vérifier
        provider = ai_config.provider_dict(row) or {}
        api_key = ai_config.provider_secret(row)
        _set_state(
            db,
            {
                "id": tick_id,
                "started_at": started.isoformat(),
                "finished_at": None,
                "duration_s": None,
                "status": "running",
                "trigger": trigger,
                "result": "",
                "analyses": 0,
                "candidates": 0,
                "proposals": 0,
                "max_minutes": limit,
                "deadline_at": tick.deadline.isoformat(),
                "journal": [],
            },
        )

        response, error = _post_chat_before(provider, api_key, build_prompt(fossor_url), tick)

        if response is not None and response.status_code != 200:
            error = f"HTTP {response.status_code} : {response.text[:200]}"

        timed_out = error == "timeout"
        analyses, candidates, proposals = _counts(db, tick, started)
        finished = _now()
        if timed_out:
            status = "timeout"
            result = (
                f"tick clos après {limit} min (durée maximale) : la requête vers le "
                "provider a été annulée, le verrou est rendu"
            )
        elif error:
            status = "error"
            result = error[:500]
        elif analyses == 0:
            # L'agent a répondu, mais rien n'est arrivé dans Fossor : le tick
            # ne peut pas être « ok ».
            status = "ok_sans_analyse"
            result = (
                "tick terminé mais aucune analyse reçue : 0 POST /api/hermes/analyses "
                "enregistré pendant la fenêtre du tick"
            )
        else:
            status = "ok"
            result = "tick terminé"

        state = {
            "id": tick_id,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_s": int((finished - started).total_seconds()),
            "status": status,
            "trigger": trigger,
            "result": result,
            "analyses": analyses,
            "candidates": candidates,
            "proposals": proposals,
            "max_minutes": limit,
            "journal": current_journal(),
        }
        if status in ("ok_sans_analyse", "timeout", "interrupted"):
            state["warning"] = True
        if status == "ok" and response is not None:
            try:
                data = response.json()
                choice = (data.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                if content:
                    state["extract"] = " ".join(str(content).split())[:300]
            except (ValueError, AttributeError, IndexError):
                pass
        _set_state(db, state)
        return state
    finally:
        _current_tick = None
        db.close()


def trigger(db: Session, trigger: str, fossor_url: str) -> dict:
    """Démarre un tick en arrière-plan ; refuse si un tick tourne déjà."""
    if not _lock.acquire(blocking=False):
        state = get_state(db) or {}
        state["already_running"] = True
        return state
    try:
        state = get_state(db) or {}
        if state.get("status") == "running":
            _lock.release()
            state["already_running"] = True
            return state

        # Un seul identifiant : celui rendu par le POST, repris à chaque
        # écriture d'état par run_tick (début et fin).
        tick_id = str(uuid_lib.uuid4())
        started = _now()
        pending = {
            "id": tick_id,
            "started_at": started.isoformat(),
            "finished_at": None,
            "duration_s": None,
            "status": "running",
            "trigger": trigger,
            "result": "",
            "analyses": 0,
            "candidates": 0,
            "proposals": 0,
            "max_minutes": max_minutes(db),
            "journal": [],
        }
        _set_state(db, pending)

        def _worker() -> None:
            try:
                run_tick(trigger, fossor_url, tick_id=tick_id, started=started)
            finally:
                _lock.release()

        threading.Thread(target=_worker, name="fossor-veille", daemon=True).start()
        return {"id": tick_id, "status": "running", "trigger": trigger}
    except Exception:
        _lock.release()
        raise


def close_interrupted(db: Session | None = None) -> dict | None:
    """Clos en ``interrupted`` un tick resté ``running`` (redémarrage de l'app)."""
    from app.database import SessionLocal

    own = db is None
    session = db or SessionLocal()
    try:
        state = get_state(session)
        if not state or state.get("status") != "running":
            return None
        finished = _now()
        duration = None
        try:
            started = datetime.fromisoformat(str(state.get("started_at")))
            duration = int((finished - started).total_seconds())
        except (TypeError, ValueError):
            pass
        state.update(
            {
                "status": "interrupted",
                "finished_at": finished.isoformat(),
                "duration_s": duration,
                "result": "tick interrompu par un redémarrage de Fossor : verrou rendu",
                "warning": True,
                "journal": list(state.get("journal") or []),
            }
        )
        _set_state(session, state)
        return state
    finally:
        if own:
            session.close()


def _loop(fossor_url: str | None) -> None:
    from app.database import SessionLocal

    base = fossor_url or os.environ.get("FOSSOR_PUBLIC_URL") or (
        "http://127.0.0.1:" + os.environ.get("PORT", "8008")
    )
    while True:
        time.sleep(60)
        db = SessionLocal()
        try:
            ok, _reason = provider_ready(db)
            if ok and due(db):
                trigger(db, "cadence", base)
                from app import observability

                observability.mark_routine(
                    db,
                    "veille",
                    True,
                    "tick de cadence déclenché",
                    interval_s=max(3600, cadence_hours(db) * 3600),
                )
        except Exception:  # pragma: no cover - un tick en échec ne tue pas la boucle
            pass
        finally:
            db.close()


def start_scheduler(fossor_url: str | None = None) -> None:
    global _scheduler_started
    if _scheduler_started or os.environ.get("FOSSOR_VEILLE_SCHEDULER", "1") == "0":
        return
    _scheduler_started = True
    threading.Thread(target=_loop, args=(fossor_url,), name="fossor-veille-cadence", daemon=True).start()


def snapshot(db: Session) -> dict:
    state = get_state(db)
    if state and state.get("status") == "running" and _current_tick is not None:
        state = dict(state)
        tick = _current_tick
        with _journal_guard:
            state["journal"] = [dict(item) for item in tick.journal]
            state["analyses"] = tick.analyses
            state["candidates"] = tick.candidates
            state["proposals"] = tick.proposals
            state["deadline_at"] = tick.deadline.isoformat()
    rule = db.get(Rule, "watch_cadence")
    ok, reason = provider_ready(db)
    return {
        "last_tick": state,
        "running": bool(state and state.get("status") == "running"),
        "cadence_hours": cadence_hours(db),
        "cadence_enabled": bool(rule.enabled) if rule is not None else False,
        "max_minutes": max_minutes(db),
        "provider_ready": ok,
        "provider_reason": reason,
    }
