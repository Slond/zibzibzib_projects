"""Finance router - transaction tracking with webhook for iOS Shortcuts"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, Query, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, func, desc

from app.database import (
    async_session,
    Transaction,
    Category,
    FinanceAccount,
    utcnow,
)
from app.auth import (
    get_current_user,
    has_service_access,
    get_finance_account_by_webhook,
    get_finance_account_by_id,
    regenerate_finance_webhook_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/finance")
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


async def require_finance_access(request: Request):
    """Check user has access to finance module"""
    user = await get_current_user(request)
    if not user:
        return None
    if not user.is_admin and not await has_service_access(user.id, "finance"):
        return None
    return user


# ============ Webhook (Token-based, no auth) ============

class WebhookData(BaseModel):
    amount: Optional[float] = None
    category: Optional[str] = None
    merchant: Optional[str] = None
    description: Optional[str] = None


@router.post("/api/webhook/{webhook_token}")
async def webhook_notification(webhook_token: str, data: WebhookData = None):
    """iOS Shortcut sends request here when bank notification arrives"""
    account = await get_finance_account_by_webhook(webhook_token)
    if not account:
        raise HTTPException(status_code=404, detail="Invalid webhook token")

    async with async_session() as session:
        transaction = Transaction(
            account_id=account.id,
            amount=Decimal(str(data.amount)) if data and data.amount else Decimal("0"),
            category=data.category if data else None,
            merchant=data.merchant if data else None,
            description=data.description if data else None,
            timestamp=utcnow(),
            source="shortcut",
        )
        session.add(transaction)
        await session.commit()
        await session.refresh(transaction)

        return {
            "status": "ok",
            "transaction_id": transaction.id,
            "edit_url": f"/finance/add?edit={transaction.id}",
        }


# ============ Pages ============

@router.get("")
@router.get("/")
async def finance_index(request: Request):
    """Finance dashboard with stats"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "finance/index.html",
        {"request": request, "user": user}
    )


@router.get("/add")
async def add_page(request: Request, edit: Optional[int] = None):
    """Add/edit transaction form"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    transaction = None
    if edit and user.finance_account_id:
        async with async_session() as session:
            result = await session.execute(
                select(Transaction).where(
                    Transaction.id == edit,
                    Transaction.account_id == user.finance_account_id
                )
            )
            transaction = result.scalar_one_or_none()

    categories = []
    if user.finance_account_id:
        async with async_session() as session:
            result = await session.execute(
                select(Category)
                .where(Category.account_id == user.finance_account_id)
                .order_by(desc(Category.usage_count))
                .limit(20)
            )
            categories = result.scalars().all()

    return templates.TemplateResponse(
        "finance/add.html",
        {
            "request": request,
            "user": user,
            "transaction": transaction,
            "categories": categories,
        }
    )


@router.post("/add")
async def add_transaction(
    request: Request,
    amount: str = Form(...),
    category: str = Form(None),
    merchant: str = Form(None),
    description: str = Form(None),
    transaction_id: int = Form(None),
):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        return RedirectResponse(url="/login", status_code=302)

    try:
        amount_decimal = Decimal(amount.replace(",", ".").replace(" ", ""))
    except:
        return RedirectResponse(url="/finance/add?error=invalid_amount", status_code=302)

    async with async_session() as session:
        if transaction_id:
            result = await session.execute(
                select(Transaction).where(
                    Transaction.id == transaction_id,
                    Transaction.account_id == user.finance_account_id
                )
            )
            transaction = result.scalar_one_or_none()
            if transaction:
                transaction.amount = amount_decimal
                transaction.category = category or None
                transaction.merchant = merchant or None
                transaction.description = description or None
                transaction.created_by_user_id = user.id
        else:
            transaction = Transaction(
                account_id=user.finance_account_id,
                amount=amount_decimal,
                category=category or None,
                merchant=merchant or None,
                description=description or None,
                timestamp=utcnow(),
                source="manual",
                created_by_user_id=user.id,
            )
            session.add(transaction)

        if category:
            cat_result = await session.execute(
                select(Category).where(
                    Category.account_id == user.finance_account_id,
                    Category.name == category
                )
            )
            existing_cat = cat_result.scalar_one_or_none()
            if existing_cat:
                existing_cat.usage_count += 1
            else:
                new_cat = Category(
                    account_id=user.finance_account_id,
                    name=category,
                    usage_count=1,
                )
                session.add(new_cat)

        await session.commit()

    return RedirectResponse(url="/finance", status_code=302)


@router.get("/history")
async def history_page(request: Request, page: int = 1, category: str = None):
    """Transaction history with filters"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    transactions = []
    categories = []
    has_more = False

    if user.finance_account_id:
        per_page = 50
        offset = (page - 1) * per_page

        async with async_session() as session:
            query = select(Transaction).where(Transaction.account_id == user.finance_account_id)
            if category:
                query = query.where(Transaction.category == category)
            query = query.order_by(desc(Transaction.timestamp)).offset(offset).limit(per_page + 1)

            result = await session.execute(query)
            transactions = result.scalars().all()

            has_more = len(transactions) > per_page
            transactions = transactions[:per_page]

            cat_result = await session.execute(
                select(Category)
                .where(Category.account_id == user.finance_account_id)
                .order_by(desc(Category.usage_count))
            )
            categories = cat_result.scalars().all()

    return templates.TemplateResponse(
        "finance/history.html",
        {
            "request": request,
            "user": user,
            "transactions": transactions,
            "categories": categories,
            "current_category": category,
            "page": page,
            "has_more": has_more,
        }
    )


@router.post("/transactions/{transaction_id}/delete")
async def delete_transaction(request: Request, transaction_id: int):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(Transaction).where(
                Transaction.id == transaction_id,
                Transaction.account_id == user.finance_account_id
            )
        )
        transaction = result.scalar_one_or_none()
        if transaction:
            await session.delete(transaction)
            await session.commit()

    return RedirectResponse(url="/finance/history", status_code=302)


@router.get("/settings")
async def settings_page(request: Request):
    """Finance settings with webhook URL"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    account = None
    if user.finance_account_id:
        account = await get_finance_account_by_id(user.finance_account_id)

    return templates.TemplateResponse(
        "finance/settings.html",
        {"request": request, "user": user, "account": account}
    )


@router.post("/settings/regenerate-token")
async def regenerate_token(request: Request):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        return RedirectResponse(url="/login", status_code=302)

    await regenerate_finance_webhook_token(user.finance_account_id)
    return RedirectResponse(url="/finance/settings", status_code=302)


# ============ API Endpoints ============

@router.get("/api/transactions")
async def api_get_transactions(
    request: Request,
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    category: str = Query(None),
):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    async with async_session() as session:
        query = select(Transaction).where(Transaction.account_id == user.finance_account_id)
        if category:
            query = query.where(Transaction.category == category)
        query = query.order_by(desc(Transaction.timestamp)).offset(offset).limit(limit)

        result = await session.execute(query)
        transactions = result.scalars().all()

        return [
            {
                "id": t.id,
                "amount": float(t.amount),
                "category": t.category,
                "merchant": t.merchant,
                "description": t.description,
                "timestamp": t.timestamp.isoformat(),
                "source": t.source,
            }
            for t in transactions
        ]


@router.get("/api/categories")
async def api_get_categories(request: Request):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    async with async_session() as session:
        result = await session.execute(
            select(Category)
            .where(Category.account_id == user.finance_account_id)
            .order_by(desc(Category.usage_count))
        )
        categories = result.scalars().all()

        return [
            {"id": c.id, "name": c.name, "icon": c.icon, "usage_count": c.usage_count}
            for c in categories
        ]


@router.get("/api/stats")
async def api_get_stats(request: Request, days: int = Query(30, le=365)):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    since = datetime.now(tz=timezone.utc) - timedelta(days=days)

    async with async_session() as session:
        result = await session.execute(
            select(
                Transaction.category,
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count"),
            )
            .where(
                Transaction.account_id == user.finance_account_id,
                Transaction.timestamp >= since,
            )
            .group_by(Transaction.category)
        )
        rows = result.all()

        total_result = await session.execute(
            select(func.sum(Transaction.amount))
            .where(
                Transaction.account_id == user.finance_account_id,
                Transaction.timestamp >= since,
            )
        )
        total = total_result.scalar() or 0

        return {
            "period_days": days,
            "total": float(total),
            "by_category": [
                {
                    "category": row.category or "Без категории",
                    "total": float(row.total),
                    "count": row.count,
                }
                for row in rows
            ],
        }
