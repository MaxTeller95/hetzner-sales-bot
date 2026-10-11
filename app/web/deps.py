from __future__ import annotations

import secrets
from pathlib import Path
from typing import AsyncIterator

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.notifier import TelegramNotifier, create_bot

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = lambda v: f"{int(v or 0):,}"
templates.env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else "—"


class LoginRequired(Exception):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def require_admin(request: Request) -> str:
    admin = request.session.get("admin")
    if not admin or admin != get_settings().admin_username:
        raise LoginRequired()
    return admin


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    form = await request.form()
    expected = request.session.get("csrf")
    if not expected or not secrets.compare_digest(str(form.get("csrf", "")), expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token, reload the page and try again.")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def render(request: Request, template: str, **ctx):
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request,
        template,
        {"csrf": csrf_token(request), "flashes": messages, "admin": request.session.get("admin"), **ctx},
    )


def back(request: Request, fallback: str = "/") -> RedirectResponse:
    target = request.headers.get("referer") or fallback
    return RedirectResponse(target, status_code=303)


_notifier: TelegramNotifier | None = None


def notifier() -> TelegramNotifier | None:
    global _notifier
    if not get_settings().bot_token:
        return None
    if _notifier is None:
        _notifier = TelegramNotifier(create_bot())
    return _notifier


async def close_notifier() -> None:
    if _notifier is not None:
        await _notifier.bot.session.close()
