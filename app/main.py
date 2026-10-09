"""
Unified FastAPI Application
Combines Dashboard, Finance Tracker, and Weather Monitoring
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
import secrets

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import YANDEX_TOKEN, POLL_INTERVAL_SECONDS
from app.database import init_db
from app.auth import (
    authenticate_user,
    create_session_token,
    get_current_user,
    update_user_password,
    ensure_admin_exists,
    ensure_activity_service,
    ensure_vpn_service,
    SESSION_COOKIE,
    serializer,
)
from app.services.scheduler import poll_yandex_devices, process_recurring_transactions, poll_iqair_sensors
from app.services.activity import purge_activity_history
from app.services.awg.runtime import poll_traffic
from app.services.currency import CurrencyRateUnavailable
from app.routers import dashboard, finance, weather, activity, vpn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
from app.services.logging_utils import RedactAccessTokens
logging.getLogger("uvicorn.access").addFilter(RedactAccessTokens())
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")

    await ensure_admin_exists()
    logger.info("Admin user ensured")
    await ensure_activity_service()
    logger.info("Activity service ensured")
    await ensure_vpn_service()
    logger.info("VPN service ensured")

    if YANDEX_TOKEN:
        scheduler.add_job(
            poll_yandex_devices,
            "interval",
            seconds=POLL_INTERVAL_SECONDS,
            id="poll_yandex",
            replace_existing=True,
        )
        await poll_yandex_devices()
        logger.info(f"Weather polling every {POLL_INTERVAL_SECONDS}s")
    else:
        logger.warning("YANDEX_TOKEN not set, weather polling disabled")
    
    # Recurring transactions - run every hour
    scheduler.add_job(
        process_recurring_transactions,
        "interval",
        hours=1,
        id="process_recurring",
        replace_existing=True,
    )
    
    # IQAir sensors - poll every 10 minutes
    scheduler.add_job(
        poll_iqair_sensors,
        "interval",
        seconds=600,
        id="poll_iqair",
        replace_existing=True,
    )
    await poll_iqair_sensors()

    scheduler.add_job(
        purge_activity_history,
        "interval",
        hours=6,
        id="activity_purge",
        replace_existing=True,
    )
    scheduler.add_job(
        poll_traffic,
        "interval",
        seconds=30,
        id="awg_traffic",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info("Scheduler started")

    yield

    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


app = FastAPI(title="Main Project", lifespan=lifespan)


@app.exception_handler(CurrencyRateUnavailable)
async def missing_currency_rate(request: Request, exc: CurrencyRateUnavailable):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.middleware("http")
async def session_guard(request: Request, call_next):
    user = await get_current_user(request)
    request.state.current_user = user
    path = request.url.path
    public_api = path.startswith(("/finance/api/webhook/", "/activity/api/"))
    password_route = path in {"/login", "/logout", "/change-password"}
    if user and user.must_change_password and not password_route and not public_api and not path.startswith("/static/") and path != "/sw.js":
        if "/api/" in path:
            return JSONResponse({"detail": "Password change required"}, status_code=403)
        return RedirectResponse("/change-password", status_code=303)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not public_api:
        origin = request.headers.get("origin")
        public_host = request.headers.get("host", request.url.netloc)
        if origin and urlsplit(origin).netloc != public_host:
            return JSONResponse({"detail": "Invalid request origin"}, status_code=403)
        if user and path != "/login":
            token = request.headers.get("x-csrf-token")
            if not token and request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
                await request.body()
                token = (await request.form()).get("csrf_token")
            try:
                data = serializer.loads(token or "", max_age=60 * 60 * 24 * 7)
                valid = isinstance(data, dict) and isinstance(data.get("csrf"), str) and secrets.compare_digest(data["csrf"], user.session_key)
            except Exception:
                valid = False
            if not valid:
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
    response = await call_next(request)
    if user:
        response.headers["Cache-Control"] = "private, no-store"
        response.set_cookie("csrf_token", serializer.dumps({"csrf": user.session_key}), samesite="strict", secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/sw.js")
async def service_worker():
    return FileResponse(Path(__file__).parent / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

# Mount static files for PWA
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(finance.router)
app.include_router(weather.router)
app.include_router(activity.router)
app.include_router(vpn.router)


# ============ Shared Auth Routes ============

@app.get("/login")
async def login_page(request: Request):
    user = await get_current_user(request)
    if user:
        if user.must_change_password:
            return RedirectResponse(url="/change-password", status_code=302)
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={},
    )


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = await authenticate_user(email, password)
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Неверный email или пароль"},
        )

    token = create_session_token(user)
    redirect_url = "/change-password" if user.must_change_password else "/"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        max_age=60 * 60 * 24 * 7,
        samesite="lax",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/change-password")
async def change_password_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="change_password.html",
        context={"user": user},
    )


@app.post("/change-password")
async def change_password(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if new_password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={"user": user, "error": "Пароли не совпадают"},
        )

    if len(new_password) < 6:
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={"user": user, "error": "Пароль должен быть минимум 6 символов"},
        )

    await update_user_password(user.id, new_password)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
