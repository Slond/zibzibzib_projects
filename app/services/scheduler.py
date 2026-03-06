import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import (
    async_session,
    Device,
    Measurement,
    RecurringTransaction,
    Transaction,
    FinanceEvent,
    Category,
)
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


async def process_recurring_transactions():
    """Process due recurring transactions and create actual transactions"""
    logger.info("Processing recurring transactions...")
    
    now = datetime.now(tz=timezone.utc)
    
    try:
        async with async_session() as session:
            # Find all due recurring transactions
            result = await session.execute(
                select(RecurringTransaction)
                .where(
                    RecurringTransaction.is_active == True,
                    RecurringTransaction.next_run <= now,
                )
            )
            due_recurring = result.scalars().all()
            
            logger.info(f"Found {len(due_recurring)} due recurring transactions")
            
            for rec in due_recurring:
                # Get event to find account_id
                event_result = await session.execute(
                    select(FinanceEvent).where(FinanceEvent.id == rec.event_id)
                )
                event = event_result.scalar_one_or_none()
                
                if not event:
                    logger.warning(f"Event {rec.event_id} not found for recurring {rec.id}")
                    continue
                
                # Get category name if set
                category_name = None
                if rec.category_id:
                    cat_result = await session.execute(
                        select(Category).where(Category.id == rec.category_id)
                    )
                    category = cat_result.scalar_one_or_none()
                    if category:
                        category_name = category.name
                
                # Create the transaction
                transaction = Transaction(
                    account_id=event.account_id,
                    event_id=rec.event_id,
                    amount=rec.amount,
                    currency=rec.currency,
                    category=category_name,
                    description=f"🔄 {rec.description}" if rec.description else "🔄 Повторяющаяся",
                    timestamp=now,
                    source="recurring",
                )
                session.add(transaction)
                
                # Update next_run
                rec.last_run = now
                if rec.frequency == "daily":
                    rec.next_run = now + timedelta(days=1)
                elif rec.frequency == "weekly":
                    rec.next_run = now + timedelta(weeks=1)
                elif rec.frequency == "monthly":
                    rec.next_run = now + timedelta(days=30)
                else:  # every_n_days
                    rec.next_run = now + timedelta(days=rec.frequency_value)
                
                logger.info(f"Created transaction from recurring {rec.id}: {rec.amount} {rec.currency}")
            
            await session.commit()
            logger.info("Recurring transactions processed successfully")
            
    except Exception as e:
        logger.error(f"Recurring transactions processing failed: {e}", exc_info=True)
