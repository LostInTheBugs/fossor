"""Inbound Hermes API (§5.4) — token-protected, used by phase 4.

Only ``/ping`` is exposed for now: it proves the ``Authorization: Bearer``
dependency works end to end without granting any data access yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import require_hermes_token

router = APIRouter(prefix="/api/hermes", tags=["hermes"])


@router.get("/ping")
async def ping(actor: str = Depends(require_hermes_token)):
    """200 with a valid token, 401 otherwise."""
    return {"ok": True, "actor": actor, "service": "fossor"}
