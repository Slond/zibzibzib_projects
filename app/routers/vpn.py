"""VPN tile: AmneziaWG keys and traffic, inside the main app."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from urllib.parse import quote

import segno
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, has_service_access
from app.database import User
from app.services.awg.manage import PanelError, client_link, dashboard, issue_client, revoke_client
from app.services.awg.runtime import awg_dir, awg_socket, open_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/vpn")
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


async def gate(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.must_change_password:
        return RedirectResponse(url="/change-password", status_code=302)
    if not user.is_admin:
        raise HTTPException(403, "VPN administration requires administrator access")
    if not await has_service_access(user.id, "vpn"):
        return RedirectResponse(url="/", status_code=302)
    return user


def _snap(error: str = "") -> dict:
    store = open_store()
    try:
        snap = dashboard(store, awg_dir(), awg_socket())
    finally:
        store.close()
    snap["error"] = error
    return snap


@router.get("")
@router.get("/")
async def vpn_index(request: Request):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    return templates.TemplateResponse(
        request=request,
        name="vpn/index.html",
        context={"user": user, "snap": _snap()},
    )


@router.post("/clients")
async def vpn_create(request: Request, name: str = Form(...)):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    store = open_store()
    try:
        try:
            row = issue_client(store, awg_dir(), awg_socket(), name)
        except PanelError as exc:
            return templates.TemplateResponse(
                request=request,
                name="vpn/index.html",
                context={"user": user, "snap": _snap(str(exc))},
                status_code=400,
            )
    finally:
        store.close()
    return RedirectResponse(url=f"/vpn/clients/{row.id}?new=1", status_code=302)


@router.get("/clients/{client_id}")
async def vpn_client(request: Request, client_id: int, new: int = 0):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    store = open_store()
    try:
        row = store.get_client(client_id)
        snap = dashboard(store, awg_dir(), awg_socket())
    finally:
        store.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    link = ""
    if row.conf.strip():
        try:
            link = client_link(row.conf, row.name)
        except ValueError as exc:
            logger.warning("vpn uri: %s", exc)
    stats = next((item for item in [*snap["clients"], *snap["revoked"]] if item["id"] == row.id), None)
    return templates.TemplateResponse(
        request=request,
        name="vpn/client.html",
        context={"user": user, "client": row, "link": link, "fresh": bool(new), "stats": stats},
    )


@router.get("/clients/{client_id}/conf")
async def vpn_conf(request: Request, client_id: int):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    store = open_store()
    try:
        row = store.get_client(client_id)
    finally:
        store.close()
    if row is None or not row.conf.strip():
        raise HTTPException(status_code=404, detail="Конфиг не сохранён")
    star = quote(f"{row.name}.conf", safe="")
    disposition = f"attachment; filename=\"awg-{row.id}.conf\"; filename*=UTF-8''{star}"
    return Response(
        content=row.conf,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@router.get("/clients/{client_id}/qr.png")
async def vpn_qr(request: Request, client_id: int):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    store = open_store()
    try:
        row = store.get_client(client_id)
    finally:
        store.close()
    if row is None or not row.conf.strip():
        raise HTTPException(status_code=404, detail="Нет ключа")
    # PNG, same as the working Amnezia panels. svg_inline() omits xmlns, so a
    # browser will not paint it when the file is loaded through <img>.
    buf = io.BytesIO()
    segno.make(client_link(row.conf, row.name), error="l").save(
        buf, kind="png", scale=6, dark="#000", light="#fff"
    )
    return Response(content=buf.getvalue(), media_type="image/png")


@router.post("/clients/{client_id}/revoke")
async def vpn_revoke(request: Request, client_id: int):
    user = await gate(request)
    if not isinstance(user, User):
        return user
    store = open_store()
    try:
        try:
            revoke_client(store, awg_dir(), awg_socket(), client_id)
        except PanelError as exc:
            return templates.TemplateResponse(
                request=request,
                name="vpn/index.html",
                context={"user": user, "snap": _snap(str(exc))},
                status_code=400,
            )
    finally:
        store.close()
    return RedirectResponse(url="/vpn", status_code=302)
