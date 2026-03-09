import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from app.config import DATABASE_URL

os.makedirs("./data", exist_ok=True)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


def utcnow():
    return datetime.now(tz=timezone.utc)


def generate_webhook_token():
    return secrets.token_urlsafe(32)


# ============================================
# User & Access Control
# ============================================

class User(Base):
    """System user - unified for all modules"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    name = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    must_change_password = Column(Boolean, default=True)
    finance_account_id = Column(Integer, ForeignKey("finance_accounts.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    service_access = relationship("UserServiceAccess", back_populates="user", cascade="all, delete-orphan")


class Service(Base):
    """Registered service for dashboard tiles"""
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    route = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    description = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    order = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)

    user_access = relationship("UserServiceAccess", back_populates="service", cascade="all, delete-orphan")


class UserServiceAccess(Base):
    """User access to service"""
    __tablename__ = "user_service_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False, index=True)
    role = Column(String, default="user")
    granted_at = Column(DateTime, default=utcnow)

    user = relationship("User", back_populates="service_access", foreign_keys=[user_id])
    service = relationship("Service", back_populates="user_access")

    __table_args__ = (
        Index("idx_user_service", "user_id", "service_id", unique=True),
    )


# ============================================
# Finance Module
# ============================================

class FinanceAccount(Base):
    """Finance account/family - groups events and categories"""
    __tablename__ = "finance_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    events = relationship("FinanceEvent", back_populates="account", cascade="all, delete-orphan")
    categories = relationship("Category", back_populates="account", cascade="all, delete-orphan")
    tags = relationship("CategoryTag", back_populates="account", cascade="all, delete-orphan")


class FinanceEvent(Base):
    """Finance event - logical grouping of transactions (daily expenses, trip, etc.)"""
    __tablename__ = "finance_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("finance_accounts.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    event_type = Column(String, default="personal")  # "personal" / "shared"
    default_currency = Column(String, default="KZT")
    webhook_token = Column(String, unique=True, nullable=False, default=generate_webhook_token)
    is_active = Column(Boolean, default=True)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)

    account = relationship("FinanceAccount", back_populates="events")
    transactions = relationship("Transaction", back_populates="event", cascade="all, delete-orphan")
    members = relationship("FinanceEventMember", back_populates="event", cascade="all, delete-orphan")
    budgets = relationship("CategoryBudget", back_populates="event", cascade="all, delete-orphan")
    recurring = relationship("RecurringTransaction", back_populates="event", cascade="all, delete-orphan")

    __table_args__ = (Index("idx_finance_event_webhook", "webhook_token"),)


class FinanceEventMember(Base):
    """Member access to shared finance event"""
    __tablename__ = "finance_event_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("finance_events.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, default="member")  # "owner" / "member"
    joined_at = Column(DateTime, default=utcnow)

    event = relationship("FinanceEvent", back_populates="members")
    user = relationship("User")

    __table_args__ = (
        Index("idx_event_member_unique", "event_id", "user_id", unique=True),
    )


class Transaction(Base):
    """Financial transaction"""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("finance_accounts.id"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("finance_events.id"), nullable=True, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, default="KZT")
    amount_usd = Column(Numeric(12, 2), nullable=True)
    exchange_rate = Column(Numeric(12, 6), nullable=True)
    category = Column(String, nullable=True)
    description = Column(String, nullable=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    source = Column(String, default="manual")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    event = relationship("FinanceEvent", back_populates="transactions")

    __table_args__ = (
        Index("idx_transaction_account_timestamp", "account_id", "timestamp"),
        Index("idx_transaction_event_timestamp", "event_id", "timestamp"),
    )


class CategoryTag(Base):
    """Meta-category tag for grouping categories"""
    __tablename__ = "category_tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("finance_accounts.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    color = Column(String, nullable=True)
    display_order = Column(Integer, default=0)

    account = relationship("FinanceAccount", back_populates="tags")
    categories = relationship("Category", back_populates="tag")

    __table_args__ = (
        Index("idx_category_tag_account_name", "account_id", "name", unique=True),
    )


class Category(Base):
    """Finance categories for autocomplete"""
    __tablename__ = "finance_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("finance_accounts.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    usage_count = Column(Integer, default=0)
    tag_id = Column(Integer, ForeignKey("category_tags.id"), nullable=True, index=True)

    account = relationship("FinanceAccount", back_populates="categories")
    tag = relationship("CategoryTag", back_populates="categories")
    budgets = relationship("CategoryBudget", back_populates="category", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_finance_account_category", "account_id", "name", unique=True),
    )


class CategoryBudget(Base):
    """Monthly budget limit for a category"""
    __tablename__ = "category_budgets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category_id = Column(Integer, ForeignKey("finance_categories.id"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("finance_events.id"), nullable=True, index=True)
    monthly_limit = Column(Numeric(12, 2), nullable=False)
    alert_threshold = Column(Integer, default=70)  # percent
    created_at = Column(DateTime, default=utcnow)

    category = relationship("Category", back_populates="budgets")
    event = relationship("FinanceEvent", back_populates="budgets")

    __table_args__ = (
        Index("idx_budget_category_event", "category_id", "event_id", unique=True),
    )


class RecurringTransaction(Base):
    """Template for recurring transactions"""
    __tablename__ = "recurring_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("finance_events.id"), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("finance_categories.id"), nullable=True, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, default="KZT")
    description = Column(String, nullable=True)
    frequency = Column(String, nullable=False)  # "daily", "weekly", "monthly", "every_n_days"
    frequency_value = Column(Integer, default=1)  # N for "every_n_days"
    next_run = Column(DateTime, nullable=False)
    last_run = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    event = relationship("FinanceEvent", back_populates="recurring")
    category = relationship("Category")


class ExchangeRate(Base):
    """Cached exchange rates"""
    __tablename__ = "exchange_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_currency = Column(String, nullable=False)
    to_currency = Column(String, nullable=False)
    rate = Column(Numeric(12, 6), nullable=False)
    rate_date = Column(Date, nullable=False, index=True)

    __table_args__ = (
        Index("idx_exchange_rate_currencies_date", "from_currency", "to_currency", "rate_date", unique=True),
    )


# ============================================
# Weather/IoT Module
# ============================================

class Device(Base):
    """IoT device from Yandex Smart Home"""
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    device_type = Column(String, nullable=True)
    room = Column(String, nullable=True)
    updated_at = Column(DateTime, default=utcnow)

    __table_args__ = (Index("idx_device_id", "device_id", unique=True),)


class Measurement(Base):
    """Device measurement reading"""
    __tablename__ = "measurements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String, nullable=False, index=True)
    property_instance = Column(String, nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String, nullable=True)
    timestamp = Column(DateTime, default=utcnow, index=True)

    __table_args__ = (
        Index("idx_measurement_device_property_time", "device_id", "property_instance", "timestamp"),
    )


class WeatherDisplaySetting(Base):
    """Settings for which device properties to display on weather dashboard"""
    __tablename__ = "weather_display_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String, nullable=False, index=True)
    property_instance = Column(String, nullable=False)
    show_on_dashboard = Column(Boolean, default=False)
    display_order = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_weather_display_device_prop", "device_id", "property_instance", unique=True),
    )


class IQAirSensor(Base):
    """IQAir external air quality sensor"""
    __tablename__ = "iqair_sensors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    track_pm25 = Column(Boolean, default=True)
    track_pm10 = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    last_poll = Column(DateTime, nullable=True)


# ============================================
# Database initialization
# ============================================

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
