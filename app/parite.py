"""Parité avec ``mining.html`` — agrégats du tableau de bord.

Trois lectures, toutes **en lecture seule** :

* le **badge de mode** (``pearl`` ou ``spec <coin>``), repris de ``modeBadge`` ;
* le **dernier événement significatif** de campagne ou de veille ;
* les **campagnes en cours**, y compris celles importées de
  ``mining-status.json`` (lancées hors Fossor avant la bascule).

Le solde d'une campagne solo vient du nœud, lu par une commande en liste
blanche (``solo_getbalance``) ; partout ailleurs c'est la dernière valeur
importée, avec sa date.  Aucune écriture, aucune commande hors liste blanche.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import Campaign, CampaignEvent, Machine, MinedCampaign

#: Types d'événements qui méritent la carte « Dernier événement ».  Les ticks
#: de veille (toutes les 6 h) sont volontairement exclus : ils noieraient la
#: carte sous du bruit.
SIGNIFICANT_KINDS = frozenset(
    {
        "prolongation",
        "start",
        "stop",
        "end",
        "note",
        "decision",
        "bascule",
        "retour",
        "proposal",
        "approve",
        "launch",
        "fin",
    }
)

_SOLO_MACHINE_HINT = "155"
_GETBALANCE_LINE = re.compile(r"^-?\d+(?:[.,]\d+)?$")


def _s(value: Any, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def parse_iso(value: Any) -> datetime | None:
    """ISO 8601 → date UTC ; ``None`` si illisible."""
    text = _s(value, 40)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_ts(value: Any) -> str:
    """Date ISO courte pour l'affichage (``18/11 23:14Z``)."""
    moment = parse_iso(value)
    if moment is None:
        return _s(value, 40)
    return moment.strftime("%d/%m %H:%MZ")


def split_event(message: str) -> tuple[str, str]:
    """« Titre. Détail » → ``(titre, détail)`` sans inventer de texte."""
    text = " ".join(_s(message, 4000).split())
    if not text:
        return "", ""
    for index, char in enumerate(text):
        if char in ".!" and index + 1 < len(text) and text[index + 1] == " ":
            return text[: index + 1].strip(), text[index + 1 :].strip()
    return text, ""


def relative_age(value: Any, now: datetime | None = None) -> str:
    """Âge lisible : « il y a 3 j », « il y a 2 h », « il y a 5 min »."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    seconds = int(((now or datetime.now(timezone.utc)) - moment).total_seconds())
    if seconds < 60:
        return "à l'instant"
    if seconds < 3600:
        return f"il y a {seconds // 60} min"
    if seconds < 86400:
        return f"il y a {seconds // 3600} h"
    return f"il y a {seconds // 86400} j"


def event_entry(
    ts: Any, coin: str, kind: str, message: str, source: str, now: datetime | None = None
) -> dict[str, Any]:
    text, detail = split_event(message)
    return {
        "ts": _s(ts, 40),
        "coin": _s(coin, 40),
        "kind": _s(kind, 40),
        "text": text,
        "detail": detail,
        "source": source,
        "age": relative_age(ts, now),
    }


def collect_events(db: Session) -> list[dict[str, Any]]:
    """Tous les événements significatifs, du plus récent au plus ancien."""
    events: list[dict[str, Any]] = []

    for row in db.query(MinedCampaign).all():
        for item in row.events or []:
            if not isinstance(item, dict):
                continue
            kind = _s(item.get("type") or item.get("kind") or "note", 40)
            if kind not in SIGNIFICANT_KINDS:
                continue
            events.append(
                event_entry(
                    item.get("ts"),
                    row.coin_ticker,
                    kind,
                    _s(item.get("msg") or item.get("message"), 4000),
                    "import",
                )
            )

    rows = (
        db.query(CampaignEvent, Campaign.coin)
        .join(Campaign, Campaign.id == CampaignEvent.campaign_id)
        .order_by(CampaignEvent.id.desc())
        .limit(200)
        .all()
    )
    for event, coin in rows:
        if event.kind not in SIGNIFICANT_KINDS:
            continue
        events.append(
            event_entry(event.ts, coin, event.kind, event.message, "fossor")
        )

    events.sort(key=lambda item: parse_iso(item["ts"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return events


def last_event(db: Session) -> dict[str, Any] | None:
    events = collect_events(db)
    return events[0] if events else None


def mode_badge(db: Session) -> dict[str, Any]:
    """``pearl`` par défaut, ``spec <coin>`` dès qu'une campagne importée tourne."""
    rows = db.query(MinedCampaign).filter(MinedCampaign.state == "active").all()
    # Le badge suit la bascule **GPU** : une campagne CPU (WAM, KORSH) laisse
    # Pearl sur les cartes et ne doit donc pas afficher « spec ».
    rows = [
        row
        for row in rows
        if (row.coin_ticker or "").upper() not in ("", "PRL", "PEARL")
        and "cpu" not in f"{row.algo or ''} {row.machine or ''}".lower()
    ]
    if not rows:
        return {"kind": "pearl", "label": "pearl", "coin": "", "since": ""}
    rows.sort(key=lambda row: parse_iso(row.start_iso) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    row = rows[0]
    ticker = (row.coin_ticker or "").upper()
    return {"kind": "spec", "label": f"spec {ticker}", "coin": ticker, "since": row.start_iso or ""}


def _mode_of(row: MinedCampaign) -> str:
    pool = (row.pool or "").lower()
    machine = (row.machine or "").lower()
    if "solo" in pool:
        return "solo"
    if "vm" in machine or "specbox" in machine:
        return "vm"
    return "host"


def _extensions(row: MinedCampaign) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in row.events or []:
        if not isinstance(item, dict):
            continue
        if _s(item.get("type") or item.get("kind"), 40) not in ("prolongation", "extension"):
            continue
        out.append({"ts": _s(item.get("ts"), 40), "text": _s(item.get("msg") or item.get("message"), 2000)})
    out.sort(key=lambda item: parse_iso(item["ts"]) or datetime.min.replace(tzinfo=timezone.utc))
    return out


def _last_note(row: MinedCampaign) -> str:
    notes = _s(row.notes, 4000)
    if notes:
        # Le fichier d'état sépare ses entrées par « | » : la dernière est la note du jour.
        parts = [part.strip() for part in notes.split("|") if part.strip()]
        return parts[-1] if parts else notes
    return _s(row.verdict, 2000)


def imported_campaigns(db: Session) -> list[dict[str, Any]]:
    rows = db.query(MinedCampaign).filter(MinedCampaign.state == "active").all()
    out: list[dict[str, Any]] = []
    for row in rows:
        extensions = _extensions(row)
        end_iso = _s(row.end_iso, 40)
        unlimited = not end_iso or "cpu" in (row.algo or "").lower()
        out.append(
            {
                "key": row.legacy_key,
                "coin": row.coin_ticker,
                "ticker": row.coin_ticker,
                "name": row.coin_name or row.coin_ticker,
                "machine": row.machine or "",
                "mode": _mode_of(row),
                "origin": "import",
                "status": "running",
                "start_at": row.start_iso,
                "end_at": None if unlimited else end_iso,
                "unlimited": unlimited,
                "extensions": extensions,
                "extension_count": len(extensions),
                "hashrate": row.hashrate or "",
                "balance": row.last_balance,
                "balance_unit": row.mined_unit or row.coin_ticker,
                "balance_source": row.balance_note or "dernière valeur importée",
                "balance_ts": row.updated_at,
                "note": _last_note(row),
                "solo": _mode_of(row) == "solo",
                "algo": row.algo or "",
            }
        )
    out.sort(key=lambda item: parse_iso(item["start_at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def fossor_campaigns(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(Campaign)
        .filter(Campaign.status.in_(("running", "stopping")))
        .order_by(Campaign.id.desc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "key": f"campaign:{row.id}",
                "coin": row.coin,
                "ticker": row.coin,
                "name": row.coin,
                "machine": "",
                "mode": "vm" if (row.execution_mode or "") in ("vm", "hybrid") else "host",
                "origin": row.origin or "manual",
                "status": "running",
                "start_at": row.started_at or row.start_at,
                "end_at": row.end_at,
                "unlimited": not row.end_at,
                "extensions": [],
                "extension_count": 0,
                "hashrate": "",
                "balance": row.start_balance,
                "balance_unit": row.balance_unit or row.coin,
                "balance_source": row.balance_source or "valeur de départ",
                "balance_ts": row.updated_at,
                "note": _s(row.note, 2000),
                "solo": False,
                "algo": "",
            }
        )
    return out


def running_campaigns(db: Session) -> list[dict[str, Any]]:
    """Campagnes Fossor + campagnes importées encore actives."""
    rows = fossor_campaigns(db) + imported_campaigns(db)
    rows.sort(key=lambda item: parse_iso(item["start_at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


def overview(db: Session) -> dict[str, Any]:
    return {
        "mode": mode_badge(db),
        "last_event": last_event(db),
        "campaigns": running_campaigns(db),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_getbalance(stdout: str) -> float | None:
    """Dernière ligne numérique de ``<coin>-cli getbalance`` (aucune heuristique)."""
    for line in reversed((stdout or "").splitlines()):
        text = line.strip().replace(",", ".")
        if _GETBALANCE_LINE.match(text):
            try:
                return float(text)
            except ValueError:
                continue
    return None


def solo_machine(db: Session, ticker: str = "BIG") -> Machine | None:
    """Machine qui héberge le nœud solo (BigCoin : le Ryzen ``.155``)."""
    target = (ticker or "").upper()
    if target != "BIG":
        return None
    for machine in db.query(Machine).all():
        host = _s(machine.host, 100)
        name = _s(machine.name, 100).lower()
        if _SOLO_MACHINE_HINT in host or "ryen" in name or "ryzen" in name:
            return machine
    return None
