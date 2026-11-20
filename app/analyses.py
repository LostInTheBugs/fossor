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
    # Grille critère par critère (facultative : les analyses importées d'avant
    # n'en ont pas et restent « non évalué » — rien n'est inventé).
    criteria = normalize_criteria(raw.get("criteria"))
    if criteria:
        candidate["criteria"] = criteria
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


VERDICT_LABELS = {"retenu": "retenu", "surveillance": "surveillance", "rejete": "rejeté"}
BASE_VERDICTS = ("retenu", "surveillance", "rejete")
CRITERION_VERDICTS = ("ok", "warn", "ko", "na")


def verdict_key(value: Any) -> str:
    """Clé de verdict stable ; « rejeté » accentué vaut « rejete »."""
    raw = _text(value)
    text = _strip_accents(raw.lower())
    if text.startswith("retenu"):
        return "retenu"
    if text.startswith("surveill"):
        return "surveillance"
    if text.startswith("rejet") or not text:
        return "rejete"
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "rejete"


def normalize_criteria(value: Any) -> list[dict[str, str]]:
    """``[{key, verdict: ok|warn|ko|na, note}]`` — ordre conservé."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = _norm_key(item.get("key"))
        if not key:
            continue
        verdict = _strip_accents(_text(item.get("verdict"), 20).lower())
        if verdict not in CRITERION_VERDICTS:
            verdict = "na"
        out.append({"key": key, "verdict": verdict, "note": _text(item.get("note"), 500)})
    return out


def _norm_key(value: Any) -> str:
    """Clé de token : minuscules, accents et ponctuation retirés."""
    text = _strip_accents(_text(value, 200).lower())
    return re.sub(r"[^a-z0-9]+", "", text)


def _paren_contents(label: str) -> list[str]:
    return [group for group in re.findall(r"[\(\[](.+?)[\)\]]", label or "") if group.strip()]


def _words(label: str) -> set[str]:
    parts = re.split(r"[^0-9A-Za-z\u00c0-\u024f]+", label or "")
    return {_norm_key(part) for part in parts if len(_norm_key(part)) >= 3}


def candidate_identifiers(candidate: dict[str, Any]) -> dict[str, Any]:
    """Identifiants d'une évaluation : ticker, nom, contenu de parenthèses, mots.

    « NEX Chain (NEXC) » produit ``nexchain``, ``nexc`` et les mots
    ``nex``/``chain`` ; « BIG (BigCoin) » produit ``big`` et ``bigcoin``.
    """
    label = _text(candidate.get("name"), 200)
    tickers = {key for key in (_norm_key(candidate.get("ticker")),) if key}
    inner = {_norm_key(item) for item in _paren_contents(label) if _norm_key(item)}
    outer = re.sub(r"[\(\[](.+?)[\)\]]", " ", label)
    name = _norm_key(outer)
    words = _words(outer)
    names = ({name} if name else set()) | inner | words
    tickers |= inner
    return {
        "tickers": tickers,
        # Les mots ne sont *pas* des nœuds du graphe : « chain » ou « coin »
        # rapprocheraient des tokens sans rapport. Ils servent seulement à
        # reconnaître un ticker (« BIG » dans « bigcoin big »).
        "names": ({name} if name else set()) | inner,
        "words": words,
        "phrase": name,
        "all": tickers | ({name} if name else set()) | inner,
    }


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, key: str) -> None:
        self.parent.setdefault(key, key)

    def find(self, key: str) -> str:
        self.add(key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, left: str, right: str, blocked: set[frozenset[str]] | None = None) -> None:
        if not left or not right:
            return
        if blocked and frozenset((left, right)) in blocked:
            return
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def token_groups(
    analyses: list[dict[str, Any]], aliases: list[dict[str, str]] | None = None
) -> _UnionFind:
    """Graphe des identifiants : heuristique ticker ↔ nom, puis alias manuels.

    Un alias ``split`` bloque le lien direct entre deux identifiants ; un alias
    ``merge`` en ajoute un.  Un composant = un token.
    """
    entries = list(aliases or [])
    blocked = {
        frozenset((_norm_key(item.get("canonical")), _norm_key(item.get("member"))))
        for item in entries
        if str(item.get("mode") or "merge") == "split"
    }
    uf = _UnionFind()
    tickers: set[str] = set()
    phrases: list[tuple[str, set[str]]] = []
    for analysis in analyses:
        for candidate in analysis.get("candidates") or []:
            ids = candidate_identifiers(candidate)
            for key in ids["all"]:
                uf.add(key)
                uf.union(sorted(ids["all"])[0], key, blocked)
            tickers |= ids["tickers"]
            if ids["phrase"]:
                phrases.append((ids["phrase"], ids["words"]))
    # Heuristique : un ticker qui est le nom (ou un mot du nom) d'une autre
    # évaluation désigne le même token ; de même pour un préfixe de nom
    # (NEXC → NEXCHAIN, BIG → BIGCOIN), jamais en deçà de trois caractères.
    for ticker in tickers:
        for phrase, words in phrases:
            if ticker == phrase or (len(ticker) >= 3 and ticker in words):
                # Correspondance exacte ticker ↔ nom, ou ticker présent comme
                # mot du nom (« bigcoin big »). Aucun préfixe : KPOWBIT ne doit
                # pas absorber KPOW ; les cas BIG/NEX/KSH passent par les
                # parenthèses de l'évaluation elle-même.
                uf.union(ticker, phrase, blocked)
    for item in entries:
        if str(item.get("mode") or "merge") == "split":
            continue
        canonical = _norm_key(item.get("canonical"))
        member = _norm_key(item.get("member"))
        if canonical and member and canonical != member:
            uf.union(canonical, member)
    return uf


def token_rows(
    analyses: list[dict[str, Any]], aliases: list[dict[str, str]] | None = None
) -> list[dict[str, Any]]:
    """Une ligne par token regroupé, agrégée sur toutes les analyses."""
    rows: dict[str, dict[str, Any]] = {}
    uf = token_groups(analyses, aliases)
    ordered = sorted(enumerate(analyses), key=lambda item: (item[1].get("ts") or "", item[0]))
    for _index, analysis in ordered:
        ts = _text(analysis.get("ts"), 40)
        for candidate in analysis.get("candidates") or []:
            ids = candidate_identifiers(candidate)
            if not ids["all"]:
                continue
            key = uf.find(sorted(ids["tickers"])[0] if ids["tickers"] else ids["phrase"])
            ticker = _text(candidate.get("ticker"), 20).upper()
            name = _text(candidate.get("name"), 200)
            row = rows.get(key)
            if row is None:
                row = rows[key] = {
                    "key": key,
                    "ticker": ticker,
                    "name": name or ticker,
                    "algo": "",
                    "links": [],
                    "verdict": "rejete",
                    "verdict_label": "rejeté",
                    "first_ts": ts,
                    "last_ts": ts,
                    "age": "",
                    "pour": [],
                    "contre": [],
                    "evaluations": 0,
                    "history": [],
                }
            verdict = verdict_key(candidate.get("verdict"))
            entry = {
                "ts": ts,
                "run": _text(analysis.get("run"), 80),
                "verdict": verdict,
                "verdict_label": VERDICT_LABELS.get(verdict, _text(candidate.get("verdict"), 40) or verdict),
                "age": _text(candidate.get("age"), 80),
                "pour": _string_list(candidate.get("pour")),
                "contre": _string_list(candidate.get("contre")),
                "criteria": normalize_criteria(candidate.get("criteria")),
            }
            for link in _string_list(candidate.get("links")):
                if link not in row["links"]:
                    row["links"].append(link)
            row["history"].append(entry)
            row["evaluations"] += 1
            if ts and (not row["first_ts"] or ts < row["first_ts"]):
                row["first_ts"] = ts
            if ts >= (row["last_ts"] or ""):
                row["last_ts"] = ts
                row["verdict"] = verdict
                row["verdict_label"] = entry["verdict_label"]
                row["age"] = entry["age"]
                row["pour"] = entry["pour"]
                row["contre"] = entry["contre"]
                row["ticker"] = ticker or row["ticker"]
                row["name"] = name or row["name"]
                row["algo"] = _text(candidate.get("algo"), 200) or row["algo"]
    for row in rows.values():
        row["history"].reverse()
    return sorted(rows.values(), key=lambda item: item["last_ts"] or "", reverse=True)


def verdict_catalog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Puces de filtre : les trois verdicts de base puis ceux présents dans les données."""
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for row in rows:
        key = row["verdict"]
        counts[key] = counts.get(key, 0) + 1
        labels[key] = row.get("verdict_label") or VERDICT_LABELS.get(key, key)
    order = list(BASE_VERDICTS) + sorted(key for key in counts if key not in BASE_VERDICTS)
    return [
        {"value": key, "label": labels.get(key, VERDICT_LABELS.get(key, key)), "count": counts.get(key, 0)}
        for key in order
    ]


def filter_token_rows(
    rows: list[dict[str, Any]], verdicts: list[str] | None = None, query: str = ""
) -> list[dict[str, Any]]:
    """Sélection multiple de verdicts (vide = tous) et recherche nom/ticker."""
    selected = {_text(item, 40) for item in (verdicts or []) if _text(item, 40)}
    needle = _strip_accents(_text(query, 120).lower())
    out: list[dict[str, Any]] = []
    for row in rows:
        if selected and row["verdict"] not in selected:
            continue
        if needle:
            haystack = _strip_accents(f"{row.get('ticker', '')} {row.get('name', '')}".lower())
            if needle not in haystack:
                continue
        out.append(row)
    return out
