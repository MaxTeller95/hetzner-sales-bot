from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.gate import channel_link, channel_ok, send_tos
from app.db.session import SessionLocal
from app.services import wallet as wallet_svc

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """One DB session per update; committed when the handler succeeds."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with SessionLocal() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler: Handler, event: Update, data: dict[str, Any]) -> Any:
        tg = data.get("event_from_user")
        if tg is None or tg.is_bot:
            return await handler(event, data)
        user = await wallet_svc.get_or_create_user(data["session"], tg.id, tg.username, tg.first_name)
        if user.is_banned:
            if event.callback_query:
                await event.callback_query.answer(T.BANNED, show_alert=True)
            elif event.message:
                await event.message.answer(T.BANNED)
            return None
        data["user"] = user
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, interval: float = 0.6):
        self.interval = interval
        self._last: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            if now - self._last.get(user.id, 0) < self.interval:
                if isinstance(event, CallbackQuery):
                    await event.answer(T.SLOW_DOWN)
                return None
            self._last[user.id] = now
            if len(self._last) > 50_000:
                self._last.clear()
        return await handler(event, data)


GATE_FREE_CALLBACKS = ("tos:", "gate:", "ta:")


class GateMiddleware(BaseMiddleware):
    """Blocks everything until ToS is accepted and the required channel is joined."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("user")
        if user is None:
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and (event.data or "").startswith(GATE_FREE_CALLBACKS):
            return await handler(event, data)

        bot = data["bot"]
        session = data["session"]
        chat_id = event.chat.id if isinstance(event, Message) else event.from_user.id
        if user.tos_accepted_at is None:
            if isinstance(event, CallbackQuery):
                await event.answer()
            await send_tos(bot, chat_id, session, user)
            return None
        ok, s = await channel_ok(bot, session, user)
        if not ok:
            if isinstance(event, CallbackQuery):
                await event.answer(T.NOT_JOINED, show_alert=True)
            await bot.send_message(chat_id, T.join_channel(s.required_channel), reply_markup=K.join_channel(channel_link(s)))
            return None
        return await handler(event, data)
