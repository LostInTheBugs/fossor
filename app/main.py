from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from app.database import init_db
from app.auth import router as auth_router
from app.routers.security import router as security_router
from app.routers.machines import router as machines_router
from app.routers.hiveos import router as hiveos_router, router_settings as hiveos_settings_router
from app.routers.audit import router as audit_router
from app.routers.devices import router as devices_router
from app.routers.config import router as config_router
from app.routers.ai import router as ai_router
from app.routers.hermes import router as hermes_router
from app.routers.rules import router as rules_router
from app.routers.update import router as update_router
from app.routers.metrics import router as metrics_router
from app.routers.history import router as history_router
from app.routers.coins import router as coins_router
from app.routers.mined import router as mined_router
from app.routers.energy import router as energy_router
from app.routers.power import router as power_router
from app.routers.watchdog import router as watchdog_router
from app.routers.ssh import router as ssh_router
from app.routers.notifications import router as notifications_router
from app.routers.miners import router as miners_router
from app.routers.psu import router as psu_router
from app.routers.boost import router as boost_router
from app.routers.campaigns import router as campaigns_router
from app.routers.ui import router as ui_router
from app.routers.health import router as health_router
from app.routers.asic import router as asic_router
from app.routers.rentability import router as rentability_router
from app.routers.vms import router as vms_router
from app.routers.pve import router as pve_router
from app.routers.vault import admin_router as vault_router, hermes_router as vault_hermes_router
from pathlib import Path

app = FastAPI(title="Fossor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def hermes_tick_journal(request: Request, call_next):
    """Journal du tick : méthode, chemin et code des appels reçus de Hermes."""
    response = await call_next(request)
    if request.url.path.startswith("/api/hermes/"):
        from app import veille

        veille.note_request(request.method, request.url.path, response.status_code)
    return response


# Include routers
app.include_router(auth_router)
app.include_router(security_router)
app.include_router(machines_router)
app.include_router(hiveos_router)
app.include_router(hiveos_settings_router)
app.include_router(audit_router)
app.include_router(devices_router)
app.include_router(config_router)
app.include_router(ai_router)
app.include_router(hermes_router)
app.include_router(rules_router)
app.include_router(update_router)
app.include_router(metrics_router)
app.include_router(history_router)
app.include_router(coins_router)
app.include_router(mined_router)
app.include_router(energy_router)
app.include_router(power_router)
app.include_router(watchdog_router)
app.include_router(ssh_router)
app.include_router(notifications_router)
app.include_router(miners_router)
app.include_router(psu_router)
app.include_router(campaigns_router)
app.include_router(boost_router)
app.include_router(ui_router)
app.include_router(health_router)
app.include_router(pve_router)
app.include_router(asic_router)
app.include_router(rentability_router)
app.include_router(vms_router)
app.include_router(vault_router)
app.include_router(vault_hermes_router)

from app.routers.parite import router as parite_router

app.include_router(parite_router)


@app.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    """Écran « Analyse des tokens » (§4.8/§4.9)."""
    from app.auth import get_current_user

    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse((STATIC_DIR / "analysis.html").read_text())


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    """Écran « Comment ça marche » — repris de l'onglet de mining.html."""
    from app.auth import get_current_user

    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse((STATIC_DIR / "help.html").read_text())


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    """Écran « Historique » (§10.7) — séries et export comptable."""
    from app.auth import get_current_user

    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse((STATIC_DIR / "history.html").read_text())


@app.get("/mined", response_class=HTMLResponse)
def mined_page(request: Request):
    """Écran « Tokens minés » (§6 écran 3)."""
    from app.auth import get_current_user

    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse((STATIC_DIR / "mined.html").read_text())

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/static/nav.js")
def nav_script():
    """Single source of the navigation bar, shared by every page (§2f)."""
    return FileResponse(
        STATIC_DIR / "nav.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/static/chart.js")
def chart_script():
    """Mini bibliothèque de graphiques (§10.7) — servie localement, sans CDN."""
    return FileResponse(
        STATIC_DIR / "chart.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.on_event("startup")
async def startup():
    init_db()
    # §4.8 : un tick resté « running » quand l'app s'est arrêtée n'a plus de
    # thread pour le terminer — il est clos en « interrupted » et le verrou
    # est rendu, sinon tout lancement suivant répondrait 409 pour toujours.
    try:
        from app import veille as _veille

        _veille.close_interrupted()
    except Exception:  # pragma: no cover - startup guard
        import logging
        logging.getLogger("fossor.veille").warning(
            "tick interrompu non clos au démarrage", exc_info=True
        )
    # §2026.11.003-c6 : un catalogue vide reçoit les coins de base.  Sans cela
    # la correspondance pool → coin ne trouvait jamais rien (prod : 0 ligne).
    try:
        from app.coins import seed_builtin_coins
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            seed_builtin_coins(db)
        finally:
            db.close()
    except Exception:  # pragma: no cover - startup guard
        import logging
        logging.getLogger("fossor.coins").warning(
            "coins de base non créés", exc_info=True
        )
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
    # §4.16 : relevé périodique borné des paiements de pool et blocs solo.
    from app import payments
    payments.start()
    # §4.8/§4.12 : la cadence de la veille déclenche un tick Hermes (après le
    # premier tick manuel).  Aucun appel au démarrage.
    from app import veille
    veille.start_scheduler()
    # §4.14: notification queue and event watchers.
    from app import notifications
    notifications.start()
    # §4.7 : planificateur des campagnes (même processus, une seule boucle).
    from app import campaign_runner
    campaign_runner.start()
    # §10.3 : planificateur du mode boost (même processus).
    from app import boost_runner
    boost_runner.start()
    # Préprod : niveau de journalisation choisi en Configuration → Journal,
    # puis une sauvegarde au démarrage et toutes les six heures.
    from app import observability
    from app.database import SessionLocal as _SessionLocal

    _db = _SessionLocal()
    try:
        observability.apply_logging(observability.get_log_level(_db))
    finally:
        _db.close()
    observability.start_backup()


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


# §10.4 — benchmark a la demande de la limite de puissance.
from app.routers import benchmark as benchmark_router_module  # noqa: E402

app.include_router(benchmark_router_module.router)
