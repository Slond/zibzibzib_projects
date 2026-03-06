from typing import Optional

from fastapi import Request, HTTPException
from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import select

from app.config import SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
from app.database import (
    async_session,
    User,
    Service,
    UserServiceAccess,
    FinanceAccount,
    FinanceEvent,
    FinanceEventMember,
    generate_webhook_token,
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(SECRET_KEY)

SESSION_MAX_AGE = 60 * 60 * 24 * 7
SESSION_COOKIE = "session"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_session_token(user_id: int) -> str:
    return serializer.dumps({"user_id": user_id})


def verify_session_token(token: str) -> dict | None:
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data
    except (BadSignature, SignatureExpired):
        return None


async def get_user_by_email(email: str) -> User | None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


async def get_user_by_id(user_id: int) -> User | None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()


async def authenticate_user(email: str, password: str) -> User | None:
    user = await get_user_by_email(email)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def create_user(
    email: str,
    password: str,
    name: str = None,
    is_admin: bool = False,
    finance_account_id: int = None,
) -> User:
    async with async_session() as session:
        user = User(
            email=email,
            password_hash=hash_password(password),
            name=name,
            is_admin=is_admin,
            finance_account_id=finance_account_id,
            must_change_password=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def update_user_password(user_id: int, new_password: str):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.password_hash = hash_password(new_password)
            user.must_change_password = False
            await session.commit()


async def delete_user(user_id: int):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            await session.delete(user)
            await session.commit()


async def get_all_users() -> list[User]:
    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()))
        return result.scalars().all()


async def get_current_user(request: Request) -> Optional[User]:
    """Get current user from session cookie"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = verify_session_token(token)
    if not data:
        return None
    user = await get_user_by_id(data["user_id"])
    return user


async def require_auth(request: Request) -> User:
    """Require authenticated user"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def require_admin(request: Request) -> User:
    """Require admin user"""
    user = await require_auth(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def ensure_admin_exists():
    """Create default admin and initial data if database is empty"""
    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()

        if not users:
            finance_account = FinanceAccount(name="Default Account")
            session.add(finance_account)
            await session.flush()

            # Create default finance event
            default_event = FinanceEvent(
                account_id=finance_account.id,
                name="Ежедневные траты",
                event_type="personal",
                default_currency="KZT",
                is_active=True,
            )
            session.add(default_event)

            admin = User(
                email=ADMIN_EMAIL,
                password_hash=hash_password(ADMIN_PASSWORD),
                name="Administrator",
                is_admin=True,
                finance_account_id=finance_account.id,
                must_change_password=True,
            )
            session.add(admin)

            default_services = [
                Service(
                    name="Finance Tracker",
                    slug="finance",
                    route="/finance",
                    icon="💰",
                    description="Учет личных финансов",
                    order=1,
                ),
                Service(
                    name="Home Weather",
                    slug="weather",
                    route="/weather",
                    icon="🌡️",
                    description="Мониторинг датчиков",
                    order=2,
                ),
            ]
            for svc in default_services:
                session.add(svc)

            await session.commit()


async def get_user_services(user_id: int) -> list[dict]:
    """Get all services user has access to"""
    async with async_session() as session:
        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()

        if user and user.is_admin:
            services_result = await session.execute(
                select(Service).where(Service.is_active == True).order_by(Service.order)
            )
            services = services_result.scalars().all()
            return [{"service": s, "role": "admin"} for s in services]

        result = await session.execute(
            select(UserServiceAccess, Service)
            .join(Service, UserServiceAccess.service_id == Service.id)
            .where(
                UserServiceAccess.user_id == user_id,
                Service.is_active == True,
            )
            .order_by(Service.order)
        )
        rows = result.all()
        return [
            {"service": row.Service, "role": row.UserServiceAccess.role}
            for row in rows
        ]


async def has_service_access(user_id: int, service_slug: str) -> bool:
    """Check if user has access to a specific service"""
    async with async_session() as session:
        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()

        if user and user.is_admin:
            return True

        result = await session.execute(
            select(UserServiceAccess)
            .join(Service, UserServiceAccess.service_id == Service.id)
            .where(
                UserServiceAccess.user_id == user_id,
                Service.slug == service_slug,
                Service.is_active == True,
            )
        )
        return result.scalar_one_or_none() is not None


async def grant_service_access(user_id: int, service_id: int, role: str = "user"):
    async with async_session() as session:
        existing = await session.execute(
            select(UserServiceAccess).where(
                UserServiceAccess.user_id == user_id,
                UserServiceAccess.service_id == service_id,
            )
        )
        if existing.scalar_one_or_none():
            return

        access = UserServiceAccess(
            user_id=user_id,
            service_id=service_id,
            role=role,
        )
        session.add(access)
        await session.commit()


async def revoke_service_access(user_id: int, service_id: int):
    async with async_session() as session:
        result = await session.execute(
            select(UserServiceAccess).where(
                UserServiceAccess.user_id == user_id,
                UserServiceAccess.service_id == service_id,
            )
        )
        access = result.scalar_one_or_none()
        if access:
            await session.delete(access)
            await session.commit()


async def get_finance_account_by_id(account_id: int) -> FinanceAccount | None:
    async with async_session() as session:
        result = await session.execute(
            select(FinanceAccount).where(FinanceAccount.id == account_id)
        )
        return result.scalar_one_or_none()


async def get_finance_event_by_webhook(token: str) -> FinanceEvent | None:
    """Get finance event by webhook token"""
    async with async_session() as session:
        result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.webhook_token == token)
        )
        return result.scalar_one_or_none()


async def get_finance_event_by_id(event_id: int) -> FinanceEvent | None:
    """Get finance event by ID"""
    async with async_session() as session:
        result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.id == event_id)
        )
        return result.scalar_one_or_none()


async def get_user_events(user_id: int, account_id: int) -> list[FinanceEvent]:
    """Get all events user has access to (owned + member)"""
    async with async_session() as session:
        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            return []
        
        # Get events owned by user's account
        owned_events = await session.execute(
            select(FinanceEvent)
            .where(
                FinanceEvent.account_id == account_id,
                FinanceEvent.is_archived == False,
            )
            .order_by(FinanceEvent.created_at.desc())
        )
        owned = list(owned_events.scalars().all())
        
        # Get events where user is a member (shared events)
        member_events = await session.execute(
            select(FinanceEvent)
            .join(FinanceEventMember, FinanceEvent.id == FinanceEventMember.event_id)
            .where(
                FinanceEventMember.user_id == user_id,
                FinanceEvent.is_archived == False,
            )
            .order_by(FinanceEvent.created_at.desc())
        )
        member = list(member_events.scalars().all())
        
        # Combine and deduplicate
        seen_ids = set()
        result = []
        for event in owned + member:
            if event.id not in seen_ids:
                seen_ids.add(event.id)
                result.append(event)
        
        return result


async def regenerate_event_webhook_token(event_id: int) -> str | None:
    """Regenerate webhook token for an event"""
    async with async_session() as session:
        result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.id == event_id)
        )
        event = result.scalar_one_or_none()
        if event:
            event.webhook_token = generate_webhook_token()
            await session.commit()
            return event.webhook_token
        return None


async def create_default_event(account_id: int) -> FinanceEvent:
    """Create default 'Daily expenses' event for new account"""
    async with async_session() as session:
        event = FinanceEvent(
            account_id=account_id,
            name="Ежедневные траты",
            event_type="personal",
            default_currency="KZT",
            is_active=True,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event
