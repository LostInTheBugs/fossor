"""Update status API (§4.18, part 1).

Only the *check* is exposed here: installing an update is a later step.
"""

from fastapi import APIRouter, Depends

from app import update_checker
from app.dependencies import require_login

router = APIRouter(prefix="/api/update", tags=["update"])


@router.get("/status")
async def update_status(_user=Depends(require_login)):
    """Installed version, latest known version, availability and release notes."""
    return update_checker.status()


@router.post("/check")
async def check_now(_user=Depends(require_login)):
    """Force a check (used by the Configuration screen button)."""
    await update_checker.check(force=True)
    return update_checker.status()
