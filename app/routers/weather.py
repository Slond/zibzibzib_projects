"""Weather router - Yandex Smart Home device monitoring"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import async_session, Device, Measurement, WeatherDisplaySetting, IQAirSensor
from app.auth import get_current_user, has_service_access
from app.services.iqair_client import extract_station_name_from_url
from app.services.scheduler import poll_iqair_sensors

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/weather")
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


async def require_weather_access(request: Request):
    """Check user has access to weather module"""
    user = await get_current_user(request)
    if not user:
        return None
    if not user.is_admin and not await has_service_access(user.id, "weather"):
        return None
    return user


@router.get("")
@router.get("/")
async def weather_index(request: Request):
    """Weather dashboard with device data"""
    user = await require_weather_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "weather/index.html",
        {"request": request, "user": user}
    )


@router.get("/settings")
async def weather_settings(request: Request):
    """Weather display settings page"""
    user = await require_weather_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse(url="/weather", status_code=302)

    async with async_session() as session:
        devices_result = await session.execute(select(Device).order_by(Device.name))
        devices = devices_result.scalars().all()

        props_result = await session.execute(
            select(
                Measurement.device_id,
                Measurement.property_instance,
                Measurement.unit,
            )
            .distinct()
            .order_by(Measurement.device_id, Measurement.property_instance)
        )
        properties = props_result.all()

        settings_result = await session.execute(select(WeatherDisplaySetting))
        settings = {
            f"{s.device_id}|{s.property_instance}": s
            for s in settings_result.scalars().all()
        }
        
        # IQAir sensors
        iqair_result = await session.execute(select(IQAirSensor).order_by(IQAirSensor.created_at.desc()))
        iqair_sensors = [
            {
                "id": s.id,
                "url": s.url,
                "name": s.name,
                "is_active": s.is_active,
                "track_pm25": s.track_pm25,
                "track_pm10": s.track_pm10,
                "last_poll": s.last_poll,
            }
            for s in iqair_result.scalars().all()
        ]

    devices_map = {d.device_id: d for d in devices}

    grouped = {}
    for prop in properties:
        device = devices_map.get(prop.device_id)
        if not device:
            continue
        if device.device_id not in grouped:
            grouped[device.device_id] = {
                "device": device,
                "properties": []
            }
        key = f"{prop.device_id}|{prop.property_instance}"
        setting = settings.get(key)
        grouped[device.device_id]["properties"].append({
            "property": prop.property_instance,
            "unit": prop.unit,
            "show_on_dashboard": setting.show_on_dashboard if setting else False,
            "display_order": setting.display_order if setting else 0,
        })

    return templates.TemplateResponse(
        "weather/settings.html",
        {
            "request": request,
            "user": user,
            "devices_grouped": grouped,
            "iqair_sensors": iqair_sensors,
        }
    )


@router.post("/settings/toggle")
async def toggle_display(
    request: Request,
    device_id: str = Form(...),
    property_instance: str = Form(...),
    show: bool = Form(False),
):
    """Toggle whether a property is shown on dashboard"""
    user = await require_weather_access(request)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403)

    async with async_session() as session:
        stmt = sqlite_insert(WeatherDisplaySetting).values(
            device_id=device_id,
            property_instance=property_instance,
            show_on_dashboard=show,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["device_id", "property_instance"],
            set_={"show_on_dashboard": show},
        )
        await session.execute(stmt)
        await session.commit()

    return {"status": "ok"}


# ============ IQAir Management ============

@router.post("/iqair/add")
async def add_iqair_sensor(
    request: Request,
    url: str = Form(...),
    name: str = Form(None),
    track_pm25: bool = Form(True),
    track_pm10: bool = Form(True),
):
    """Add a new IQAir sensor"""
    user = await require_weather_access(request)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403)

    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    
    if not name:
        name = extract_station_name_from_url(url)
    
    try:
        async with async_session() as session:
            sensor = IQAirSensor(
                url=url,
                name=name,
                track_pm25=track_pm25,
                track_pm10=track_pm10,
            )
            session.add(sensor)
            await session.commit()
            logger.info(f"Added IQAir sensor: {name}")
    except Exception as e:
        logger.error(f"Failed to add IQAir sensor: {e}")

    return RedirectResponse(url="/weather/settings", status_code=302)


@router.post("/iqair/{sensor_id}/delete")
async def delete_iqair_sensor(request: Request, sensor_id: int):
    """Delete an IQAir sensor"""
    user = await require_weather_access(request)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403)

    async with async_session() as session:
        result = await session.execute(select(IQAirSensor).where(IQAirSensor.id == sensor_id))
        sensor = result.scalar_one_or_none()
        if sensor:
            await session.delete(sensor)
            await session.commit()
            logger.info(f"Deleted IQAir sensor: {sensor.name}")

    return RedirectResponse(url="/weather/settings", status_code=302)


@router.post("/iqair/{sensor_id}/toggle")
async def toggle_iqair_sensor(request: Request, sensor_id: int):
    """Toggle IQAir sensor active state"""
    user = await require_weather_access(request)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403)

    async with async_session() as session:
        result = await session.execute(select(IQAirSensor).where(IQAirSensor.id == sensor_id))
        sensor = result.scalar_one_or_none()
        if sensor:
            sensor.is_active = not sensor.is_active
            await session.commit()
            logger.info(f"Toggled IQAir sensor {sensor.name}: active={sensor.is_active}")

    return RedirectResponse(url="/weather/settings", status_code=302)


@router.post("/iqair/poll")
async def poll_iqair_now(request: Request):
    """Manually trigger IQAir polling"""
    user = await require_weather_access(request)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403)

    await poll_iqair_sensors()
    return RedirectResponse(url="/weather/settings", status_code=302)


# ============ API Endpoints ============

@router.get("/api/devices")
async def get_devices(request: Request):
    user = await require_weather_access(request)
    if not user:
        raise HTTPException(status_code=401)

    async with async_session() as session:
        result = await session.execute(select(Device).order_by(Device.name))
        devices = result.scalars().all()
        return [
            {
                "device_id": d.device_id,
                "name": d.name,
                "type": d.device_type,
                "room": d.room,
            }
            for d in devices
        ]


@router.get("/api/properties")
async def get_available_properties(request: Request):
    user = await require_weather_access(request)
    if not user:
        raise HTTPException(status_code=401)

    async with async_session() as session:
        result = await session.execute(
            select(
                Measurement.device_id,
                Measurement.property_instance,
                Measurement.unit,
            )
            .distinct()
            .order_by(Measurement.device_id, Measurement.property_instance)
        )
        rows = result.all()

        devices_result = await session.execute(select(Device))
        devices = {d.device_id: d.name for d in devices_result.scalars().all()}

        return [
            {
                "device_id": row.device_id,
                "device_name": devices.get(row.device_id, "Unknown"),
                "property": row.property_instance,
                "unit": row.unit,
            }
            for row in rows
        ]


@router.get("/api/measurements")
async def get_measurements(
    request: Request,
    device_id: str = Query(..., description="Device ID"),
    property_instance: str = Query(..., description="Property instance"),
    hours: int = Query(24, description="Hours of history"),
):
    user = await require_weather_access(request)
    if not user:
        raise HTTPException(status_code=401)

    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

    async with async_session() as session:
        result = await session.execute(
            select(Measurement)
            .where(
                Measurement.device_id == device_id,
                Measurement.property_instance == property_instance,
                Measurement.timestamp >= since,
            )
            .order_by(Measurement.timestamp)
        )
        measurements = result.scalars().all()

        return [
            {
                "timestamp": m.timestamp.isoformat() + "Z",
                "value": m.value,
                "unit": m.unit,
            }
            for m in measurements
        ]


@router.get("/api/latest")
async def get_latest_measurements(request: Request, visible_only: bool = Query(False)):
    """Get latest measurements. If visible_only=True, only return those marked for dashboard."""
    user = await require_weather_access(request)
    if not user:
        raise HTTPException(status_code=401)

    async with async_session() as session:
        subq = (
            select(
                Measurement.device_id,
                Measurement.property_instance,
                func.max(Measurement.timestamp).label("max_ts"),
            )
            .group_by(Measurement.device_id, Measurement.property_instance)
            .subquery()
        )

        result = await session.execute(
            select(Measurement)
            .join(
                subq,
                (Measurement.device_id == subq.c.device_id)
                & (Measurement.property_instance == subq.c.property_instance)
                & (Measurement.timestamp == subq.c.max_ts),
            )
        )
        measurements = result.scalars().all()

        devices_result = await session.execute(select(Device))
        devices = {d.device_id: d.name for d in devices_result.scalars().all()}

        if visible_only:
            settings_result = await session.execute(
                select(WeatherDisplaySetting).where(WeatherDisplaySetting.show_on_dashboard == True)
            )
            visible = {
                f"{s.device_id}|{s.property_instance}": s.display_order
                for s in settings_result.scalars().all()
            }

            filtered = []
            for m in measurements:
                key = f"{m.device_id}|{m.property_instance}"
                if key in visible:
                    filtered.append((m, visible[key]))

            filtered.sort(key=lambda x: x[1])
            measurements = [m for m, _ in filtered]

        return [
            {
                "device_id": m.device_id,
                "device_name": devices.get(m.device_id, "Unknown"),
                "property": m.property_instance,
                "value": m.value,
                "unit": m.unit,
                "timestamp": m.timestamp.isoformat() + "Z",
            }
            for m in measurements
        ]
