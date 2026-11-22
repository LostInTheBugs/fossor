"""API Historique §10.7 — rétention, séries, export comptable."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app import history
from app.database import get_db
from app.dependencies import require_login
from app.models import Device, Machine, Wallet

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("/retention")
def get_retention(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
) -> dict[str, Any]:
    """Rétention en vigueur : brut, horaire, journalier (0 = illimité)."""
    return history.get_retention(db)


@router.put("/retention")
def put_retention(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    _user=Depends(require_login),
) -> dict[str, Any]:
    """Fixe la rétention ; les valeurs hors bornes sont ramenées dans la plage."""
    return history.set_retention(db, payload)


@router.get("/targets")
def targets(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
) -> dict[str, Any]:
    """Cibles des graphiques : machines, devices et coins détenus."""
    machines = db.query(Machine).order_by(Machine.name.asc()).all()
    devices = (
        db.query(Device, Machine.name)
        .join(Machine, Machine.id == Device.machine_id)
        .filter(Device.present == True)  # noqa: E712
        .order_by(Machine.name.asc(), Device.kind.asc(), Device.index.asc())
        .all()
    )
    tickers = sorted({row.coin_ticker.upper() for row in db.query(Wallet).all()})
    return {
        "machines": [
            {"uuid": machine.uuid, "name": machine.name}
            for machine in machines
            if machine.uuid
        ],
        "devices": [
            {
                "uuid": device.uuid,
                "label": f"{device.model or device.kind} — {machine_name}",
                "machine": machine_name,
                "kind": device.kind,
            }
            for device, machine_name in devices
            if device.uuid
        ],
        "coins": tickers,
    }


@router.get("/series")
def series(
    scope: str = Query(..., pattern="^(machine|device|coin)$"),
    target: str = Query(..., min_length=1, max_length=64),
    range: str = Query("24h", pattern="^(24h|7d|30d|1y|custom)$"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
) -> dict[str, Any]:
    """Points d'une série ; une période sans données renvoie ``count: 0``."""
    try:
        return history.series(db, scope, target, range, start, end)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/maintenance")
def maintenance(
    db: Session = Depends(get_db),
    _user=Depends(require_login),
) -> dict[str, Any]:
    """Lance agrégation puis purge — même code que la tâche planifiée."""
    return history.run_maintenance(db)


@router.get("/export.csv")
def export_csv(
    start: str | None = Query(None, description="début ISO (inclus)"),
    end: str | None = Query(None, description="fin ISO (exclu)"),
    coin: str | None = Query(None, description="ticker ; vide = tous"),
    db: Session = Depends(get_db),
    _user=Depends(require_login),
) -> Response:
    """CSV des gains pour la comptabilité (§10.7).

    Séparateur ``;``, décimales françaises, UTF-8 BOM, note fiscale. Aucun
    wallet complet, aucune IP : la référence est une tx de pool si connue.
    """
    if start:
        try:
            datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date de début invalide") from exc
    if end:
        try:
            datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date de fin invalide") from exc
    payload = history.csv_export(db, start, end, coin)
    filename = "fossor-gains-" + datetime.now(timezone.utc).strftime("%Y%m%d") + ".csv"
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Fossor-Note": "Donnees a titre informatif, pas de conseil fiscal",
        },
    )
