"""Currency conversion service with rate caching
Uses Open Exchange Rates API: https://openexchangerates.org
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import select, and_

from app.database import async_session, ExchangeRate

logger = logging.getLogger(__name__)

# Open Exchange Rates API configuration
OPENEXCHANGERATES_APP_ID = "c6dcf85bc4fc4db19705db0bcaacfd24"
OPENEXCHANGERATES_API_URL = "https://openexchangerates.org/api/latest.json"

# Fallback rates when API is unavailable
FALLBACK_RATES = {
    ("KZT", "USD"): Decimal("0.002174"),
    ("USD", "KZT"): Decimal("460.0"),
    ("EUR", "USD"): Decimal("1.08"),
    ("USD", "EUR"): Decimal("0.926"),
    ("RUB", "USD"): Decimal("0.011"),
    ("USD", "RUB"): Decimal("90.0"),
    ("KZT", "EUR"): Decimal("0.00201"),
    ("EUR", "KZT"): Decimal("497.0"),
    ("KZT", "RUB"): Decimal("0.198"),
    ("RUB", "KZT"): Decimal("5.05"),
}


async def get_cached_rate(from_currency: str, to_currency: str, for_date: date) -> Optional[Decimal]:
    """Get cached exchange rate from database"""
    if from_currency == to_currency:
        return Decimal("1.0")
    
    async with async_session() as session:
        result = await session.execute(
            select(ExchangeRate).where(
                and_(
                    ExchangeRate.from_currency == from_currency.upper(),
                    ExchangeRate.to_currency == to_currency.upper(),
                    ExchangeRate.rate_date == for_date,
                )
            )
        )
        rate_record = result.scalar_one_or_none()
        if rate_record:
            return rate_record.rate
    return None


async def save_rate(from_currency: str, to_currency: str, rate: Decimal, for_date: date):
    """Save exchange rate to database cache"""
    async with async_session() as session:
        # Check if exists
        existing = await session.execute(
            select(ExchangeRate).where(
                and_(
                    ExchangeRate.from_currency == from_currency.upper(),
                    ExchangeRate.to_currency == to_currency.upper(),
                    ExchangeRate.rate_date == for_date,
                )
            )
        )
        record = existing.scalar_one_or_none()
        
        if record:
            record.rate = rate
        else:
            record = ExchangeRate(
                from_currency=from_currency.upper(),
                to_currency=to_currency.upper(),
                rate=rate,
                rate_date=for_date,
            )
            session.add(record)
        
        await session.commit()


async def fetch_rates_from_api() -> Optional[dict[str, Decimal]]:
    """
    Fetch all exchange rates from Open Exchange Rates API.
    Returns rates relative to USD (base currency).
    
    The API returns: { "rates": { "EUR": 0.92, "KZT": 460, ... } }
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                OPENEXCHANGERATES_API_URL,
                params={"app_id": OPENEXCHANGERATES_APP_ID}
            )
            
            if response.status_code == 200:
                data = response.json()
                rates = {}
                for currency, rate in data.get("rates", {}).items():
                    rates[currency] = Decimal(str(rate))
                logger.info(f"Fetched {len(rates)} exchange rates from API")
                return rates
            else:
                logger.warning(f"API returned status {response.status_code}: {response.text}")
                return None
                
    except httpx.TimeoutException:
        logger.warning("Exchange rate API timeout")
        return None
    except Exception as e:
        logger.error(f"Error fetching exchange rates: {e}")
        return None


async def fetch_rate_from_api(from_currency: str, to_currency: str) -> Optional[Decimal]:
    rates = await fetch_rates_from_api()
    if not rates:
        return None

    from_cur = from_currency.upper()
    to_cur = to_currency.upper()

    today = date.today()

    try:
        if from_cur == "USD":
            if to_cur in rates:
                rate = rates[to_cur]
                await save_rate("USD", to_cur, rate, today)
                return rate
        elif to_cur == "USD":
            if from_cur in rates and rates[from_cur] != 0:
                rate = Decimal("1.0") / rates[from_cur]
                await save_rate(from_cur, "USD", rate, today)
                return rate
        else:
            if from_cur in rates and to_cur in rates and rates[from_cur] != 0:
                rate = rates[to_cur] / rates[from_cur]
                await save_rate(from_cur, to_cur, rate, today)
                return rate
    except Exception as e:
        logger.error(f"Error calculating rate {from_cur}->{to_cur}: {e}")

    return None


async def get_exchange_rate(from_currency: str, to_currency: str) -> Decimal:
    """
    Get exchange rate for converting from one currency to another.

    Priority:
    1. Today's cached rate from database
    2. Fresh rate from API (if available)
    3. Fallback hardcoded rates

    Returns rate such that: amount_from * rate = amount_to
    """
    from_cur = from_currency.upper()
    to_cur = to_currency.upper()
    
    if from_cur == to_cur:
        return Decimal("1.0")
    
    today = date.today()

    cached = await get_cached_rate(from_cur, to_cur, today)
    if cached:
        return cached

    api_rate = await fetch_rate_from_api(from_cur, to_cur)
    if api_rate:
        await save_rate(from_cur, to_cur, api_rate, today)
        return api_rate

    fallback = FALLBACK_RATES.get((from_cur, to_cur))
    if fallback:
        return fallback

    reverse_fallback = FALLBACK_RATES.get((to_cur, from_cur))
    if reverse_fallback and reverse_fallback != 0:
        return Decimal("1.0") / reverse_fallback

    if from_cur != "USD" and to_cur != "USD":
        rate_to_usd = FALLBACK_RATES.get((from_cur, "USD"))
        rate_from_usd = FALLBACK_RATES.get(("USD", to_cur))
        if rate_to_usd and rate_from_usd:
            return rate_to_usd * rate_from_usd

    return Decimal("1.0")


async def convert_to_usd(amount: Decimal, from_currency: str) -> tuple[Decimal, Decimal]:
    """
    Convert amount to USD.
    
    Returns: (amount_usd, exchange_rate)
    """
    if from_currency.upper() == "USD":
        return amount, Decimal("1.0")
    
    rate = await get_exchange_rate(from_currency, "USD")
    amount_usd = amount * rate

    amount_usd = amount_usd.quantize(Decimal("0.01"))
    
    return amount_usd, rate
