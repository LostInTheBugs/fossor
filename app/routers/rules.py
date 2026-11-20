"""Routes des règles (§4.8) et des analyses (§4.9) — session admin.

- ``GET /api/rules``, ``PATCH /api/rules/{id}``, ``GET /api/rules/{id}/history``
- ``GET /api/analyses``, ``GET /api/analyses/{id}``
- ``POST /api/analyses/import`` (URL saisie ou fichier téléversé, ``dry_run``)

Les propositions Hermes vivent dans :mod:`app.routers.hermes` : elles ne
passent jamais par ces routes, qui exigent une session.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import analyses as A
from app import rules as R
from app.database import get_db
from app.dependencies import require_login
from app.models import Analysis, AuditLog, Rule, RuleChange

router = APIRouter(prefix="/api", tags=["rules"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(db: Session, actor: str, action: str, ref: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor=(actor or "fred")[:20],
            action=action[:50],
            object_ref=ref[:255],
            result=result[:200],
            output=output[:500],
        )
    )
    db.commit()


def _actor(user) -> str:
    if isinstance(user, str):
        return (user or "fred")[:20]
    return str(getattr(user, "username", "") or "fred")[:20]


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    params: dict | None = None


@router.get("/rules")
def list_rules(db: Session = Depends(get_db), _user=Depends(require_login)):
    rows = db.query(Rule).order_by(Rule.sort_order, Rule.id).all()
    return {"rules": [R.rule_to_dict(rule) for rule in rows], "scopes": list(R.SCOPES)}


@router.patch("/rules/{rule_id}")
def patch_rule(
    rule_id: str,
    body: RulePatch,
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    rule = db.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Règle {rule_id!r} introuvable")
    if body.enabled is None and body.params is None:
        raise HTTPException(status_code=422, detail="Aucun changement : enabled ou params attendu")

    before = {"enabled": bool(rule.enabled), "params": dict(rule.params or {})}
    after_enabled = before["enabled"] if body.enabled is None else bool(body.enabled)
    if body.params is None:
        after_params = before["params"]
    else:
        merged = {**before["params"], **body.params}
        try:
            after_params = R.validate_params(rule_id, merged)
        except R.RuleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if after_enabled == before["enabled"] and after_params == before["params"]:
        return {"rule": R.rule_to_dict(rule), "changed": False}

    rule.enabled = after_enabled
    rule.params = after_params
    rule.updated_at = _now()
    db.add(
        RuleChange(
            rule_id=rule_id,
            ts=rule.updated_at,
            actor=_actor(user),
            before=before,
            after={"enabled": after_enabled, "params": after_params},
        )
    )
    db.commit()
    _audit(
        db,
        _actor(user),
        "règle modifiée",
        f"rule:{rule_id}",
        "ok",
        f"enabled {before['enabled']}→{after_enabled} ; params {json.dumps(before['params'], ensure_ascii=False)[:180]}"
        f" → {json.dumps(after_params, ensure_ascii=False)[:180]}",
    )
    db.refresh(rule)
    return {"rule": R.rule_to_dict(rule), "changed": True}


@router.get("/rules/{rule_id}/history")
def rule_history(
    rule_id: str, limit: int = 50, db: Session = Depends(get_db), _user=Depends(require_login)
):
    if db.get(Rule, rule_id) is None:
        raise HTTPException(status_code=404, detail=f"Règle {rule_id!r} introuvable")
    rows = (
        db.query(RuleChange)
        .filter(RuleChange.rule_id == rule_id)
        .order_by(RuleChange.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "rule_id": rule_id,
        "changes": [
            {
                "id": row.id,
                "ts": row.ts,
                "actor": row.actor,
                "before": row.before or {},
                "after": row.after or {},
            }
            for row in rows
        ],
    }


@router.get("/analyses")
def list_analyses(
    limit: int = 50, db: Session = Depends(get_db), _user=Depends(require_login)
):
    rows = (
        db.query(Analysis)
        .order_by(Analysis.ts.desc(), Analysis.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {"analyses": [A.analysis_to_dict(row) for row in rows], "count": len(rows)}


@router.get("/analyses/tokens")
def token_table(
    verdicts: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Tableau « Tokens évalués » : une ligne par token, agrégée sur toutes les analyses."""
    rows = db.query(Analysis).order_by(Analysis.ts.asc(), Analysis.id.asc()).all()
    tokens = A.token_rows([A.analysis_to_dict(row) for row in rows])
    selected = [item.strip() for item in verdicts.split(",") if item.strip()]
    filtered = A.filter_token_rows(tokens, selected, q)
    return {
        "tokens": filtered,
        "total": len(tokens),
        "filtered": len(filtered),
        "evaluations": sum(row["evaluations"] for row in tokens),
        "verdicts": A.verdict_catalog(tokens),
    }


@router.get("/analyses/{analysis_id}")
def read_analysis(
    analysis_id: int, db: Session = Depends(get_db), _user=Depends(require_login)
):
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Analyse introuvable")
    return A.analysis_to_dict(row)


@router.post("/analyses/import")
async def import_analyses(
    dry_run: bool = Form(True),
    url: str = Form(""),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    """Prévisualise (``dry_run=true``) puis importe, sans doublon au rejeu."""
    if file is not None and file.filename:
        raw = await file.read(A.MAX_DOWNLOAD + 1)
        if len(raw) > A.MAX_DOWNLOAD:
            raise HTTPException(status_code=413, detail="Fichier trop volumineux (5 Mio maximum)")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"JSON invalide : {exc}") from exc
        source = f"fichier:{file.filename}"
    elif url.strip():
        try:
            document = A.fetch_document(url.strip())
        except A.AnalysisError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        source = "url"
    else:
        raise HTTPException(status_code=422, detail="Fournir un fichier ou une URL")

    try:
        entries = A.parse_document(document)
    except A.AnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    plan = A.plan_import(db, entries)
    result = {
        "dry_run": dry_run,
        "total": plan["total"],
        "new": plan["new"],
        "duplicates": plan["duplicates"],
        "source": source,
        "preview": [
            {
                "ts": entry["ts"],
                "run": entry["run"],
                "action": entry["action"],
                "candidates": [
                    {"ticker": c.get("ticker"), "name": c.get("name"), "verdict": c.get("verdict")}
                    for c in entry["candidates"]
                ],
            }
            for entry in plan["entries"][:20]
        ],
    }
    if dry_run:
        return result

    created = A.store_entries(db, entries, source="import")
    _audit(
        db,
        _actor(user),
        "analyses importées",
        source,
        "ok",
        f"{created} créée(s), {plan['duplicates']} doublon(s) ignoré(s)",
    )
    result["created"] = created
    return result
