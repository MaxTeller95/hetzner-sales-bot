"""Sending Telegram messages from processes other than the bot (worker, dashboard)."""
from __future__ import annotations

import logging
from typing import Protocol

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from app.config import get_settings

log = logging.getLogger(__name__)


def create_bot() -> Bot:
    return Bot(
        get_settings().bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


class Notifier(Protocol):
    async def send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> int | None: ...

    async def edit(
        self, chat_id: int | None, message_id: int | None, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> int | None: ...

    async def send_admins(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None: ...


class TelegramNotifier:
    def __init__(self, bot: Bot, admin_ids: list[int] | None = None):
        self.bot = bot
        self.admin_ids = admin_ids if admin_ids is not None else get_settings().admin_id_list

    async def send(self, chat_id, text, markup=None):
        try:
            msg = await self.bot.send_message(chat_id, text, reply_markup=markup)
            return msg.message_id
        except TelegramAPIError as e:
            log.warning("send to %s failed: %s", chat_id, e)
            return None

    async def edit(self, chat_id, message_id, text, markup=None):
        """Edit a progress message in place; fall back to a new message."""
        if chat_id is None:
            return None
        if message_id:
            try:
                await self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                return message_id
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return message_id
            except TelegramAPIError as e:
                log.info("edit failed (%s), sending new message", e)
        return await self.send(chat_id, text, markup)

    async def send_admins(self, text, markup=None):
        for admin_id in self.admin_ids:
            await self.send(admin_id, text, markup)


class RecordingNotifier:
    """Test double that records messages."""

    def __init__(self):
        self.messages: list[tuple[int | None, str]] = []
        self.admin_messages: list[str] = []

    async def send(self, chat_id, text, markup=None):
        self.messages.append((chat_id, text))
        return len(self.messages)

    async def edit(self, chat_id, message_id, text, markup=None):
        self.messages.append((chat_id, text))
        return message_id or len(self.messages)

    async def send_admins(self, text, markup=None):
        self.admin_messages.append(text)
