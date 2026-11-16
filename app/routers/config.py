"""Configuration export/import API (§4.13)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config_io import ConfigError, apply_import, build_export, compute_diff, validate_document
from app.database import get_db
from app.dependencies import require_login
from app.models import ActorEnum, AuditLog

router = APIRouter(prefix="/api/config", tags=["config"])


def _filename(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"fossor-{prefix}-{stamp}.json"


def _attachment(doc: dict) -> JSONResponse:
    return JSONResponse(
        content=doc,
        headers={"Content-Disposition": f'attachment; filename="{_filename("config")}"'},
    )


@router.get("/export")
def export_config(db: Session = Depends(get_db), _user=Depends(require_login)):
    """Export without secrets (the default, safe for backups and support)."""
    return _attachment(build_export(db, include_secrets=False))


@router.post("/export")
def export_config_with_secrets(
    payload: dict,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Export, optionally with secrets encrypted by a passphrase.

    The passphrase travels in the body, never in the URL.
    """
    include_secrets = bool(payload.get("include_secrets"))
    passphrase = payload.get("passphrase")
    try:
        doc = build_export(db, include_secrets=include_secrets, passphrase=passphrase)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Export impossible : {exc}")
    return _attachment(doc)


@router.post("/import")
def import_config(
    document: dict,
    dry_run: bool = True,
    mode: str = "merge",
    passphrase: str | None = Header(default=None, alias="X-Fossor-Passphrase"),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    """Preview (``dry_run=true``, default) or apply an exported document."""
    if mode not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode invalide : « merge » ou « replace » attendu")
    try:
        validate_document(document)
        diff = compute_diff(db, document)
        if dry_run:
            return {"dry_run": True, "mode": mode, "diff": diff}

        result = apply_import(db, document, mode=mode, passphrase=passphrase)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    summary = (
        f"mode={mode} machines +{result['stats']['machines']['created']}"
        f"/~{result['stats']['machines']['updated']}/-{result['stats']['machines']['deleted']},"
        f" devices +{result['stats']['devices']['created']}"
        f"/~{result['stats']['devices']['updated']}/-{result['stats']['devices']['deleted']},"
        f" backup={result['backup']}"
    )
    db.add(AuditLog(
        actor=ActorEnum.fred,
        action="config-import",
        object_ref="config",
        result="ok",
        output=summary[:500],
    ))
    db.commit()
    return {"dry_run": False, **result, "diff": diff}
