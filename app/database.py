import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
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
    """Finance account/family - groups transactions"""
    __tablename__ = "finance_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    webhook_token = Column(String, unique=True, nullable=False, default=generate_webhook_token)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (Index("idx_finance_webhook_token", "webhook_token"),)


class Transaction(Base):
    """Financial transaction"""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("finance_accounts.id"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    category = Column(String, nullable=True)
    merchant = Column(String, nullable=True)
    description = Column(String, nullable=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    source = Column(String, default="manual")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        Index("idx_transaction_account_timestamp", "account_id", "timestamp"),
    )


class Category(Base):
    """Finance categories for autocomplete"""
    __tablename__ = "finance_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("finance_accounts.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    usage_count = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_finance_account_category", "account_id", "name", unique=True),
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


# ============================================
# Database initialization
# ============================================

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
