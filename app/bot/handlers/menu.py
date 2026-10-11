from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import NavCB
from app.services.runtime_settings import load_settings

router = Router(name="menu")


@router.message(F.text == T.BTN_HELP)
async def help_(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    s = await load_settings(session)
    await message.answer(T.help_text(s.tutorial_text, s.tos_text))


@router.message(F.text == T.BTN_SUPPORT)
async def support(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    s = await load_settings(session)
    await message.answer(T.support_text(s.support_username))


@router.callback_query(NavCB.filter(F.to == "menu"))
async def nav_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(T.MAIN_MENU, reply_markup=K.main_menu())
