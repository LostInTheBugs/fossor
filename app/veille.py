"""Veille spec mining pilotée par Fossor (§4.8, §4.12).

Fossor ne décide plus rien dans un skill : il **déclenche** un tick chez Hermes
(provider ``hermes-agent``), Hermes lit ``GET /api/hermes/context``, dépose une
analyse et, si un candidat est retenu, une proposition.  Fossor n'exécute
jamais de commande SSH ici : le tick ne peut ni lancer ni arrêter un mineur.

Deux déclencheurs :

* le bouton « Lancer une veille maintenant » (écran Analyse des tokens) ;
* la cadence de la règle ``watch_cadence`` — elle prend le relais **après** le
  premier tick manuel, pour qu'un déploiement ne déclenche pas d'appel surprise.

L'état du dernier tick (heure, durée, résultat, nombre de candidats) vit dans
la table ``settings`` (clé ``veille_last_tick``) : il est exporté avec le reste
de la configuration et ne contient aucun secret.
"""

from __future__ import annotations

import json
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
#: Un tick réel (recherche web + dépôts) dure plusieurs minutes ; le délai du
#: provider (§4.12, 120 s par défaut) ne sert qu'à sa sonde « Tester ».
TICK_TIMEOUT_S = 1800
_PROMPT_MAX = 1200

_lock = threading.Lock()
_scheduler_started = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_state(db: Session) -> dict | None:
    value = ai_config.get_setting(db, STATE_KEY, None)
    return value if isinstance(value, dict) else None


def _set_state(db: Session, state: dict) -> None:
    ai_config.set_setting(db, STATE_KEY, state)
    db.commit()


def build_prompt(fossor_url: str) -> str:
    """Consigne courte : le skill porte la méthode, Fossor porte les règles."""
    base = fossor_url.rstrip("/")
    text = (
        f"Veille Fossor ({base}) : charge le skill crypto-mining/spec-mining-fossor "
        "et exécute UN tick de veille maintenant. Lis d'abord "
        f"GET {base}/api/hermes/context (jeton Fossor dans FOSSOR_TOKEN ou "
        "~/.hermes/fossor/token). Dépose toujours une analyse via "
        f"POST {base}/api/hermes/analyses ; dépose une proposition via "
        f"POST {base}/api/hermes/proposals seulement si un candidat est retenu et "
        "minable. Aucune commande SSH vers une machine de Fred, aucun lancement. "
        "Termine par [SILENT] si rien n'est actionnable."
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
    with httpx.Client(timeout=TICK_TIMEOUT_S) as client:
        return client.post(f"{base_url}/chat/completions", json=payload, headers=headers)


def _new_counts(db: Session, since_iso: str) -> tuple[int, int, int]:
    rows = db.query(Analysis).filter(Analysis.ts >= since_iso).all()
    candidates = sum(len(row.candidates or []) for row in rows)
    ids = [row.id for row in rows]
    proposals = 0
    if ids:
        proposals = (
            db.query(Campaign)
            .filter(Campaign.analysis_id.in_(ids))
            .count()
        )
    return len(rows), candidates, proposals


def run_tick(trigger: str, fossor_url: str) -> dict:
    """Exécute un tick **synchrone** (thread séparé côté routeur)."""
    from app.database import SessionLocal

    db = SessionLocal()
    started = _now()
    try:
        ok, reason = provider_ready(db)
        if not ok:
            state = {
                "id": str(uuid_lib.uuid4()),
                "started_at": started.isoformat(),
                "finished_at": _now().isoformat(),
                "duration_s": 0,
                "status": "error",
                "trigger": trigger,
                "result": reason,
                "analyses": 0,
                "candidates": 0,
                "proposals": 0,
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
                "id": str(uuid_lib.uuid4()),
                "started_at": started.isoformat(),
                "finished_at": None,
                "duration_s": None,
                "status": "running",
                "trigger": trigger,
                "result": "",
                "analyses": 0,
                "candidates": 0,
                "proposals": 0,
            },
        )

        try:
            response = _post_chat(provider, api_key, build_prompt(fossor_url))
        except httpx.HTTPError as exc:
            response = None
            error = f"connexion impossible : {exc}"
        else:
            error = ""

        if response is not None and response.status_code != 200:
            error = f"HTTP {response.status_code} : {response.text[:200]}"

        # Le compteur est lu APRÈS l'appel : les analyses déposées pendant le
        # tick portent un ``ts`` postérieur au démarrage.
        analyses, candidates, proposals = _new_counts(db, started.isoformat())
        finished = _now()
        state = {
            "id": str(uuid_lib.uuid4()),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_s": int((finished - started).total_seconds()),
            "status": "error" if error else "ok",
            "trigger": trigger,
            "result": (error or "tick terminé")[:500],
            "analyses": analyses,
            "candidates": candidates,
            "proposals": proposals,
        }
        if not error and response is not None:
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
            state["already_running"] = True
            return state

        # L'état « running » est écrit AVANT le départ du thread : l'écran qui
        # interroge juste après le POST voit le tick en cours, pas l'ancien.
        pending = {
            "id": str(uuid_lib.uuid4()),
            "started_at": _now().isoformat(),
            "finished_at": None,
            "duration_s": None,
            "status": "running",
            "trigger": trigger,
            "result": "",
            "analyses": 0,
            "candidates": 0,
            "proposals": 0,
        }
        _set_state(db, pending)

        def _worker() -> None:
            try:
                run_tick(trigger, fossor_url)
            finally:
                _lock.release()

        threading.Thread(target=_worker, name="fossor-veille", daemon=True).start()
        return {"id": (get_state(db) or {}).get("id"), "status": "running", "trigger": trigger}
    except Exception:
        _lock.release()
        raise


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
    rule = db.get(Rule, "watch_cadence")
    ok, reason = provider_ready(db)
    return {
        "last_tick": state,
        "running": bool(state and state.get("status") == "running"),
        "cadence_hours": cadence_hours(db),
        "cadence_enabled": bool(rule.enabled) if rule is not None else False,
        "provider_ready": ok,
        "provider_reason": reason,
    }
