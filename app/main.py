from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from app.database import init_db
from app.auth import router as auth_router
from app.routers.machines import router as machines_router
from app.routers.audit import router as audit_router
from app.routers.devices import router as devices_router
from app.routers.config import router as config_router
from app.routers.ai import router as ai_router
from app.routers.hermes import router as hermes_router
from app.routers.update import router as update_router
from app.routers.metrics import router as metrics_router
from app.routers.coins import router as coins_router
from app.routers.mined import router as mined_router
from app.routers.energy import router as energy_router
from app.routers.ssh import router as ssh_router
from app.routers.ui import router as ui_router
from pathlib import Path

app = FastAPI(title="Fossor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router)
app.include_router(machines_router)
app.include_router(audit_router)
app.include_router(devices_router)
app.include_router(config_router)
app.include_router(ai_router)
app.include_router(hermes_router)
app.include_router(update_router)
app.include_router(metrics_router)
app.include_router(coins_router)
app.include_router(mined_router)
app.include_router(energy_router)
app.include_router(ssh_router)
app.include_router(ui_router)


@app.get("/mined", response_class=HTMLResponse)
def mined_page(request: Request):
    """Écran « Tokens minés » (§6 écran 3)."""
    from app.auth import get_current_user

    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse((STATIC_DIR / "mined.html").read_text())

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.on_event("startup")
async def startup():
    init_db()
    # §4.1 bis: Fossor's own SSH identity exists from the very first start.
    # Best-effort: a missing master key must not take the whole app down.
    try:
        from app import fossor_keys
        fossor_keys.ensure_key()
    except Exception as exc:  # pragma: no cover - startup guard
        import logging
        logging.getLogger("fossor.keys").error("Fossor SSH key unavailable: %s", exc)
    # §4.18: best-effort background check, jittered, never blocking startup.
    from app import update_checker
    update_checker.start()
    # §4.18: mise à jour automatique (désactivée par défaut, plage horaire).
    from app import auto_update
    auto_update.start()
    # §8 phase 2a: metrics collection every FOSSOR_COLLECT_INTERVAL seconds.
    from app import collector
    collector.start()


@app.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    # If already logged in, land on the dashboard (§6 écran 1)
    from app.auth import get_current_user
    if get_current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return HTMLResponse((STATIC_DIR / "login.html").read_text())


@app.get("/api/version")
def version():
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    return {"version": version_file.read_text().strip()}


@app.get("/health")
def health():
    return {"status": "ok"}
