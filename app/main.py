from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from app.database import init_db
from app.auth import router as auth_router
from app.routers.machines import router as machines_router
from app.routers.audit import router as audit_router
from app.routers.devices import router as devices_router
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
app.include_router(ui_router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    # If already logged in, redirect to machines page
    from app.auth import get_current_user
    if get_current_user(request):
        return RedirectResponse(url="/machines", status_code=303)
    return HTMLResponse((STATIC_DIR / "login.html").read_text())


@app.get("/api/version")
def version():
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    return {"version": version_file.read_text().strip()}


@app.get("/health")
def health():
    return {"status": "ok"}
