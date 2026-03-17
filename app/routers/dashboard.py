"""Dashboard router - main page with service tiles and admin panel"""
import logging
from pathlib import Path

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session, User, Service, UserServiceAccess, FinanceAccount
from app.auth import (
    get_current_user,
    get_user_services,
    get_all_users,
    create_user,
    delete_user,
    hash_password,
    grant_service_access,
    revoke_service_access,
)

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


@router.get("/")
async def index(request: Request):
    """Main dashboard with service tiles"""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.must_change_password:
        return RedirectResponse(url="/change-password", status_code=302)

    user_services = await get_user_services(user.id)

    return templates.TemplateResponse(
        "dashboard/index.html",
        {"request": request, "user": user, "services": user_services}
    )


@router.get("/admin")
async def admin_page(request: Request):
    """Admin panel"""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse(url="/", status_code=302)

    users = await get_all_users()

    async with async_session() as session:
        services_result = await session.execute(select(Service).order_by(Service.order))
        services = services_result.scalars().all()

        accounts_result = await session.execute(
            select(FinanceAccount)
            .options(selectinload(FinanceAccount.events))
            .order_by(FinanceAccount.name)
        )
        accounts_raw = accounts_result.scalars().all()

        accounts = [
            {
                "id": acc.id,
                "name": acc.name,
                "events_count": len(acc.events),
                "created_at": acc.created_at,
            }
            for acc in accounts_raw
        ]

        access_result = await session.execute(select(UserServiceAccess))
        all_access = access_result.scalars().all()

    access_map = {f"{a.user_id}_{a.service_id}": a for a in all_access}

    return templates.TemplateResponse(
        "dashboard/admin.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "services": services,
            "accounts": accounts,
            "access_map": access_map,
        }
    )


@router.post("/admin/users/create")
async def admin_create_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(None),
    is_admin: bool = Form(False),
    finance_account_id: int = Form(None),
):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    try:
        await create_user(
            email=email,
            password=password,
            name=name,
            is_admin=is_admin,
            finance_account_id=finance_account_id if finance_account_id else None,
        )
    except Exception as e:
        logger.error(f"Failed to create user: {e}")

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(request: Request, user_id: int):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    if user.id == user_id:
        return RedirectResponse(url="/admin", status_code=302)

    await delete_user(user_id)
    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(request: Request, user_id: int):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        target_user = result.scalar_one_or_none()
        if target_user:
            target_user.password_hash = hash_password("123456")
            target_user.must_change_password = True
            await session.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/users/{user_id}/edit")
async def admin_edit_user(
    request: Request,
    user_id: int,
    name: str = Form(None),
    finance_account_id: int = Form(None),
    is_admin: bool = Form(False),
):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        target_user = result.scalar_one_or_none()
        if target_user:
            target_user.name = name if name else None
            target_user.finance_account_id = finance_account_id if finance_account_id else None
            # Don't allow removing own admin status
            if user_id != user.id:
                target_user.is_admin = is_admin
            await session.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/accounts/create")
async def admin_create_account(request: Request, name: str = Form(...)):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        account = FinanceAccount(name=name)
        session.add(account)
        await session.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/services/create")
async def admin_create_service(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    route: str = Form(...),
    icon: str = Form(None),
    description: str = Form(None),
):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        max_order = await session.execute(select(Service.order).order_by(Service.order.desc()).limit(1))
        order = (max_order.scalar() or 0) + 1

        service = Service(
            name=name,
            slug=slug,
            route=route,
            icon=icon,
            description=description,
            order=order,
        )
        session.add(service)
        await session.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/services/{service_id}/delete")
async def admin_delete_service(request: Request, service_id: int):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(Service).where(Service.id == service_id))
        service = result.scalar_one_or_none()
        if service:
            await session.delete(service)
            await session.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/access/grant")
async def admin_grant_access(
    request: Request,
    user_id: int = Form(...),
    service_id: int = Form(...),
    role: str = Form("user"),
):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    await grant_service_access(user_id=user_id, service_id=service_id, role=role)
    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/access/revoke")
async def admin_revoke_access(
    request: Request,
    user_id: int = Form(...),
    service_id: int = Form(...),
):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return RedirectResponse(url="/login", status_code=302)

    await revoke_service_access(user_id, service_id)
    return RedirectResponse(url="/admin", status_code=302)


# ============================================
# User Profile
# ============================================

@router.get("/profile")
async def profile_page(request: Request, success: str = None, error: str = None):
    """User profile page"""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "dashboard/profile.html",
        {"request": request, "user": user, "success": success, "error": error}
    )


@router.post("/profile/update-name")
async def profile_update_name(request: Request, name: str = Form(None)):
    """Update user's name"""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            db_user.name = name if name else None
            await session.commit()

    return RedirectResponse(url="/profile?success=Имя обновлено", status_code=302)


@router.post("/profile/update-email")
async def profile_update_email(
    request: Request,
    email: str = Form(...),
    current_password: str = Form(...),
):
    """Update user's email"""
    from app.auth import verify_password

    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            return RedirectResponse(url="/profile?error=Пользователь не найден", status_code=302)

        if not verify_password(current_password, db_user.password_hash):
            return RedirectResponse(url="/profile?error=Неверный пароль", status_code=302)

        # Check if email is taken
        existing = await session.execute(select(User).where(User.email == email, User.id != user.id))
        if existing.scalar_one_or_none():
            return RedirectResponse(url="/profile?error=Email уже используется", status_code=302)

        db_user.email = email
        await session.commit()

    return RedirectResponse(url="/profile?success=Email обновлён", status_code=302)


@router.post("/profile/update-password")
async def profile_update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    """Update user's password"""
    from app.auth import verify_password

    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if new_password != confirm_password:
        return RedirectResponse(url="/profile?error=Пароли не совпадают", status_code=302)

    if len(new_password) < 6:
        return RedirectResponse(url="/profile?error=Пароль слишком короткий (минимум 6 символов)", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            return RedirectResponse(url="/profile?error=Пользователь не найден", status_code=302)

        if not verify_password(current_password, db_user.password_hash):
            return RedirectResponse(url="/profile?error=Неверный текущий пароль", status_code=302)

        db_user.password_hash = hash_password(new_password)
        db_user.must_change_password = False
        await session.commit()

    return RedirectResponse(url="/profile?success=Пароль изменён", status_code=302)
