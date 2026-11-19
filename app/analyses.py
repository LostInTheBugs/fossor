"""Analyses de veille (§4.9) : normalisation, empreinte, import idempotent.

Le format d'entrée accepté est celui de Hermes **et** celui de l'historique
``mining-analyses.json`` (``nom``/``lien``/``action_detail``).  Tout est ramené
au schéma de la spec : ``ts``, ``run``, ``candidates[{name,ticker,algo,links,
verdict,pour,contre}]``, ``action``, ``detail``.

L'empreinte SHA-256 du contenu normalisé est la clé d'idempotence : rejouer le
même fichier ne crée aucun doublon, même si les entrées arrivent dans un autre
ordre.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import Analysis

VERDICTS = ("retenu", "surveillance", "rejete")
ACTIONS = ("rien", "bascule", "prolongation", "retour")
MAX_DOWNLOAD = 5 * 1024 * 1024
_TICKER_NAME = re.compile(r"^\s*([A-Za-z0-9]{2,12})\s*[\(\[](.+?)[\)\]]\s*$")


class AnalysisError(ValueError):
    """Document d'analyse invalide — message affichable tel quel."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _strip_accents(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char)
    )


def normalize_verdict(value: Any) -> str:
    text = _strip_accents(_text(value).lower())
    if text.startswith("retenu"):
        return "retenu"
    if text.startswith("surveill"):
        return "surveillance"
    if text.startswith("rejet"):
        return "rejete"
    return "rejete"


def normalize_action(value: Any) -> str:
    text = _strip_accents(_text(value).lower())
    return text if text in ACTIONS else "rien"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [_text(item, 2000) for item in value if _text(item, 2000)]


def split_ticker_name(raw: str) -> tuple[str, str]:
    """« NEB (Neural Engine Byte) » → ``("NEB", "Neural Engine Byte")``."""
    match = _TICKER_NAME.match(raw)
    if match:
        return match.group(1).upper()[:20], match.group(2).strip()
    return "", raw.strip()


def normalize_candidate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AnalysisError("candidat invalide : objet JSON attendu")
    label = _text(raw.get("name") or raw.get("nom"))
    ticker = _text(raw.get("ticker")).upper()[:20]
    if not ticker:
        ticker, label = split_ticker_name(label)
    links = _string_list(raw.get("links") or raw.get("lien"))
    candidate = {
        "name": label or ticker or "?",
        "ticker": ticker,
        "algo": _text(raw.get("algo"), 200),
        "links": links,
        "verdict": normalize_verdict(raw.get("verdict")),
        "pour": _string_list(raw.get("pour")),
        "contre": _string_list(raw.get("contre")),
    }
    age = raw.get("age")
    if age not in (None, ""):
        candidate["age"] = _text(age, 80)
    return candidate


def normalize_analysis(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AnalysisError("analyse invalide : objet JSON attendu")
    candidates = raw.get("candidates") or []
    if not isinstance(candidates, list):
        raise AnalysisError("candidates doit être une liste")
    normalized = {
        "ts": _text(raw.get("ts"), 40) or _now(),
        "run": _text(raw.get("run"), 80),
        "action": normalize_action(raw.get("action")),
        "detail": _text(raw.get("action_detail") or raw.get("detail"), 8000),
        "delivered": bool(raw.get("delivered", False)),
        "candidates": [normalize_candidate(item) for item in candidates],
    }
    return normalized


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = {
        "ts": payload.get("ts", ""),
        "run": payload.get("run", ""),
        "action": payload.get("action", "rien"),
        "detail": payload.get("detail", ""),
        "delivered": bool(payload.get("delivered", False)),
        "candidates": payload.get("candidates", []),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_document(document: Any) -> list[dict[str, Any]]:
    """Accepte ``{"analyses": [...]}``, une liste, ou une analyse seule."""
    if isinstance(document, dict) and "analyses" in document:
        document = document["analyses"]
    if isinstance(document, dict):
        document = [document]
    if not isinstance(document, list):
        raise AnalysisError("document invalide : liste d'analyses attendue")
    if not document:
        raise AnalysisError("document vide : aucune analyse")
    return [normalize_analysis(item) for item in document]


def existing_fingerprints(db: Session) -> set[str]:
    return {row[0] for row in db.query(Analysis.fingerprint).all()}


def plan_import(db: Session, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Prévisualisation : ce qui serait créé et ce qui existe déjà."""
    known = existing_fingerprints(db)
    batch: set[str] = set()
    to_create: list[dict[str, Any]] = []
    duplicates = 0
    for entry in entries:
        print_ = fingerprint(entry)
        if print_ in known or print_ in batch:
            duplicates += 1
            continue
        batch.add(print_)
        to_create.append({**entry, "fingerprint": print_})
    return {"total": len(entries), "new": len(to_create), "duplicates": duplicates, "entries": to_create}


def store_entries(db: Session, entries: list[dict[str, Any]], source: str = "import") -> int:
    """Écrit les analyses nouvelles ; renvoie le nombre réellement créé."""
    known = existing_fingerprints(db)
    created = 0
    for entry in entries:
        print_ = fingerprint(entry)
        if print_ in known:
            continue
        db.add(
            Analysis(
                ts=entry["ts"],
                run=entry["run"],
                action=entry["action"],
                detail=entry["detail"],
                candidates=entry["candidates"],
                delivered=entry["delivered"],
                source=source,
                fingerprint=print_,
            )
        )
        known.add(print_)
        created += 1
    if created:
        db.commit()
    return created


def analysis_to_dict(analysis: Analysis) -> dict[str, Any]:
    return {
        "id": analysis.id,
        "ts": analysis.ts,
        "run": analysis.run,
        "action": analysis.action,
        "detail": analysis.detail,
        "delivered": bool(analysis.delivered),
        "source": analysis.source,
        "candidates": analysis.candidates or [],
        "created_at": analysis.created_at,
    }


def fetch_document(url: str) -> Any:
    """Télécharge un JSON d'analyses depuis l'URL saisie dans l'écran d'import."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise AnalysisError("URL invalide : http ou https attendu")
    if not parsed.netloc:
        raise AnalysisError("URL invalide : hôte manquant")
    request = urllib.request.Request(url, headers={"User-Agent": "Fossor-import/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(MAX_DOWNLOAD + 1)
    except Exception as exc:  # réseau : message clair, jamais de trace
        raise AnalysisError(f"téléchargement impossible : {exc}") from exc
    if len(raw) > MAX_DOWNLOAD:
        raise AnalysisError("document trop volumineux (5 Mio maximum)")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"JSON invalide : {exc}") from exc
