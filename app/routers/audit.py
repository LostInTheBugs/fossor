from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_login
from app.models import AuditLog, ActorEnum

router = APIRouter(tags=["audit"])


@router.get("/api/audit-log")
def list_audit_log(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
):
    logs = db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(200).all()
    return [
        {
            "id": l.id,
            "ts": l.ts,
            "actor": getattr(l.actor, "value", l.actor),
            "action": l.action,
            "machine_id": l.machine_id,
            "object_ref": l.object_ref,
            "result": l.result,
            "output": l.output,
        }
        for l in logs
    ]
