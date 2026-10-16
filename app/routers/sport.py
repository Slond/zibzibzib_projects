"""Per-account sports workspace and a separate nutrition placeholder."""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from time import monotonic

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text, update
from sqlalchemy.dialects.sqlite import insert

from app.auth import get_current_user, has_service_access
from app.database import SportWorkspace, async_session, utcnow
from app.services.sport import SportState, SportUpdate, empty_workspace, refresh_future_workouts, today, upgrade_workspace
from app.services.sport_lookup import ExerciseImport, import_exercise
from app.services.sport_catalog import SHARED_EXERCISES

router = APIRouter(tags=["sport"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
lookup_limits = OrderedDict()


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
        result = {"state": upgrade_workspace(json.loads(row.document)), "revision": row.revision,
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
    boot["catalog"] = SHARED_EXERCISES
    boot["user"] = {"id": user.id, "name": user.name or user.email}
    return templates.TemplateResponse(request=request, name="sport/index.html", context={"boot": boot})


@router.get("/sport/api/state")
async def get_state(request: Request):
    return await snapshot((await require_user(request)).id)


@router.post("/sport/api/exercises/import")
async def import_exercise_card(request: Request):
    user = await require_user(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            raise HTTPException(413, "Слишком длинное название или ссылка")
    try:
        payload = ExerciseImport.model_validate_json(body)
    except ValueError:
        raise HTTPException(422, "Введите название и ссылку на упражнение") from None
    now = monotonic()
    calls = [at for at in lookup_limits.get(user.id, []) if now - at < 60]
    if len(calls) >= 20:
        raise HTTPException(429, "Слишком много загрузок. Подождите минуту.")
    lookup_limits[user.id] = [*calls, now]
    lookup_limits.move_to_end(user.id)
    while len(lookup_limits) > 1000:
        lookup_limits.popitem(last=False)
    try:
        return {"exercise": await import_exercise(payload.name, payload.url)}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except httpx.HTTPError:
        raise HTTPException(502, "Источник временно недоступен. Ссылка сохранится; заполните карточку вручную.") from None


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
        row = await session.get(SportWorkspace, user.id)
        if row.revision != payload.revision:
            raise HTTPException(409, "Журнал изменён в другой вкладке. Скачайте черновик и обновите страницу.")
        document = refresh_future_workouts(upgrade_workspace(json.loads(row.document)),
                                          upgrade_workspace(payload.state.model_dump(exclude_none=True)))
        SportState.model_validate(document)
        result = await session.execute(update(SportWorkspace).where(
            SportWorkspace.user_id == user.id, SportWorkspace.revision == payload.revision,
        ).values(document=json.dumps(document, ensure_ascii=False),
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
