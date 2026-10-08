"""Object-level permissions shared by every Finance endpoint."""
from fastapi import HTTPException, Request
from sqlalchemy import and_, or_, select

from app.auth import get_current_user, has_service_access
from app.database import async_session, Category, CategoryBudget, CategoryTag, FinanceEvent, FinanceEventMember, RecurringTransaction, Transaction


async def event_for_user(user, event_id: int, *, manage=False):
    async with async_session() as session:
        event = await session.get(FinanceEvent, event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        if event.account_id == user.finance_account_id:
            return event
        role = await session.scalar(select(FinanceEventMember.role).where(FinanceEventMember.event_id == event_id, FinanceEventMember.user_id == user.id))
        if role and (not manage or role == "owner"):
            return event
    raise HTTPException(404, "Event not found")


async def transaction_scope(user):
    async with async_session() as session:
        owned = select(FinanceEvent.id).where(FinanceEvent.account_id == user.finance_account_id) if user.finance_account_id else select(FinanceEvent.id).where(False)
        member = select(FinanceEventMember.event_id).where(FinanceEventMember.user_id == user.id)
        ids = list((await session.scalars(owned.union(member))).all())
    return or_(Transaction.event_id.in_(ids), and_(Transaction.event_id.is_(None), Transaction.account_id == user.finance_account_id))


async def authorize_finance_request(request: Request):
    if request.url.path.startswith("/finance/api/webhook/"):
        return
    user = await get_current_user(request)
    if not user or not await has_service_access(user.id, "finance"):
        return  # Existing handlers choose a login redirect or API 401.
    values = dict(request.query_params)
    if request.method == "POST" and request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        values.update(await request.form())
    values.update(request.path_params)
    manage = "/events/" in request.url.path and request.url.path.endswith(("/settings", "/regenerate-token", "/archive", "/delete"))
    for name in ("event", "event_id"):
        if values.get(name):
            try:
                event_id = int(values[name])
            except (TypeError, ValueError):
                raise HTTPException(422, "Invalid event id")
            await event_for_user(user, event_id, manage=manage)
    mapping = {"category_id": Category, "tag_id": CategoryTag, "budget_id": CategoryBudget, "recurring_id": RecurringTransaction, "transaction_id": Transaction, "edit": Transaction}
    # Category pages use ?edit for their own UI only; currently edit belongs to /add.
    if request.url.path != "/finance/add":
        mapping.pop("edit")
    async with async_session() as session:
        for name, model in mapping.items():
            if not values.get(name):
                continue
            try:
                object_id = int(values[name])
            except (TypeError, ValueError):
                raise HTTPException(422, "Invalid object id")
            if not object_id:
                continue
            obj = await session.get(model, object_id)
            if obj is None:
                raise HTTPException(404, "Object not found")
            if model in (Category, CategoryTag):
                allowed = obj.account_id == user.finance_account_id
            elif model is CategoryBudget:
                category = await session.get(Category, obj.category_id)
                allowed = category is not None and category.account_id == user.finance_account_id
            elif obj.event_id:
                await event_for_user(user, obj.event_id)
                allowed = True
            else:
                allowed = obj.account_id == user.finance_account_id
            if not allowed:
                raise HTTPException(404, "Object not found")

