"""Telegram bot process.  Usage: python -m app.bot"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher, F
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from app.bot import texts as T
from app.bot.handlers import admin, buy, ips, menu, servers, start, wallet
from app.bot.middlewares import DbSessionMiddleware, GateMiddleware, ThrottleMiddleware, UserMiddleware
from app.config import get_settings
from app.providers.registry import close_all
from app.services.errors import UserError
from app.services.notifier import create_bot
from app.services.pricing import PricingNotConfigured

log = logging.getLogger("bot")


def build_storage() -> BaseStorage:
    url = get_settings().redis_url
    if url:
        from aiogram.fsm.storage.redis import RedisStorage

        return RedisStorage.from_url(url)
    return MemoryStorage()


async def on_error(event: ErrorEvent) -> None:
    exc = event.exception
    if isinstance(exc, PricingNotConfigured):
        exc = UserError("قیمت‌گذاری هنوز توسط مدیریت تنظیم نشده است.")
    if isinstance(exc, UserError):
        text = str(exc)
    else:
        log.exception("unhandled error", exc_info=exc)
        text = T.ERROR_GENERIC
    upd = event.update
    try:
        if upd.callback_query:
            await upd.callback_query.answer(text[:200], show_alert=True)
        elif upd.message:
            await upd.message.answer(text)
    except Exception:  # noqa: BLE001
        pass


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=build_storage())
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserMiddleware())
    for observer in (dp.message, dp.callback_query):
        observer.middleware(ThrottleMiddleware())
        observer.middleware(GateMiddleware())
    dp.message.filter(F.chat.type == "private")
    dp.include_routers(start.router, admin.router, menu.router, buy.router, servers.router, ips.router, wallet.router)
    dp.errors.register(on_error)
    return dp


async def main() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN is not set")
    cfg.check_production()
    bot = create_bot()
    dp = build_dispatcher()
    await bot.set_my_commands([BotCommand(command="start", description="منوی اصلی")])
    log.info("bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await close_all()


if __name__ == "__main__":
    asyncio.run(main())
