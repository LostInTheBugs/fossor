from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path

from app.auth import require_login, get_current_user

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    require_login(request)
    return HTMLResponse((STATIC_DIR / "dashboard.html").read_text())


@router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request):
    # Redirect unauthenticated users to login
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    user = require_login(request)
    html = (STATIC_DIR / "machines.html").read_text()
    return HTMLResponse(html)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    require_login(request)
    return HTMLResponse((STATIC_DIR / "settings.html").read_text())


@router.get("/vault", response_class=HTMLResponse)
def vault_page(request: Request):
    """Onglet Coffre : même écran que Réglages, panneau Coffre ouvert par la route."""
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    require_login(request)
    return HTMLResponse((STATIC_DIR / "settings.html").read_text())


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    require_login(request)
    html = (STATIC_DIR / "audit.html").read_text()
    return HTMLResponse(html)


@router.get("/followup", response_class=HTMLResponse)
def followup_page(request: Request):
    """Écran « Suivi » (§4.19) : monitoring détaillé, machine par machine."""
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    require_login(request)
    return HTMLResponse((STATIC_DIR / "followup.html").read_text())


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request):
    """Écran « Campagnes » (§4.7)."""
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    require_login(request)
    return HTMLResponse((STATIC_DIR / "campaigns.html").read_text())


@router.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request):
    """Écran « Profils de minage » (§4.5)."""
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    require_login(request)
    return HTMLResponse((STATIC_DIR / "profiles.html").read_text())
