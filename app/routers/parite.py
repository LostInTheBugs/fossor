"""API de parité avec ``mining.html`` (tableau de bord et solde solo)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import parite
from app import ssh_client as SSH
from app.database import get_db
from app.dependencies import require_login

router = APIRouter(prefix="/api", tags=["parite"])

#: Cache court du solde solo : la page se recharge toutes les 30 s et un SSH
#: par rechargement serait du bruit sur le nœud.
_BALANCE_TTL_S = 60
_balance_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@router.get("/parite/overview")
def parite_overview(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Badge de mode, dernier événement et campagnes en cours (Fossor + import)."""
    return parite.overview(db)


@router.get("/parite/balance/{ticker}")
async def parite_balance(
    ticker: str, db: Session = Depends(get_db), user=Depends(require_login)
):
    """Solde du nœud solo, **lecture seule** (``getbalance`` en liste blanche)."""
    symbol = (ticker or "").upper()
    now = time.monotonic()
    cached = _balance_cache.get(symbol)
    if cached is not None and now - cached[0] < _BALANCE_TTL_S:
        return cached[1]

    machine = parite.solo_machine(db, symbol)
    if machine is None:
        result = {
            "ok": False,
            "ticker": symbol,
            "reason": "aucune machine de nœud configurée pour ce ticker",
            "source": "",
        }
    elif symbol != "BIG":
        result = {
            "ok": False,
            "ticker": symbol,
            "reason": f"lecture du nœud non câblée pour {symbol}",
            "source": "",
        }
    else:
        answer = await SSH.ssh_solo_getbalance(machine, actor=str(user or "system"))
        balance = parite.parse_getbalance(answer.get("stdout", "")) if answer.get("ok") else None
        result = {
            "ok": balance is not None,
            "ticker": symbol,
            "balance": balance,
            "unit": symbol,
            "source": f"nœud {machine.name} ({machine.host}) — getbalance",
            "reason": "" if balance is not None else (answer.get("error") or "réponse getbalance illisible"),
            "machine": machine.name,
            "host": machine.host,
        }
    _balance_cache[symbol] = (now, result)
    return result


@router.post("/parite/balance/{ticker}/refresh")
async def parite_balance_refresh(
    ticker: str, db: Session = Depends(get_db), user=Depends(require_login)
):
    """Force une relecture du nœud (bouton « Lire le nœud »)."""
    _balance_cache.pop((ticker or "").upper(), None)
    return await parite_balance(ticker, db, user)


@router.get("/parite/token-verdicts")
def parite_token_verdicts(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Compteurs de verdicts pour les puces de l'onglet « Tokens évalués »."""
    from app import analyses as A
    from app.models import Analysis, TokenAlias

    rows = db.query(Analysis).order_by(Analysis.ts.asc(), Analysis.id.asc()).all()
    aliases = [
        {"canonical": row.canonical, "member": row.member, "mode": row.mode}
        for row in db.query(TokenAlias).order_by(TokenAlias.id).all()
    ]
    tokens = A.token_rows([A.analysis_to_dict(row) for row in rows], aliases)
    return {"verdicts": A.verdict_catalog(tokens), "total": len(tokens)}
