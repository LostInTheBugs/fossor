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

from app import miner_catalogue as MC

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


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


def parse_ts(value: Any, default_year: int | None = None) -> datetime | None:
    """Horodatage d'analyse → ``datetime`` comparable, ou ``None``.

    L'historique de veille mélange les écritures : ISO avec ``Z`` ou un décalage
    (``2026-10-29T23:43:00+01:00``, ``2026-10-27T17:02+0100``), et des dates
    jour/mois saisies à la main (``17/11 05:50``). Comparer ces chaînes classe
    « 17/11 05:50 » **avant** toute date ISO (le « 1 » est plus petit que le
    « 2 » de l'année) : un verdict « retenu » récent était alors écrasé par une
    surveillance ancienne. On compare donc des instants, jamais du texte.
    """
    text = _text(value, 40)
    if not text:
        return None
    iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text.replace("Z", "+00:00"))
    try:
        moment: datetime | None = datetime.fromisoformat(iso)
    except ValueError:
        moment = None
    if moment is None:
        for fmt in _TS_FORMATS:
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if moment is None:
        short = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
        if short and default_year:
            moment = datetime(
                default_year,
                int(short.group(2)),
                int(short.group(1)),
                int(short.group(3) or 0),
                int(short.group(4) or 0),
                tzinfo=timezone.utc,
            )
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stamp_key(value: Any, default_year: int) -> tuple[int, datetime, str]:
    """Clé de tri stable : instants d'abord, texte non datable ensuite."""
    text = _text(value, 40)
    moment = parse_ts(text, default_year=default_year)
    if moment is None:
        return (0, _EPOCH, text)
    return (1, moment, text)


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
    """« NEB (Neural Engine Byte) » → ``("NEB", "Neural Engine Byte")``.

    Les deux conventions existent dans l'historique : « NEB (Neural Engine
    Byte) » met le ticker devant, « Nival (NVL) » le met entre parenthèses. Le
    membre le plus court est le ticker (un nom de token dépasse rarement trois
    caractères) — sinon « NIVAL (NVL) » ressortait avec le ticker « NIVAL » et
    le nom « NVL », inversés à l'écran.
    """
    match = _TICKER_NAME.match(raw)
    if match:
        left, inner = match.group(1).strip(), match.group(2).strip()
        if len(_norm_key(inner)) < len(_norm_key(left)) and len(_norm_key(inner)) <= 12:
            return inner.upper()[:20], left
        return left.upper()[:20], inner
    return "", raw.strip()


def normalize_candidate(raw: Any, strict: bool = False) -> dict[str, Any]:
    """Candidat normalisé ; ``strict`` exige ``reason`` (et la grille si retenu/rejeté).

    Le mode strict ne sert qu'à ``POST /api/hermes/analyses`` : un import
    d'historique reste rétrocompatible, ses évaluations sans raison portent la
    mention « raison non renseignée (import) » à l'affichage.
    """
    if not isinstance(raw, dict):
        raise AnalysisError("candidat invalide : objet JSON attendu")
    label = _text(raw.get("name") or raw.get("nom"))
    ticker = _text(raw.get("ticker")).upper()[:20]
    if not ticker:
        ticker, label = split_ticker_name(label)
    links = _string_list(raw.get("links") or raw.get("lien"))
    verdict = normalize_verdict(raw.get("verdict"))
    reason = _text(raw.get("reason") or raw.get("raison"), 2000)
    criteria = normalize_criteria(raw.get("criteria"))
    name = label or ticker or "?"
    if strict:
        if not reason:
            raise AnalysisError(
                f"candidat {ticker or name} : reason obligatoire "
                "(1 à 2 phrases : pourquoi ce verdict, ce qui le ferait changer)"
            )
        if verdict in ("retenu", "rejete") and not criteria:
            raise AnalysisError(
                f"candidat {ticker or name} : criteria obligatoire pour un verdict « {verdict} »"
            )
    candidate = {
        "name": name,
        "ticker": ticker,
        "algo": _text(raw.get("algo"), 200),
        "links": links,
        "verdict": verdict,
        "reason": reason,
        "pour": _string_list(raw.get("pour")),
        "contre": _string_list(raw.get("contre")),
    }
    # Grille critère par critère (facultative hors strict : les analyses
    # importées d'avant n'en ont pas et restent « non évalué » — rien n'est inventé).
    if criteria:
        candidate["criteria"] = criteria
    age = raw.get("age")
    if age not in (None, ""):
        candidate["age"] = _text(age, 80)
    # 6.4 : identification catalogue + contradiction « standard » / « aucun profil ».
    if candidate.get("algo"):
        candidate["catalogue"] = MC.review_candidate(candidate)
    return candidate


def normalize_analysis(raw: Any, strict: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AnalysisError("analyse invalide : objet JSON attendu")
    candidates = raw.get("candidates") or []
    if not isinstance(candidates, list):
        raise AnalysisError("candidates doit être une liste")
    normalized = {
        "ts": _text(raw.get("ts"), 40) or _now(),
        "run": _text(raw.get("run"), 80),
        "action": normalize_action(raw.get("action")),
        "detail": _text(
            raw.get("action_detail") or raw.get("detail") or raw.get("reason"), 8000
        ),
        "delivered": bool(raw.get("delivered", False)),
        "candidates": [normalize_candidate(item, strict=strict) for item in candidates],
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
    entries = [normalize_analysis(item) for item in document]
    # 2026.11.021 : une analyse sans candidat doit dire pourquoi (l'analyse 63
    # est arrivée vide en prod). Le refus est un 422 côté route d'import.
    for entry in entries:
        if not entry["candidates"] and not entry["detail"]:
            raise AnalysisError(
                "analyse sans candidat : « detail » (ou « reason ») obligatoire "
                f"({entry.get('ts') or entry.get('run') or 'sans date'})"
            )
    return entries


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


_LAUNCH_NEG = re.compile(
    r"pas (encore )?lanc|non lanc|pas avant|pr[ée]vu[es]? (le|pour|en|d[ée]but)|attendu[es]? (le|pour|en)",
    re.IGNORECASE,
)
_LAUNCH_POS = re.compile(
    r"mainnet (actif|en ligne|d[ée]j[àa] lanc)|est (actif|en ligne|d[ée]j[àa] lanc)|d[ée]j[àa] lanc[ée]",
    re.IGNORECASE,
)
_MONTHS = r"janv|f[ée]vr|mars|avr|mai|juin|juil|ao[uû]t|sept|oct|nov|d[ée]c"
_MAINNET_DATE = re.compile(
    r"mainnet[^.;\n]{0,40}?(\d{1,2})(?:er)?\s+(" + _MONTHS + r")",
    re.IGNORECASE,
)


def _short(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _contradiction(previous_text: str, text: str) -> str:
    """Signaux factuels opposés entre deux évaluations (« pas lancé » → « actif »)."""
    previous_text = previous_text or ""
    text = text or ""
    prev_neg = bool(_LAUNCH_NEG.search(previous_text))
    cur_neg = bool(_LAUNCH_NEG.search(text))
    prev_pos = bool(_LAUNCH_POS.search(previous_text))
    cur_pos = bool(_LAUNCH_POS.search(text))
    if (prev_neg and cur_pos) or (prev_pos and cur_neg):
        return f"« {_short(previous_text)} » puis « {_short(text)} »"
    left = _MAINNET_DATE.search(previous_text)
    right = _MAINNET_DATE.search(text)
    if left and right and left.group(0).lower() != right.group(0).lower():
        return f"« {_short(previous_text)} » puis « {_short(text)} »"
    return ""


def coherence_warnings(
    verdict: str,
    contre: list[str],
    reason: str,
    criteria: list[dict[str, str]],
    previous: dict[str, Any] | None = None,
    text: str = "",
) -> list[str]:
    """Contrôles de cohérence **affichés**, jamais bloquants.

    Une note d'exploitation (« jobs cron armés ») n'est pas un argument sur le
    token : un verdict « surveillance » sans aucun « contre » ni raison est
    signalé, et une contradiction factuelle avec l'évaluation précédente aussi.
    """
    warnings: list[str] = []
    label = VERDICT_LABELS.get(verdict, verdict)
    if verdict in ("surveillance", "rejete") and not contre and not reason:
        warnings.append(
            f"verdict « {label} » sans aucun argument « contre » ni raison : il n'est pas justifié."
        )
    if verdict == "retenu":
        kos = [item.get("key", "") for item in criteria if item.get("verdict") == "ko"]
        if kos:
            warnings.append(
                "verdict « retenu » alors que le critère " + ", ".join(f"« {key} »" for key in kos) + " est ko."
            )
    if previous:
        previous_label = VERDICT_LABELS.get(previous.get("verdict", ""), previous.get("verdict", ""))
        if previous.get("verdict") != verdict and not reason:
            warnings.append(
                f"verdict changé ({previous_label} → {label}) sans raison : l'écart n'est pas expliqué."
            )
        contradictory = _contradiction(previous.get("detail", ""), text)
        if contradictory and not reason:
            warnings.append(f"contredit l'évaluation précédente sans raison : {contradictory}.")
    return warnings


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
    years = [dt.year for dt in (parse_ts(item.get("ts")) for item in analyses) if dt is not None]
    default_year = max(years) if years else datetime.now(timezone.utc).year
    ordered = sorted(
        enumerate(analyses),
        key=lambda item: (_stamp_key(item[1].get("ts"), default_year), item[0]),
    )
    first_keys: dict[str, tuple[int, datetime, str]] = {}
    last_keys: dict[str, tuple[int, datetime, str]] = {}
    for _index, analysis in ordered:
        ts = _text(analysis.get("ts"), 40)
        stamp = _stamp_key(ts, default_year)
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
                    "reason": "",
                    "detail": "",
                    "warnings": [],
                    "catalogue": None,
                    "aliases": [],
                    "pour": [],
                    "contre": [],
                    "evaluations": 0,
                    "history": [],
                }
            verdict = verdict_key(candidate.get("verdict"))
            contre = _string_list(candidate.get("contre"))
            criteria = normalize_criteria(candidate.get("criteria"))
            reason = _text(candidate.get("reason") or candidate.get("raison"), 2000)
            if not reason and analysis.get("source") == "import":
                reason = "raison non renseignée (import)"
            detail = _text(analysis.get("detail"), 1000)
            previous = row["history"][-1] if row["history"] else None
            review = candidate.get("catalogue")
            if not review and candidate.get("algo"):
                review = MC.review_candidate(candidate)
            warnings = coherence_warnings(verdict, contre, reason, criteria, previous, detail)
            if review:
                warnings += [w for w in review.get("warnings") or [] if w not in warnings]
            entry = {
                "ts": ts,
                "run": _text(analysis.get("run"), 80),
                "source": _text(analysis.get("source"), 20) or "hermes",
                "verdict": verdict,
                "verdict_label": VERDICT_LABELS.get(verdict, _text(candidate.get("verdict"), 40) or verdict),
                "age": _text(candidate.get("age"), 80),
                "reason": reason,
                "detail": detail,
                "pour": _string_list(candidate.get("pour")),
                "contre": contre,
                "criteria": criteria,
                "warnings": warnings,
                "catalogue": review,
            }
            for link in _string_list(candidate.get("links")):
                if link not in row["links"]:
                    row["links"].append(link)
            row["history"].append(entry)
            row["evaluations"] += 1
            if ts and (key not in first_keys or stamp < first_keys[key]):
                first_keys[key] = stamp
                row["first_ts"] = ts
            if key not in last_keys or stamp >= last_keys[key]:
                last_keys[key] = stamp
                row["last_ts"] = ts
                row["verdict"] = verdict
                row["verdict_label"] = entry["verdict_label"]
                row["age"] = entry["age"]
                row["reason"] = entry["reason"]
                row["detail"] = entry["detail"]
                row["warnings"] = entry["warnings"]
                row["pour"] = entry["pour"]
                row["contre"] = entry["contre"]
                row["catalogue"] = entry.get("catalogue")
                row["ticker"] = ticker or row["ticker"]
                row["name"] = name or row["name"]
                row["algo"] = _text(candidate.get("algo"), 200) or row["algo"]
    for row in rows.values():
        row["history"].reverse()
        row["aliases"] = [
            {
                "canonical": item.get("canonical"),
                "member": item.get("member"),
                "mode": item.get("mode") or "merge",
            }
            for item in (aliases or [])
            if uf.find(_norm_key(item.get("canonical"))) == row["key"]
        ]
    return sorted(
        rows.values(),
        key=lambda item: _stamp_key(item["last_ts"], default_year),
        reverse=True,
    )


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
