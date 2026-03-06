import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import async_session, Device, Measurement
from app.services.yandex_client import YandexSmartHomeClient

logger = logging.getLogger(__name__)


async def poll_yandex_devices():
    """Poll Yandex Smart Home devices and save measurements"""
    logger.info("Starting devices poll...")

    client = YandexSmartHomeClient()

    try:
        devices = await client.get_all_devices_with_properties()
        logger.info(f"Got {len(devices)} devices")

        async with async_session() as session:
            for device_data in devices:
                device_id = device_data.get("id")
                device_name = device_data.get("name", "Unknown")
                device_type = device_data.get("type", "")
                room = device_data.get("room", "")

                stmt = sqlite_insert(Device).values(
                    device_id=device_id,
                    name=device_name,
                    device_type=device_type,
                    room=room,
                    updated_at=datetime.now(tz=timezone.utc),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["device_id"],
                    set_={
                        "name": device_name,
                        "device_type": device_type,
                        "room": room,
                        "updated_at": datetime.now(tz=timezone.utc),
                    },
                )
                await session.execute(stmt)

                properties = device_data.get("properties", [])
                for prop in properties:
                    state = prop.get("state")
                    if not state:
                        continue

                    instance = state.get("instance", "unknown")
                    value = state.get("value")

                    if value is None:
                        continue

                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Skipping non-numeric value for {device_name}/{instance}: {value}"
                        )
                        continue

                    unit = prop.get("parameters", {}).get("unit", "")

                    measurement = Measurement(
                        device_id=device_id,
                        property_instance=instance,
                        value=value,
                        unit=unit,
                        timestamp=datetime.now(tz=timezone.utc),
                    )
                    session.add(measurement)
                    logger.debug(f"Saved: {device_name}/{instance} = {value} {unit}")

            await session.commit()
            logger.info("Poll completed successfully")

    except Exception as e:
        logger.error(f"Poll failed: {e}", exc_info=True)
