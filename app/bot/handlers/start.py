from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.gate import channel_link, channel_ok, send_tos, tos_unlocked
from app.db.models import User
from app.services.runtime_settings import load_settings
from app.utils import utcnow

router = Router(name="start")


async def _after_gate(bot: Bot, chat_id: int, session: AsyncSession, user: User) -> None:
    ok, s = await channel_ok(bot, session, user)
    if not ok:
        await bot.send_message(chat_id, T.join_channel(s.required_channel), reply_markup=K.join_channel(channel_link(s)))
        return
    await bot.send_message(chat_id, T.WELCOME, reply_markup=K.main_menu())


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    if user.tos_accepted_at is None:
        await send_tos(message.bot, message.chat.id, session, user)
        return
    await _after_gate(message.bot, message.chat.id, session, user)


@router.callback_query(F.data == "tos:wait")
async def tos_wait(cb: CallbackQuery) -> None:
    await cb.answer(T.TOS_TOO_EARLY, show_alert=True)


@router.callback_query(F.data == "tos:agree")
async def tos_agree(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    if user.tos_accepted_at is None:
        s = await load_settings(session)
        if not tos_unlocked(user, s):  # enforce the lock server-side too
            await cb.answer(T.TOS_TOO_EARLY, show_alert=True)
            return
        user.tos_accepted_at = utcnow()
        await session.commit()
    await cb.answer(T.TOS_ACCEPTED)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await _after_gate(cb.bot, cb.from_user.id, session, user)


@router.callback_query(F.data == "gate:check")
async def gate_check(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    ok, _ = await channel_ok(cb.bot, session, user)
    if not ok:
        await cb.answer(T.NOT_JOINED, show_alert=True)
        return
    await cb.answer("✅")
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    if user.tos_accepted_at is None:
        await send_tos(cb.bot, cb.from_user.id, session, user)
        return
    await cb.bot.send_message(cb.from_user.id, T.WELCOME, reply_markup=K.main_menu())
