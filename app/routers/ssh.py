"""Fossor SSH identity endpoints (§4.1 bis)."""

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.dependencies import require_login

router = APIRouter(prefix="/api/ssh", tags=["ssh"])


@router.get("/public-key", response_class=PlainTextResponse)
def fossor_public_key(_user=Depends(require_login)):
    """Public key of Fossor, OpenSSH text with the ``fossor@<instance>`` comment."""
    from app import fossor_keys

    return fossor_keys.public_key_text()
