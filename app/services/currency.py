"""Currency conversion service with rate caching
Uses Open Exchange Rates API: https://openexchangerates.org
"""
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import select, and_, desc

from app.database import async_session, ExchangeRate

logger = logging.getLogger(__name__)

# Open Exchange Rates API configuration
OPENEXCHANGERATES_APP_ID = "c6dcf85bc4fc4db19705db0bcaacfd24"
OPENEXCHANGERATES_BASE_URL = "https://openexchangerates.org/api"

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


async def fetch_rates_from_api(for_date: Optional[date] = None) -> Optional[dict[str, Decimal]]:
    """
    Fetch all exchange rates from Open Exchange Rates API.
    Returns rates relative to USD (base currency).
    
    Args:
        for_date: Optional date for historical rates. If None, fetches latest rates.
    
    The API returns: { "rates": { "EUR": 0.92, "KZT": 460, ... } }
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if for_date and for_date < date.today():
                # Historical rates endpoint
                url = f"{OPENEXCHANGERATES_BASE_URL}/historical/{for_date.isoformat()}.json"
            else:
                # Latest rates endpoint
                url = f"{OPENEXCHANGERATES_BASE_URL}/latest.json"
            
            response = await client.get(url, params={"app_id": OPENEXCHANGERATES_APP_ID})
            
            if response.status_code == 200:
                data = response.json()
                rates = {}
                for currency, rate in data.get("rates", {}).items():
                    rates[currency] = Decimal(str(rate))
                logger.info(f"Fetched {len(rates)} exchange rates from API for {for_date or 'today'}")
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


async def get_closest_cached_rate(from_currency: str, to_currency: str, target_date: date) -> Optional[Decimal]:
    """
    Get the closest available cached rate to the target date.
    Searches within 30 days before the target date.
    """
    async with async_session() as session:
        min_date = target_date - timedelta(days=30)
        result = await session.execute(
            select(ExchangeRate)
            .where(
                and_(
                    ExchangeRate.from_currency == from_currency.upper(),
                    ExchangeRate.to_currency == to_currency.upper(),
                    ExchangeRate.rate_date >= min_date,
                    ExchangeRate.rate_date <= target_date,
                )
            )
            .order_by(desc(ExchangeRate.rate_date))
            .limit(1)
        )
        rate_record = result.scalar_one_or_none()
        if rate_record:
            logger.info(f"Using cached rate from {rate_record.rate_date} for {from_currency}->{to_currency}")
            return rate_record.rate
    return None


async def fetch_rate_from_api(from_currency: str, to_currency: str, for_date: Optional[date] = None) -> Optional[Decimal]:
    target_date = for_date or date.today()
    rates = await fetch_rates_from_api(for_date=target_date)
    if not rates:
        return None

    from_cur = from_currency.upper()
    to_cur = to_currency.upper()

    try:
        if from_cur == "USD":
            if to_cur in rates:
                rate = rates[to_cur]
                await save_rate("USD", to_cur, rate, target_date)
                return rate
        elif to_cur == "USD":
            if from_cur in rates and rates[from_cur] != 0:
                rate = Decimal("1.0") / rates[from_cur]
                await save_rate(from_cur, "USD", rate, target_date)
                return rate
        else:
            if from_cur in rates and to_cur in rates and rates[from_cur] != 0:
                rate = rates[to_cur] / rates[from_cur]
                await save_rate(from_cur, to_cur, rate, target_date)
                return rate
    except Exception as e:
        logger.error(f"Error calculating rate {from_cur}->{to_cur}: {e}")

    return None


async def get_exchange_rate(from_currency: str, to_currency: str, for_date: Optional[date] = None) -> Decimal:
    """
    Get exchange rate for converting from one currency to another.

    Priority:
    1. Exact date cached rate from database
    2. Fresh rate from API for that date (if available)
    3. Closest available cached rate (within 30 days)
    4. Fallback hardcoded rates

    Args:
        from_currency: Source currency code
        to_currency: Target currency code
        for_date: Optional date for historical rate. If None, uses today.

    Returns rate such that: amount_from * rate = amount_to
    """
    from_cur = from_currency.upper()
    to_cur = to_currency.upper()
    
    if from_cur == to_cur:
        return Decimal("1.0")
    
    target_date = for_date or date.today()

    # 1. Try exact date cached rate
    cached = await get_cached_rate(from_cur, to_cur, target_date)
    if cached:
        return cached

    # 2. Try API for that date
    api_rate = await fetch_rate_from_api(from_cur, to_cur, for_date=target_date)
    if api_rate:
        return api_rate

    # 3. Try closest cached rate (for historical dates)
    if for_date:
        closest = await get_closest_cached_rate(from_cur, to_cur, target_date)
        if closest:
            return closest

    # 4. Fallback to hardcoded rates
    fallback = FALLBACK_RATES.get((from_cur, to_cur))
    if fallback:
        return fallback

    reverse_fallback = FALLBACK_RATES.get((to_cur, from_cur))
    if reverse_fallback and reverse_fallback != 0:
        return Decimal("1.0") / reverse_fallback

    # Try converting through USD
    if from_cur != "USD" and to_cur != "USD":
        rate_to_usd = FALLBACK_RATES.get((from_cur, "USD"))
        rate_from_usd = FALLBACK_RATES.get(("USD", to_cur))
        if rate_to_usd and rate_from_usd:
            return rate_to_usd * rate_from_usd

    # Unknown currency pair
    return Decimal("1.0")


async def convert_to_usd(amount: Decimal, from_currency: str, for_date: Optional[date] = None) -> tuple[Decimal, Decimal]:
    """
    Convert amount to USD.
    
    Args:
        amount: Amount to convert
        from_currency: Source currency code
        for_date: Optional date for historical rate
    
    Returns: (amount_usd, exchange_rate)
    """
    if from_currency.upper() == "USD":
        return amount, Decimal("1.0")
    
    rate = await get_exchange_rate(from_currency, "USD", for_date=for_date)
    amount_usd = amount * rate

    amount_usd = amount_usd.quantize(Decimal("0.01"))
    
    return amount_usd, rate
