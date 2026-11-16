"""Notifications API (§4.14).

Secrets are write-only: the API only ever returns ``has_*`` flags. Sending a
test is the only action that talks to a third party.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import notifications
from app.auth import require_login
from app.database import get_db

router = APIRouter()


class NotificationUpdate(BaseModel):
    channels: dict | None = None
    matrix: dict | None = None
    quiet_hours: dict | None = None
    clear_secrets: list[str] | None = None


@router.get("/api/notifications")
def get_notifications(db: Session = Depends(get_db), _user=Depends(require_login)):
    payload = notifications.public_config(db)
    payload["log"] = notifications.list_log(db, limit=50)
    return payload


@router.put("/api/notifications")
def put_notifications(
    body: NotificationUpdate,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    payload = body.model_dump(exclude_none=True)
    try:
        notifications.save_config(db, payload)
        notifications.update_secrets_from_payload(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return notifications.public_config(db)


@router.post("/api/notifications/test/{channel}")
async def test_notification(
    channel: str,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    if channel not in notifications.CHANNELS:
        raise HTTPException(status_code=404, detail="canal inconnu")
    try:
        return await notifications.send_test(db, channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/notifications/log")
def notification_log(
    limit: int = 100,
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    return {"entries": notifications.list_log(db, limit=limit)}
