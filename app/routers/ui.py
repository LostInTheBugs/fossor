from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from pathlib import Path

from app.auth import require_login

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@router.get("/", response_class=HTMLResponse)
@router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request):
    user = require_login(request)
    html = (STATIC_DIR / "machines.html").read_text()
    return HTMLResponse(html)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    user = require_login(request)
    html = (STATIC_DIR / "audit.html").read_text()
    return HTMLResponse(html)
