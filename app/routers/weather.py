"""Weather router - Yandex Smart Home device monitoring"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func

from app.database import async_session, Device, Measurement
from app.auth import get_current_user, has_service_access

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
                "timestamp": m.timestamp.isoformat(),
                "value": m.value,
                "unit": m.unit,
            }
            for m in measurements
        ]


@router.get("/api/latest")
async def get_latest_measurements(request: Request):
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

        return [
            {
                "device_id": m.device_id,
                "device_name": devices.get(m.device_id, "Unknown"),
                "property": m.property_instance,
                "value": m.value,
                "unit": m.unit,
                "timestamp": m.timestamp.isoformat(),
            }
            for m in measurements
        ]
