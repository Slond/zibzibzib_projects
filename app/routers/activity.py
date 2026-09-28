"""Activity diary. Each user sees only their own places, devices, and window titles."""
import logging
from datetime import date, datetime
from pathlib import Path

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Form
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, has_service_access
from app.database import User
from app.services.activity import (
    authenticate_device,
    create_device,
    delete_device,
    delete_place,
    device_config,
    latest_fix,
    layered_page,
    list_devices,
    list_places,
    record_location,
    record_screen,
    regenerate_device_token,
    remember_stay,
    rename_device,
    settings_view,
    update_settings,
    upsert_place,
    zone_or_default,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/activity")
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
CLIENT_DIR = Path(__file__).resolve().parents[2] / "clients" / "activity"
CLIENT_FILES = {
    "client.py": "text/x-python; charset=utf-8",
    "requirements.txt": "text/plain; charset=utf-8",
}


def public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    host = str(host).split(",")[0].strip()
    return f"{proto}://{host}"


def parse_day(value: str | None, tz_name: str) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(zone_or_default(tz_name)).date()


async def gate(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.must_change_password:
        return RedirectResponse(url="/change-password", status_code=302)
    if not await has_service_access(user.id, "activity"):
        return RedirectResponse(url="/", status_code=302)
    return user


def page_context(request: Request, user: User, page: str, **extra):
    context = {
        "request": request,
        "user": user,
        "page": page,
        "base_url": public_base(request),
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
    }
    context.update(extra)
    return context


async def require_device(token: str):
    device = await authenticate_device(token)
    if not device or not await has_service_access(device.user_id, "activity"):
        raise HTTPException(status_code=404, detail="unknown device")
    return device


def form_text(form, name: str) -> str:
    value = form.get(name)
    if value is None or hasattr(value, "read"):
        return ""
    return str(value)


async def screen_payload(request: Request):
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="expected object")
        return data
    form = await request.form()
    return {
        "app_name": form_text(form, "app_name"),
        "window_title": form_text(form, "window_title"),
        "wifi_ssid": form_text(form, "wifi_ssid"),
        "captured_at": form_text(form, "captured_at"),
    }


def lowered(data: dict) -> dict:
    return {str(key).lower(): value for key, value in data.items()}


def pick_number(data: dict, *keys: str) -> float | None:
    for key in keys:
        if key not in data or data[key] in (None, ""):
            continue
        return float(str(data[key]).replace(",", "."))
    return None


def client_bundle(name: str) -> Path:
    media = CLIENT_FILES.get(name)
    if not media:
        raise HTTPException(status_code=404, detail="not found")
    path = (CLIENT_DIR / name).resolve()
    if path.parent != CLIENT_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return path


async def require_desktop_device(token: str):
    device = await require_device(token)
    if device.platform == "iphone":
        raise HTTPException(status_code=404, detail="unknown device")
    return device


@router.get("/api/client/{token}/requirements.txt")
async def download_client_requirements(token: str):
    await require_desktop_device(token)
    return FileResponse(
        client_bundle("requirements.txt"),
        media_type=CLIENT_FILES["requirements.txt"],
        filename="requirements.txt",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/client/{token}")
async def download_client(token: str):
    await require_desktop_device(token)
    return FileResponse(
        client_bundle("client.py"),
        media_type=CLIENT_FILES["client.py"],
        filename="client.py",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/config/{token}")
async def activity_config(token: str):
    device = await require_device(token)
    return await device_config(device)


@router.post("/api/ingest/{token}")
async def activity_ingest(token: str, request: Request):
    device = await require_device(token)
    payload = await screen_payload(request)
    result = await record_screen(
        device,
        app_name=payload.get("app_name"),
        window_title=payload.get("window_title"),
        wifi_ssid=payload.get("wifi_ssid"),
        captured_at=payload.get("captured_at"),
    )
    if result.status == "error":
        raise HTTPException(status_code=400, detail=result.reason or "rejected")
    return result.as_dict()


@router.post("/api/location/{token}")
async def activity_location(token: str, request: Request):
    device = await require_device(token)
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="expected object")
        data = lowered(raw)
    else:
        form = await request.form()
        data = lowered({key: form_text(form, key) for key in form.keys()})
    try:
        lat = pick_number(data, "latitude", "lat")
        lon = pick_number(data, "longitude", "lon", "lng")
        accuracy = pick_number(data, "accuracy", "horizontalaccuracy", "accuracy_m")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad coordinates")
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="latitude and longitude are required")
    captured = data.get("captured_at") or data.get("timestamp")
    result = await record_location(
        device,
        lat=lat,
        lon=lon,
        accuracy=accuracy,
        captured_at=str(captured) if captured else None,
    )
    if result.status == "error":
        raise HTTPException(status_code=400, detail=result.reason or "rejected")
    return result.as_dict()


@router.get("")
@router.get("/")
async def activity_index(request: Request, date: str | None = None, stay: str | None = None):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    prefs = await settings_view(user.id)
    day = parse_day(date, prefs["timezone"])
    view = await layered_page(user.id, day, stay)
    return templates.TemplateResponse(
        request=request,
        name="activity/index.html",
        context=page_context(request, user, "day", **view),
    )


@router.post("/remember")
async def activity_remember(
    request: Request,
    date: str = Form(""),
    stay_start: str = Form(""),
    name: str = Form(""),
):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    prefs = await settings_view(user.id)
    day = parse_day(date or None, prefs["timezone"])
    error = await remember_stay(user.id, day, stay_start, name)
    params = {"date": day.isoformat()}
    if stay_start:
        params["stay"] = stay_start
    if error:
        params["error"] = error
    else:
        params["success"] = "Место запомнено"
    return RedirectResponse("/activity?" + urlencode(params), status_code=303)


@router.get("/places")
async def activity_places(request: Request, edit: int | None = None, lat: str | None = None, lon: str | None = None):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    places = await list_places(user.id)
    editing = next((place for place in places if place["id"] == edit), None)
    form = editing
    if form is None and (lat or lon):
        form = {
            "id": "",
            "name": "",
            "note": "",
            "wifi_ssid": "",
            "latitude": lat or "",
            "longitude": lon or "",
            "radius_m": 10,
        }
    return templates.TemplateResponse(
        request=request,
        name="activity/places.html",
        context=page_context(
            request,
            user,
            "places",
            places=places,
            form=form,
            last_fix=await latest_fix(user.id),
        ),
    )


@router.post("/places")
async def activity_save_place(
    request: Request,
    place_id: str = Form(""),
    name: str = Form(""),
    note: str = Form(""),
    wifi_ssid: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    radius_m: str = Form(""),
):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    parsed_id = int(place_id) if place_id.strip().isdigit() else None
    error = await upsert_place(
        user.id,
        parsed_id,
        name,
        note,
        wifi_ssid,
        latitude,
        longitude,
        radius_m,
    )
    if error:
        places = await list_places(user.id)
        return templates.TemplateResponse(
            request=request,
            name="activity/places.html",
            context=page_context(
                request,
                user,
                "places",
                places=places,
                form={
                    "id": place_id,
                    "name": name,
                    "note": note,
                    "wifi_ssid": wifi_ssid,
                    "latitude": latitude,
                    "longitude": longitude,
                    "radius_m": radius_m,
                },
                last_fix=await latest_fix(user.id),
                error=error,
            ),
        )
    return RedirectResponse(url="/activity/places?success=Место+сохранено", status_code=302)


@router.post("/places/{place_id}/delete")
async def activity_delete_place(request: Request, place_id: int):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    await delete_place(user.id, place_id)
    return RedirectResponse(url="/activity/places?success=Место+удалено", status_code=302)


@router.get("/devices")
async def activity_devices(request: Request):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    prefs = await settings_view(user.id)
    devices = await list_devices(user.id, prefs["timezone"])
    return templates.TemplateResponse(
        request=request,
        name="activity/devices.html",
        context=page_context(request, user, "devices", devices=devices),
    )


@router.post("/devices")
async def activity_create_device(
    request: Request,
    name: str = Form(""),
    platform: str = Form(""),
):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    error = await create_device(user.id, name, platform)
    if error:
        prefs = await settings_view(user.id)
        devices = await list_devices(user.id, prefs["timezone"])
        return templates.TemplateResponse(
            request=request,
            name="activity/devices.html",
            context=page_context(request, user, "devices", devices=devices, error=error),
        )
    return RedirectResponse(url="/activity/devices?success=Устройство+добавлено", status_code=302)


@router.post("/devices/{device_id}/rename")
async def activity_rename_device(request: Request, device_id: int, name: str = Form("")):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    await rename_device(user.id, device_id, name)
    return RedirectResponse(url="/activity/devices?success=Название+обновлено", status_code=302)


@router.post("/devices/{device_id}/token")
async def activity_new_token(request: Request, device_id: int):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    await regenerate_device_token(user.id, device_id)
    return RedirectResponse(url="/activity/devices?success=Токен+обновлён", status_code=302)


@router.post("/devices/{device_id}/delete")
async def activity_delete_device(request: Request, device_id: int):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    await delete_device(user.id, device_id)
    return RedirectResponse(url="/activity/devices?success=Устройство+удалено", status_code=302)


@router.get("/settings")
async def activity_settings(request: Request):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    prefs = await settings_view(user.id)
    return templates.TemplateResponse(
        request=request,
        name="activity/settings.html",
        context=page_context(request, user, "settings", prefs=prefs),
    )


@router.post("/settings")
async def activity_save_settings(
    request: Request,
    timezone_name: str = Form("Asia/Almaty"),
    sample_minutes: str = Form("5"),
    idle_minutes: str = Form("3"),
    skip_apps: str = Form(""),
):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    error = await update_settings(
        user.id,
        timezone_name,
        sample_minutes,
        idle_minutes,
        skip_apps,
    )
    if error:
        prefs = await settings_view(user.id)
        return templates.TemplateResponse(
            request=request,
            name="activity/settings.html",
            context=page_context(request, user, "settings", prefs=prefs, error=error),
        )
    return RedirectResponse(url="/activity/settings?success=Настройки+сохранены", status_code=302)
