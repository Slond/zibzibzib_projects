"""Authenticated job search, with original files and explicitly enabled sharing."""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.sqlite import insert

from app.auth import get_current_user, has_service_access
from app.database import JobSearchFile, JobSearchWorkspace, async_session, utcnow
from app.services.job_search import FileCommentUpdate, WorkspaceUpdate, empty_workspace

router = APIRouter(prefix="/jobs", tags=["jobs"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
ZONE = ZoneInfo("Asia/Almaty")
MAX_FILE = 10 * 1024 * 1024
MAX_STORAGE = 100 * 1024 * 1024
MEDIA = {".pdf": "application/pdf", ".txt": "text/plain", ".doc": "application/msword",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


async def require_user(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    if not await has_service_access(user.id, "jobs"):
        raise HTTPException(403, "Нет доступа к разделу поиска работы")
    return user


def file_view(row):
    return {"id": row.id, "name": row.name, "size": row.size, "type": row.media_type,
            "date": row.created_at.date().isoformat(), "primary": row.is_primary,
            "label": "Личный документ", "shared": bool(row.share_token),
            "comment": row.comment,
            "share_url": f"/jobs/shared/{row.share_token}" if row.share_token else ""}


async def files_view(session, user_id):
    rows = (await session.execute(select(
        JobSearchFile.id, JobSearchFile.name, JobSearchFile.size, JobSearchFile.media_type,
        JobSearchFile.created_at, JobSearchFile.is_primary, JobSearchFile.share_token,
        JobSearchFile.comment,
    ).where(JobSearchFile.user_id == user_id).order_by(JobSearchFile.created_at))).all()
    return [file_view(row) for row in rows]


async def ensure_workspace(session, user_id):
    await session.execute(insert(JobSearchWorkspace).values(
        user_id=user_id, revision=0, document=json.dumps(empty_workspace()), updated_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=["user_id"]))


async def snapshot(user_id):
    async with async_session() as session:
        await ensure_workspace(session, user_id)
        row = await session.get(JobSearchWorkspace, user_id)
        state = json.loads(row.document)
        state["files"] = await files_view(session, user_id)
        result = {"state": state, "revision": row.revision,
                  "today": datetime.now(ZONE).date().isoformat(), "timezone": "Asia/Almaty"}
        await session.commit()
        return result


@router.get("")
@router.get("/")
async def index(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not await has_service_access(user.id, "jobs"):
        return RedirectResponse("/", status_code=302)
    boot = await snapshot(user.id)
    boot["user"] = {"id": user.id, "name": user.name or user.email}
    return templates.TemplateResponse(request=request, name="jobs/index.html", context={"boot": boot})


@router.get("/api/state")
async def get_state(request: Request):
    return await snapshot((await require_user(request)).id)


@router.put("/api/state")
async def save_state(request: Request):
    user = await require_user(request)
    # Bound the streamed body before parsing JSON; large/malformed writes never reach SQLite.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(413, "Слишком много данных. Экспортируйте и архивируйте старые отклики.")
    try:
        payload = WorkspaceUpdate.model_validate_json(body)
    except ValueError:
        raise HTTPException(422, "Проверьте поля, даты и связи откликов") from None
    document = payload.state.model_dump_json(exclude_none=True)
    async with async_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        await ensure_workspace(session, user.id)
        owned = set((await session.scalars(select(JobSearchFile.id).where(JobSearchFile.user_id == user.id))).all())
        if any(j.cv and j.cv not in owned for j in payload.state.jobs):
            raise HTTPException(422, "Выбранный документ недоступен. Обновите страницу.")
        result = await session.execute(update(JobSearchWorkspace).where(
            JobSearchWorkspace.user_id == user.id, JobSearchWorkspace.revision == payload.revision,
        ).values(document=document, revision=payload.revision + 1, updated_at=utcnow()))
        if result.rowcount != 1:
            raise HTTPException(409, "Данные изменены в другой вкладке. Сохраните черновик и обновите страницу.")
        await session.commit()
    return {"revision": payload.revision + 1}


@router.post("/api/files", status_code=201)
async def upload_file(request: Request, name: str):
    user = await require_user(request)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    suffix = Path(name).suffix.lower()
    if not name or len(name) > 255 or suffix not in MEDIA:
        raise HTTPException(422, "Поддерживаются PDF, DOC, DOCX, TXT, PNG и JPG")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_FILE:
            raise HTTPException(413, "Файл больше 10 МБ")
    if not content:
        raise HTTPException(422, "Файл пуст")
    async with async_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        count, size = (await session.execute(select(func.count(), func.coalesce(func.sum(JobSearchFile.size), 0)).where(JobSearchFile.user_id == user.id))).one()
        if count >= 100 or size + len(content) > MAX_STORAGE:
            raise HTTPException(413, "Лимит библиотеки: 100 файлов и 100 МБ")
        row = JobSearchFile(id=str(uuid4()), user_id=user.id, name=name, size=len(content),
                            content=bytes(content), media_type=MEDIA[suffix], is_primary=count == 0)
        session.add(row)
        await session.flush()
        result = file_view(row)
        await session.commit()
        return result


def download(row):
    return Response(row.content, media_type=row.media_type, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.name, safe='')}",
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'", "Referrer-Policy": "no-referrer",
    })


async def owned_file(session, file_id, user_id):
    row = await session.get(JobSearchFile, file_id)
    if not row or row.user_id != user_id:
        raise HTTPException(404, "Документ не найден")
    return row


@router.get("/api/files/{file_id}/download")
async def download_file(request: Request, file_id: str):
    user = await require_user(request)
    async with async_session() as session:
        return download(await owned_file(session, file_id, user.id))


@router.post("/api/files/{file_id}/primary")
async def make_primary(request: Request, file_id: str):
    user = await require_user(request)
    async with async_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        row = await owned_file(session, file_id, user.id)
        await session.execute(update(JobSearchFile).where(JobSearchFile.user_id == user.id).values(is_primary=False))
        row.is_primary = True
        await session.commit()
        return {"id": file_id}


@router.patch("/api/files/{file_id}/comment")
async def update_file_comment(request: Request, file_id: str):
    user = await require_user(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 40 * 1024:
            raise HTTPException(413, "Комментарий слишком большой")
    try:
        payload = FileCommentUpdate.model_validate_json(body)
    except ValueError:
        raise HTTPException(422, "Комментарий — текст до 6000 символов") from None
    async with async_session() as session:
        row = await owned_file(session, file_id, user.id)
        row.comment = payload.comment.strip()
        result = file_view(row)
        await session.commit()
    return result


@router.post("/api/files/{file_id}/share")
async def share_file(request: Request, file_id: str):
    user = await require_user(request)
    async with async_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        row = await owned_file(session, file_id, user.id)
        if not row.share_token:
            row.share_token = secrets.token_urlsafe(32)
        result = file_view(row)
        await session.commit()
        return result


@router.delete("/api/files/{file_id}/share")
async def unshare_file(request: Request, file_id: str):
    user = await require_user(request)
    async with async_session() as session:
        row = await owned_file(session, file_id, user.id)
        row.share_token = None
        await session.commit()
    return {"shared": False}


@router.delete("/api/files/{file_id}")
async def delete_file(request: Request, file_id: str):
    user = await require_user(request)
    async with async_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        row = await owned_file(session, file_id, user.id)
        await ensure_workspace(session, user.id)
        workspace = await session.get(JobSearchWorkspace, user.id)
        data = json.loads(workspace.document)
        for job in data["jobs"]:
            if job.get("cv") == file_id:
                job["cv"] = ""
        workspace.document = json.dumps(data, ensure_ascii=False)
        workspace.revision += 1
        workspace.updated_at = utcnow()
        await session.delete(row)
        await session.flush()
        if row.is_primary:
            replacement = await session.scalar(select(JobSearchFile).where(JobSearchFile.user_id == user.id).order_by(JobSearchFile.created_at))
            if replacement:
                replacement.is_primary = True
        await session.commit()
        return {"revision": workspace.revision}


@router.get("/shared/{token}")
async def shared_file(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(404, "Документ недоступен")
    async with async_session() as session:
        row = await session.scalar(select(JobSearchFile).where(JobSearchFile.share_token == token))
        if not row or not await has_service_access(row.user_id, "jobs"):
            raise HTTPException(404, "Документ недоступен")
        return download(row)
