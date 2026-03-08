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
from sqlalchemy import select, func, desc, and_, or_

from app.database import (
    async_session,
    Transaction,
    Category,
    CategoryTag,
    CategoryBudget,
    FinanceAccount,
    FinanceEvent,
    FinanceEventMember,
    RecurringTransaction,
    utcnow,
)
from app.auth import (
    get_current_user,
    has_service_access,
    get_finance_event_by_webhook,
    get_finance_event_by_id,
    get_finance_account_by_id,
    get_user_events,
    regenerate_event_webhook_token,
)
from app.services.currency import convert_to_usd

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
    type: Optional[str] = "expense"  # "expense" or "income"
    category: Optional[str] = None
    description: Optional[str] = None
    currency: Optional[str] = None


@router.post("/api/webhook/{webhook_token}")
async def webhook_notification(webhook_token: str, data: WebhookData = None):
    """iOS Shortcut sends transaction data here.
    
    Accepts positive amount with type field:
    - type="expense" -> amount becomes negative
    - type="income" -> amount stays positive
    """
    event = await get_finance_event_by_webhook(webhook_token)
    if not event:
        raise HTTPException(status_code=404, detail="Invalid webhook token")

    async with async_session() as session:
        event_result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.id == event.id)
        )
        event_fresh = event_result.scalar_one()
        
        # Parse amount (always positive from Shortcut)
        raw_amount = abs(Decimal(str(data.amount))) if data and data.amount else Decimal("0")
        
        # Apply sign based on type
        tx_type = (data.type or "expense").lower() if data else "expense"
        if tx_type == "expense":
            amount = -raw_amount
        else:
            amount = raw_amount
        
        # Normalize inputs
        currency = (data.currency.upper().strip() if data and data.currency 
                    else event_fresh.default_currency)
        category = (data.category.strip().capitalize() if data and data.category 
                    else None)
        description = (data.description.strip().capitalize() if data and data.description 
                       else None)
        
        # Convert to USD
        amount_usd, exchange_rate = await convert_to_usd(amount, currency)
        
        transaction = Transaction(
            account_id=event_fresh.account_id,
            event_id=event_fresh.id,
            amount=amount,
            currency=currency,
            amount_usd=amount_usd,
            exchange_rate=exchange_rate,
            category=category,
            description=description,
            timestamp=utcnow(),
            source="shortcut",
        )
        session.add(transaction)
        await session.commit()
        await session.refresh(transaction)

        return {
            "status": "ok",
            "transaction_id": transaction.id,
            "event_id": event_fresh.id,
            "type": tx_type,
            "amount": float(amount),
            "currency": currency,
            "amount_usd": float(amount_usd),
            "category": category,
            "description": description,
            "edit_url": f"/finance/add?event={event_fresh.id}&edit={transaction.id}",
        }


@router.get("/api/webhook/{webhook_token}/categories")
async def webhook_get_categories(webhook_token: str):
    """Get categories for iOS Shortcut picker (simple string list)"""
    event = await get_finance_event_by_webhook(webhook_token)
    if not event:
        raise HTTPException(status_code=404, detail="Invalid webhook token")

    async with async_session() as session:
        event_result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.id == event.id)
        )
        event_fresh = event_result.scalar_one()
        
        result = await session.execute(
            select(Category.name, Category.icon)
            .where(Category.account_id == event_fresh.account_id)
            .order_by(desc(Category.usage_count))
            .limit(20)
        )
        categories = result.all()
        
        # Return simple list of strings for Shortcuts compatibility
        category_names = [
            f"{cat.icon} {cat.name}".strip() if cat.icon else cat.name
            for cat in categories
        ]
        
        return {
            "event_name": event_fresh.name,
            "default_currency": event_fresh.default_currency,
            "currencies": ["KZT", "USD", "EUR", "RUB"],
            "categories": category_names
        }


# ============ Pages ============

@router.get("")
@router.get("/")
async def finance_index(
    request: Request,
    budget_alert: int = Query(None),
    cat: str = Query(None),
    spent: float = Query(None),
    limit: float = Query(None),
    percent: float = Query(None),
):
    """Finance dashboard with stats and events overview"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    events = []
    if user.finance_account_id:
        events = await get_user_events(user.id, user.finance_account_id)

    # Budget alert data
    alert = None
    if budget_alert and cat:
        alert = {
            "category": cat,
            "spent": spent or 0,
            "limit": limit or 0,
            "percent": percent or 0,
            "remaining": (limit or 0) - (spent or 0),
        }

    return templates.TemplateResponse(
        "finance/index.html",
        {
            "request": request,
            "user": user,
            "events": events,
            "budget_alert": alert,
        }
    )


BASE_CURRENCIES = ["KZT", "USD", "EUR", "RUB"]


async def get_recent_currencies(account_id: int) -> list[str]:
    """Get currencies used in the last month"""
    since = datetime.now(tz=timezone.utc) - timedelta(days=30)
    async with async_session() as session:
        result = await session.execute(
            select(Transaction.currency)
            .where(
                Transaction.account_id == account_id,
                Transaction.timestamp >= since,
                Transaction.currency.notin_(BASE_CURRENCIES),
            )
            .distinct()
        )
        return [row[0] for row in result.all() if row[0]]


@router.get("/add")
async def add_page(
    request: Request,
    event: Optional[int] = None,
    edit: Optional[int] = None,
):
    """Add/edit transaction form with event selection"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    transaction = None
    events = []
    selected_event = None
    categories = []
    currencies = list(BASE_CURRENCIES)
    
    if user.finance_account_id:
        # Get user's events
        events = await get_user_events(user.id, user.finance_account_id)
        
        # Determine selected event
        if event:
            selected_event = await get_finance_event_by_id(event)
        elif events:
            selected_event = events[0]  # Default to first event
        
        # Get transaction for editing
        if edit:
            async with async_session() as session:
                result = await session.execute(
                    select(Transaction).where(
                        Transaction.id == edit,
                        Transaction.account_id == user.finance_account_id
                    )
                )
                transaction = result.scalar_one_or_none()
                if transaction and transaction.event_id:
                    selected_event = await get_finance_event_by_id(transaction.event_id)

        # Get categories
        async with async_session() as session:
            result = await session.execute(
                select(Category)
                .where(Category.account_id == user.finance_account_id)
                .order_by(desc(Category.usage_count))
                .limit(20)
            )
            categories = result.scalars().all()
        
        # Get recent currencies
        recent = await get_recent_currencies(user.finance_account_id)
        for c in recent:
            if c not in currencies:
                currencies.append(c)

    return templates.TemplateResponse(
        "finance/add.html",
        {
            "request": request,
            "user": user,
            "transaction": transaction,
            "events": events,
            "selected_event": selected_event,
            "categories": categories,
            "currencies": currencies,
            "default_currency": selected_event.default_currency if selected_event else "KZT",
        }
    )


@router.post("/add")
async def add_transaction(
    request: Request,
    amount: str = Form(...),
    event_id: int = Form(...),
    category: str = Form(None),
    currency: str = Form("KZT"),
    description: str = Form(None),
    transaction_id: int = Form(None),
    custom_date: str = Form(None),
    custom_time: str = Form(None),
):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        return RedirectResponse(url="/login", status_code=302)

    try:
        amount_decimal = Decimal(amount.replace(",", ".").replace(" ", ""))
    except:
        return RedirectResponse(url=f"/finance/add?event={event_id}&error=invalid_amount", status_code=302)

    # Parse custom date if provided
    transaction_timestamp = utcnow()
    transaction_date = None
    if custom_date:
        try:
            time_str = custom_time or "12:00"
            dt_str = f"{custom_date} {time_str}"
            transaction_timestamp = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            transaction_date = datetime.strptime(custom_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    budget_alert = None
    
    # Convert to USD (using transaction date for historical rate if needed)
    amount_usd, exchange_rate = await convert_to_usd(amount_decimal, currency, for_date=transaction_date)

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
                transaction.event_id = event_id
                transaction.category = category or None
                transaction.currency = currency
                transaction.amount_usd = amount_usd
                transaction.exchange_rate = exchange_rate
                transaction.description = description or None
                transaction.timestamp = transaction_timestamp
                transaction.created_by_user_id = user.id
        else:
            transaction = Transaction(
                account_id=user.finance_account_id,
                event_id=event_id,
                amount=amount_decimal,
                currency=currency,
                amount_usd=amount_usd,
                exchange_rate=exchange_rate,
                category=category or None,
                description=description or None,
                timestamp=transaction_timestamp,
                source="manual",
                created_by_user_id=user.id,
            )
            session.add(transaction)

        # Update category usage or create new
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
                
                # Check budget
                budget_result = await session.execute(
                    select(CategoryBudget).where(
                        CategoryBudget.category_id == existing_cat.id,
                        or_(
                            CategoryBudget.event_id == event_id,
                            CategoryBudget.event_id == None
                        )
                    )
                )
                budget = budget_result.scalar_one_or_none()
                if budget:
                    # Calculate spent this month
                    now = datetime.now(tz=timezone.utc)
                    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    spent_result = await session.execute(
                        select(func.sum(func.abs(Transaction.amount)))
                        .where(
                            Transaction.account_id == user.finance_account_id,
                            Transaction.category == category,
                            Transaction.amount < 0,
                            Transaction.timestamp >= month_start,
                        )
                    )
                    spent = spent_result.scalar() or 0
                    spent_percent = (float(spent) / float(budget.monthly_limit)) * 100 if budget.monthly_limit else 0
                    
                    if spent_percent >= budget.alert_threshold:
                        budget_alert = {
                            "category": category,
                            "spent": float(spent),
                            "limit": float(budget.monthly_limit),
                            "percent": spent_percent,
                            "remaining": float(budget.monthly_limit) - float(spent),
                        }
            else:
                new_cat = Category(
                    account_id=user.finance_account_id,
                    name=category,
                    usage_count=1,
                )
                session.add(new_cat)

        await session.commit()

    if budget_alert:
        return RedirectResponse(
            url=f"/finance?budget_alert=1&cat={budget_alert['category']}&spent={budget_alert['spent']:.0f}&limit={budget_alert['limit']:.0f}&percent={budget_alert['percent']:.0f}",
            status_code=302
        )
    
    return RedirectResponse(url="/finance", status_code=302)


@router.get("/history")
async def history_page(
    request: Request,
    page: int = 1,
    event: int = None,
    category: str = None,
):
    """Transaction history with filters"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    transactions = []
    categories = []
    events = []
    selected_event = None
    has_more = False

    if user.finance_account_id:
        per_page = 50
        offset = (page - 1) * per_page
        
        events = await get_user_events(user.id, user.finance_account_id)
        if event:
            selected_event = await get_finance_event_by_id(event)

        async with async_session() as session:
            query = select(Transaction).where(Transaction.account_id == user.finance_account_id)
            if event:
                query = query.where(Transaction.event_id == event)
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
            "events": events,
            "selected_event": selected_event,
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
    """Finance settings - redirect to events list"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    
    return RedirectResponse(url="/finance/events", status_code=302)


@router.get("/events")
async def events_list_page(request: Request):
    """List all finance events"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    account = None
    events = []
    
    if user.finance_account_id:
        account = await get_finance_account_by_id(user.finance_account_id)
        events = await get_user_events(user.id, user.finance_account_id)

    return templates.TemplateResponse(
        "finance/events.html",
        {"request": request, "user": user, "account": account, "events": events}
    )


@router.get("/events/{event_id}/settings")
async def event_settings_page(request: Request, event_id: int):
    """Settings page for a specific event with webhook URL"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    event = await get_finance_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Get event members
    members = []
    async with async_session() as session:
        result = await session.execute(
            select(FinanceEventMember)
            .where(FinanceEventMember.event_id == event_id)
        )
        members = result.scalars().all()

    return templates.TemplateResponse(
        "finance/event_settings.html",
        {
            "request": request,
            "user": user,
            "event": event,
            "members": members,
            "currencies": BASE_CURRENCIES,
        }
    )


@router.post("/events/{event_id}/settings")
async def update_event_settings(
    request: Request,
    event_id: int,
    name: str = Form(...),
    default_currency: str = Form("KZT"),
):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.id == event_id)
        )
        event = result.scalar_one_or_none()
        if event:
            event.name = name
            event.default_currency = default_currency
            await session.commit()

    return RedirectResponse(url=f"/finance/events/{event_id}/settings", status_code=302)


@router.post("/events/{event_id}/regenerate-token")
async def regenerate_event_token(request: Request, event_id: int):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    await regenerate_event_webhook_token(event_id)
    return RedirectResponse(url=f"/finance/events/{event_id}/settings", status_code=302)


@router.post("/events/create")
async def create_event(
    request: Request,
    name: str = Form(...),
    event_type: str = Form("personal"),
    default_currency: str = Form("KZT"),
):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        event = FinanceEvent(
            account_id=user.finance_account_id,
            name=name,
            event_type=event_type,
            default_currency=default_currency,
            is_active=True,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)

    return RedirectResponse(url=f"/finance/events/{event.id}/settings", status_code=302)


@router.post("/events/{event_id}/archive")
async def archive_event(request: Request, event_id: int):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.id == event_id)
        )
        event = result.scalar_one_or_none()
        if event:
            event.is_archived = True
            await session.commit()

    return RedirectResponse(url="/finance/events", status_code=302)


@router.post("/events/{event_id}/delete")
async def delete_event(request: Request, event_id: int):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(FinanceEvent).where(FinanceEvent.id == event_id)
        )
        event = result.scalar_one_or_none()
        if event:
            await session.delete(event)
            await session.commit()

    return RedirectResponse(url="/finance/events", status_code=302)


# ============ API Endpoints ============

@router.get("/api/transactions")
async def api_get_transactions(
    request: Request,
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    event_id: int = Query(None),
    category: str = Query(None),
):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    async with async_session() as session:
        query = select(Transaction).where(Transaction.account_id == user.finance_account_id)
        if event_id:
            query = query.where(Transaction.event_id == event_id)
        if category:
            query = query.where(Transaction.category == category)
        query = query.order_by(desc(Transaction.timestamp)).offset(offset).limit(limit)

        result = await session.execute(query)
        transactions = result.scalars().all()

        return [
            {
                "id": t.id,
                "event_id": t.event_id,
                "amount": float(t.amount),
                "currency": t.currency,
                "amount_usd": float(t.amount_usd) if t.amount_usd else None,
                "category": t.category,
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
async def api_get_stats(
    request: Request,
    days: int = Query(30, le=365),
    event_id: int = Query(None),
):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    since = datetime.now(tz=timezone.utc) - timedelta(days=days)

    async with async_session() as session:
        base_filter = [
            Transaction.account_id == user.finance_account_id,
            Transaction.timestamp >= since,
        ]
        if event_id:
            base_filter.append(Transaction.event_id == event_id)
        
        # Use amount_usd for consistent currency, fallback to amount if null
        usd_amount = func.coalesce(Transaction.amount_usd, Transaction.amount)
        
        result = await session.execute(
            select(
                Transaction.category,
                func.sum(usd_amount).label("total"),
                func.count(Transaction.id).label("count"),
            )
            .where(and_(*base_filter))
            .group_by(Transaction.category)
        )
        rows = result.all()

        total_result = await session.execute(
            select(func.sum(usd_amount))
            .where(and_(*base_filter))
        )
        total = total_result.scalar() or 0
        
        # Get expenses and income separately (in USD)
        expenses_result = await session.execute(
            select(func.sum(usd_amount))
            .where(and_(*base_filter, Transaction.amount < 0))
        )
        expenses = expenses_result.scalar() or 0
        
        income_result = await session.execute(
            select(func.sum(usd_amount))
            .where(and_(*base_filter, Transaction.amount > 0))
        )
        income = income_result.scalar() or 0

        return {
            "period_days": days,
            "event_id": event_id,
            "currency": "USD",
            "total": float(total),
            "expenses": float(abs(expenses)),
            "income": float(income),
            "by_category": [
                {
                    "category": row.category or "Без категории",
                    "total": float(row.total),
                    "count": row.count,
                }
                for row in rows
            ],
        }


@router.get("/api/events")
async def api_get_events(request: Request):
    """Get all events for current user"""
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    events = await get_user_events(user.id, user.finance_account_id)
    return [
        {
            "id": e.id,
            "name": e.name,
            "event_type": e.event_type,
            "default_currency": e.default_currency,
            "is_active": e.is_active,
            "webhook_token": e.webhook_token,
        }
        for e in events
    ]


# ============ Tags & Budgets ============

@router.get("/categories")
async def categories_page(request: Request, search: str = None):
    """Category management with tags and budgets"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    tags = []
    untagged_categories = []
    all_categories = []
    
    if user.finance_account_id:
        async with async_session() as session:
            # Get all tags
            tags_result = await session.execute(
                select(CategoryTag)
                .where(CategoryTag.account_id == user.finance_account_id)
                .order_by(CategoryTag.display_order)
            )
            tags = tags_result.scalars().all()
            
            # Get all categories
            cat_query = select(Category).where(Category.account_id == user.finance_account_id)
            if search:
                cat_query = cat_query.where(Category.name.ilike(f"%{search}%"))
            cat_query = cat_query.order_by(desc(Category.usage_count))
            
            cat_result = await session.execute(cat_query)
            all_categories = cat_result.scalars().all()
            
            # Separate untagged
            untagged_categories = [c for c in all_categories if c.tag_id is None]

    return templates.TemplateResponse(
        "finance/categories.html",
        {
            "request": request,
            "user": user,
            "tags": tags,
            "untagged_categories": untagged_categories,
            "all_categories": all_categories,
            "search": search or "",
        }
    )


@router.post("/tags/create")
async def create_tag(
    request: Request,
    name: str = Form(...),
    color: str = Form("#4361ee"),
):
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        tag = CategoryTag(
            account_id=user.finance_account_id,
            name=name,
            color=color,
        )
        session.add(tag)
        await session.commit()

    return RedirectResponse(url="/finance/categories", status_code=302)


@router.post("/tags/{tag_id}/delete")
async def delete_tag(request: Request, tag_id: int):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        # Unlink categories first
        await session.execute(
            select(Category).where(Category.tag_id == tag_id)
        )
        
        result = await session.execute(
            select(CategoryTag).where(CategoryTag.id == tag_id)
        )
        tag = result.scalar_one_or_none()
        if tag:
            # Set categories' tag_id to None
            cat_result = await session.execute(
                select(Category).where(Category.tag_id == tag_id)
            )
            for cat in cat_result.scalars().all():
                cat.tag_id = None
            
            await session.delete(tag)
            await session.commit()

    return RedirectResponse(url="/finance/categories", status_code=302)


@router.post("/categories/{category_id}/set-tag")
async def set_category_tag(
    request: Request,
    category_id: int,
    tag_id: int = Form(None),
):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(Category).where(
                Category.id == category_id,
                Category.account_id == user.finance_account_id,
            )
        )
        category = result.scalar_one_or_none()
        if category:
            category.tag_id = tag_id if tag_id else None
            await session.commit()

    return RedirectResponse(url="/finance/categories", status_code=302)


@router.get("/budgets")
async def budgets_page(request: Request):
    """Budget management page"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    budgets = []
    categories = []
    events = []
    
    if user.finance_account_id:
        events = await get_user_events(user.id, user.finance_account_id)
        
        async with async_session() as session:
            # Get all budgets with category info
            budget_result = await session.execute(
                select(CategoryBudget, Category)
                .join(Category, CategoryBudget.category_id == Category.id)
                .where(Category.account_id == user.finance_account_id)
            )
            budgets = budget_result.all()
            
            # Get categories without budgets
            cat_result = await session.execute(
                select(Category)
                .where(Category.account_id == user.finance_account_id)
                .order_by(desc(Category.usage_count))
            )
            categories = cat_result.scalars().all()

    return templates.TemplateResponse(
        "finance/budgets.html",
        {
            "request": request,
            "user": user,
            "budgets": budgets,
            "categories": categories,
            "events": events,
        }
    )


@router.post("/budgets/create")
async def create_budget(
    request: Request,
    category_id: int = Form(...),
    monthly_limit: str = Form(...),
    event_id: int = Form(None),
    alert_threshold: int = Form(70),
):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        limit_decimal = Decimal(monthly_limit.replace(",", ".").replace(" ", ""))
    except:
        return RedirectResponse(url="/finance/budgets?error=invalid_amount", status_code=302)

    async with async_session() as session:
        # Check if budget exists for this category/event
        existing = await session.execute(
            select(CategoryBudget).where(
                CategoryBudget.category_id == category_id,
                CategoryBudget.event_id == event_id,
            )
        )
        budget = existing.scalar_one_or_none()
        
        if budget:
            budget.monthly_limit = limit_decimal
            budget.alert_threshold = alert_threshold
        else:
            budget = CategoryBudget(
                category_id=category_id,
                event_id=event_id if event_id else None,
                monthly_limit=limit_decimal,
                alert_threshold=alert_threshold,
            )
            session.add(budget)
        
        await session.commit()

    return RedirectResponse(url="/finance/budgets", status_code=302)


@router.post("/budgets/{budget_id}/delete")
async def delete_budget(request: Request, budget_id: int):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(CategoryBudget).where(CategoryBudget.id == budget_id)
        )
        budget = result.scalar_one_or_none()
        if budget:
            await session.delete(budget)
            await session.commit()

    return RedirectResponse(url="/finance/budgets", status_code=302)


# ============ Recurring Transactions ============

FREQUENCY_LABELS = {
    "daily": "Ежедневно",
    "weekly": "Еженедельно",
    "monthly": "Ежемесячно",
    "every_n_days": "Каждые N дней",
}


@router.get("/recurring")
async def recurring_page(request: Request):
    """Recurring transactions management"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    recurring = []
    events = []
    categories = []
    
    if user.finance_account_id:
        events = await get_user_events(user.id, user.finance_account_id)
        
        async with async_session() as session:
            # Get all recurring transactions for user's events
            event_ids = [e.id for e in events]
            if event_ids:
                result = await session.execute(
                    select(RecurringTransaction, FinanceEvent, Category)
                    .join(FinanceEvent, RecurringTransaction.event_id == FinanceEvent.id)
                    .outerjoin(Category, RecurringTransaction.category_id == Category.id)
                    .where(RecurringTransaction.event_id.in_(event_ids))
                    .order_by(RecurringTransaction.next_run)
                )
                recurring = result.all()
            
            # Get categories
            cat_result = await session.execute(
                select(Category)
                .where(Category.account_id == user.finance_account_id)
                .order_by(desc(Category.usage_count))
            )
            categories = cat_result.scalars().all()

    return templates.TemplateResponse(
        "finance/recurring.html",
        {
            "request": request,
            "user": user,
            "recurring": recurring,
            "events": events,
            "categories": categories,
            "currencies": BASE_CURRENCIES,
            "frequency_labels": FREQUENCY_LABELS,
        }
    )


@router.post("/recurring/create")
async def create_recurring(
    request: Request,
    event_id: int = Form(...),
    amount: str = Form(...),
    currency: str = Form("KZT"),
    category_id: int = Form(None),
    description: str = Form(None),
    frequency: str = Form(...),
    frequency_value: int = Form(1),
):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        amount_decimal = Decimal(amount.replace(",", ".").replace(" ", ""))
    except:
        return RedirectResponse(url="/finance/recurring?error=invalid_amount", status_code=302)

    # Calculate next run date
    now = datetime.now(tz=timezone.utc)
    if frequency == "daily":
        next_run = now + timedelta(days=1)
    elif frequency == "weekly":
        next_run = now + timedelta(weeks=1)
    elif frequency == "monthly":
        next_run = now + timedelta(days=30)
    else:  # every_n_days
        next_run = now + timedelta(days=frequency_value)

    async with async_session() as session:
        recurring = RecurringTransaction(
            event_id=event_id,
            category_id=category_id if category_id else None,
            amount=amount_decimal,
            currency=currency,
            description=description,
            frequency=frequency,
            frequency_value=frequency_value,
            next_run=next_run,
            is_active=True,
        )
        session.add(recurring)
        await session.commit()

    return RedirectResponse(url="/finance/recurring", status_code=302)


@router.post("/recurring/{recurring_id}/toggle")
async def toggle_recurring(request: Request, recurring_id: int):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(RecurringTransaction).where(RecurringTransaction.id == recurring_id)
        )
        recurring = result.scalar_one_or_none()
        if recurring:
            recurring.is_active = not recurring.is_active
            await session.commit()

    return RedirectResponse(url="/finance/recurring", status_code=302)


@router.post("/recurring/{recurring_id}/delete")
async def delete_recurring(request: Request, recurring_id: int):
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(RecurringTransaction).where(RecurringTransaction.id == recurring_id)
        )
        recurring = result.scalar_one_or_none()
        if recurring:
            await session.delete(recurring)
            await session.commit()

    return RedirectResponse(url="/finance/recurring", status_code=302)


# ============ Analytics ============

@router.get("/analytics")
async def analytics_page(
    request: Request,
    event_id: int = None,
    days: int = 30,
):
    """Analytics page with charts"""
    user = await require_finance_access(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    events = []
    selected_event = None
    
    if user.finance_account_id:
        events = await get_user_events(user.id, user.finance_account_id)
        if event_id:
            selected_event = await get_finance_event_by_id(event_id)

    return templates.TemplateResponse(
        "finance/analytics.html",
        {
            "request": request,
            "user": user,
            "events": events,
            "selected_event": selected_event,
            "days": days,
        }
    )


@router.get("/api/analytics/daily")
async def api_analytics_daily(
    request: Request,
    days: int = Query(30, le=365),
    event_id: int = Query(None),
):
    """Get daily aggregated transactions for charts"""
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    since = datetime.now(tz=timezone.utc) - timedelta(days=days)

    async with async_session() as session:
        base_filter = [
            Transaction.account_id == user.finance_account_id,
            Transaction.timestamp >= since,
        ]
        if event_id:
            base_filter.append(Transaction.event_id == event_id)
        
        # Use amount_usd for consistent currency, fallback to amount if null
        usd_amount = func.coalesce(Transaction.amount_usd, Transaction.amount)
        
        # Daily aggregation (in USD)
        result = await session.execute(
            select(
                func.date(Transaction.timestamp).label("day"),
                func.sum(func.case((Transaction.amount < 0, usd_amount), else_=0)).label("expenses"),
                func.sum(func.case((Transaction.amount > 0, usd_amount), else_=0)).label("income"),
            )
            .where(and_(*base_filter))
            .group_by(func.date(Transaction.timestamp))
            .order_by(func.date(Transaction.timestamp))
        )
        rows = result.all()

        return [
            {
                "date": str(row.day),
                "expenses": float(abs(row.expenses or 0)),
                "income": float(row.income or 0),
            }
            for row in rows
        ]


@router.get("/api/analytics/by-category")
async def api_analytics_by_category(
    request: Request,
    days: int = Query(30, le=365),
    event_id: int = Query(None),
):
    """Get category breakdown for pie chart"""
    user = await require_finance_access(request)
    if not user or not user.finance_account_id:
        raise HTTPException(status_code=401)

    since = datetime.now(tz=timezone.utc) - timedelta(days=days)

    async with async_session() as session:
        base_filter = [
            Transaction.account_id == user.finance_account_id,
            Transaction.timestamp >= since,
            Transaction.amount < 0,  # Only expenses
        ]
        if event_id:
            base_filter.append(Transaction.event_id == event_id)
        
        # Use amount_usd for consistent currency, fallback to amount if null
        usd_amount = func.coalesce(Transaction.amount_usd, Transaction.amount)
        
        result = await session.execute(
            select(
                Transaction.category,
                func.sum(func.abs(usd_amount)).label("total"),
                func.count(Transaction.id).label("count"),
            )
            .where(and_(*base_filter))
            .group_by(Transaction.category)
            .order_by(desc(func.sum(func.abs(usd_amount))))
        )
        rows = result.all()
        
        total = sum(float(row.total or 0) for row in rows)

        return {
            "currency": "USD",
            "total": total,
            "categories": [
                {
                    "category": row.category or "Без категории",
                    "total": float(row.total or 0),
                    "count": row.count,
                    "percent": round((float(row.total or 0) / total * 100) if total > 0 else 0, 1),
                }
                for row in rows
            ],
        }
