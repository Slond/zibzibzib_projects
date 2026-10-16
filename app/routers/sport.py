"""Per-account sports workspace and a separate nutrition placeholder."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text, update
from sqlalchemy.dialects.sqlite import insert

from app.auth import get_current_user, has_service_access
from app.database import SportWorkspace, async_session, utcnow
from app.services.sport import SportUpdate, empty_workspace, today

router = APIRouter(tags=["sport"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


async def require_user(request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    if not await has_service_access(user.id, "sport"):
        raise HTTPException(403, "Нет доступа к разделу спорта")
    return user


async def ensure_workspace(session, user_id):
    await session.execute(insert(SportWorkspace).values(
        user_id=user_id, revision=0, document=json.dumps(empty_workspace()), updated_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=["user_id"]))


async def snapshot(user_id):
    async with async_session() as session:
        await ensure_workspace(session, user_id)
        row = await session.get(SportWorkspace, user_id)
        result = {"state": json.loads(row.document), "revision": row.revision,
                  "today": today(), "timezone": "Asia/Almaty"}
        await session.commit()
        return result


@router.get("/sport")
@router.get("/sport/")
async def index(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not await has_service_access(user.id, "sport"):
        return RedirectResponse("/", status_code=302)
    boot = await snapshot(user.id)
    boot["user"] = {"id": user.id, "name": user.name or user.email}
    return templates.TemplateResponse(request=request, name="sport/index.html", context={"boot": boot})


@router.get("/sport/api/state")
async def get_state(request: Request):
    return await snapshot((await require_user(request)).id)


@router.put("/sport/api/state")
async def put_state(request: Request):
    user = await require_user(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4 * 1024 * 1024:
            raise HTTPException(413, "Журнал слишком большой. Скачайте экспорт перед архивированием.")
    try:
        payload = SportUpdate.model_validate_json(body)
    except ValueError:
        raise HTTPException(422, "Проверьте даты, подходы, вес и статус тренировки") from None
    async with async_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        await ensure_workspace(session, user.id)
        result = await session.execute(update(SportWorkspace).where(
            SportWorkspace.user_id == user.id, SportWorkspace.revision == payload.revision,
        ).values(document=payload.state.model_dump_json(exclude_none=True),
                 revision=payload.revision + 1, updated_at=utcnow()))
        if result.rowcount != 1:
            raise HTTPException(409, "Журнал изменён в другой вкладке. Скачайте черновик и обновите страницу.")
        await session.commit()
    return {"revision": payload.revision + 1}


@router.get("/nutrition")
@router.get("/nutrition/")
async def nutrition(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not await has_service_access(user.id, "nutrition"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="sport/nutrition.html", context={"user": user})
