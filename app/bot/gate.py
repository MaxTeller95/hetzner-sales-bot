"""First-start Terms of Service popup and forced channel membership."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as K
from app.bot import texts as T
from app.db.models import User
from app.services.runtime_settings import RuntimeSettings, load_settings
from app.utils import utcnow

log = logging.getLogger(__name__)

_background: set[asyncio.Task] = set()
_member_ok: dict[tuple[str, int], float] = {}
MEMBER_CACHE_SECONDS = 300


async def send_tos(bot: Bot, chat_id: int, session: AsyncSession, user: User) -> None:
    """Send tutorial + ToS with a button that unlocks after `tos_lock_seconds`."""
    s = await load_settings(session)
    user.tos_shown_at = utcnow()
    await session.commit()
    seconds = max(0, s.tos_lock_seconds)
    msg = await bot.send_message(
        chat_id,
        T.tos_message(s.tutorial_text, s.tos_text),
        reply_markup=K.tos_wait(seconds) if seconds else K.tos_agree(),
    )
    if seconds:
        task = asyncio.create_task(_countdown(bot, chat_id, msg.message_id, seconds))
        _background.add(task)
        task.add_done_callback(_background.discard)


async def _countdown(bot: Bot, chat_id: int, message_id: int, seconds: int) -> None:
    remaining = seconds
    while remaining > 0:
        step = min(2, remaining)
        await asyncio.sleep(step)
        remaining -= step
        markup = K.tos_wait(remaining) if remaining > 0 else K.tos_agree()
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramAPIError:
            pass


def tos_unlocked(user: User, s: RuntimeSettings) -> bool:
    if user.tos_shown_at is None:
        return False
    return (utcnow() - user.tos_shown_at).total_seconds() >= s.tos_lock_seconds


def _chat_ref(channel: str) -> str | int:
    channel = channel.strip()
    return int(channel) if channel.lstrip("-").isdigit() else channel


async def is_member(bot: Bot, channel: str, user_id: int) -> bool:
    key = (channel, user_id)
    if time.monotonic() - _member_ok.get(key, 0) < MEMBER_CACHE_SECONDS:
        return True
    try:
        member = await bot.get_chat_member(_chat_ref(channel), user_id)
    except TelegramAPIError as e:
        # Misconfiguration (bot not admin in the channel) must not lock every user out.
        log.error("cannot check membership in %s: %s — is the bot an admin there?", channel, e)
        return True
    ok = member.status in ("creator", "administrator", "member") or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )
    if ok:
        _member_ok[key] = time.monotonic()
    return ok


async def channel_ok(bot: Bot, session: AsyncSession, user: User) -> tuple[bool, RuntimeSettings]:
    s = await load_settings(session)
    if not s.required_channel:
        return True, s
    return await is_member(bot, s.required_channel, user.telegram_id), s


def channel_link(s: RuntimeSettings) -> str:
    if s.required_channel_link:
        return s.required_channel_link
    if s.required_channel.startswith("@"):
        return f"https://t.me/{s.required_channel[1:]}"
    return ""
