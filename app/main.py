"""
Unified FastAPI Application
Combines Dashboard, Finance Tracker, and Weather Monitoring
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
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
    SESSION_COOKIE,
)
from app.services.scheduler import poll_yandex_devices, process_recurring_transactions, poll_iqair_sensors
from app.routers import dashboard, finance, weather

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")

    await ensure_admin_exists()
    logger.info("Admin user ensured")

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
    
    scheduler.start()
    logger.info("Scheduler started")

    yield

    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


app = FastAPI(title="Main Project", lifespan=lifespan)

# Mount static files for PWA
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(finance.router)
app.include_router(weather.router)


# ============ Shared Auth Routes ============

@app.get("/login")
async def login_page(request: Request):
    user = await get_current_user(request)
    if user:
        if user.must_change_password:
            return RedirectResponse(url="/change-password", status_code=302)
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = await authenticate_user(email, password)
    if not user:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Неверный email или пароль"}
        )

    token = create_session_token(user.id)
    redirect_url = "/change-password" if user.must_change_password else "/"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        max_age=60 * 60 * 24 * 7,
        samesite="lax",
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
        "change_password.html", {"request": request, "user": user}
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
            "change_password.html",
            {"request": request, "user": user, "error": "Пароли не совпадают"},
        )

    if len(new_password) < 6:
        return templates.TemplateResponse(
            "change_password.html",
            {"request": request, "user": user, "error": "Пароль должен быть минимум 6 символов"},
        )

    await update_user_password(user.id, new_password)
    return RedirectResponse(url="/", status_code=302)
