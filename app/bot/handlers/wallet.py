"""Wallet: balance, history and manual (card-to-card) top-ups."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import WalletCB
from app.bot.handlers.common import show
from app.config import get_settings
from app.db.models import Transaction, User
from app.services import topups
from app.services import wallet as wallet_svc
from app.services.runtime_settings import load_settings
from app.utils import fmt_money, parse_amount

router = Router(name="wallet")


class TopUp(StatesGroup):
    amount = State()
    receipt = State()


async def render_wallet(session: AsyncSession, user: User):
    wallet = await wallet_svc.get_wallet(session, user.id)
    burn = await wallet_svc.hourly_burn(session, user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ افزایش موجودی", callback_data=WalletCB(action="top"))
    kb.button(text="📜 تراکنش‌ها", callback_data=WalletCB(action="hist"))
    kb.adjust(1)
    return T.wallet_card(wallet.balance, burn), kb.as_markup()


def cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text=T.CANCEL, callback_data=WalletCB(action="cancel"))
    return kb.as_markup()


@router.message(F.text == T.BTN_WALLET)
async def wallet_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    text, kb = await render_wallet(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(WalletCB.filter())
async def wallet_action(cb: CallbackQuery, callback_data: WalletCB, session: AsyncSession, user: User, state: FSMContext) -> None:
    a = callback_data.action
    if a in ("view", "cancel"):
        await state.clear()
        await cb.answer()
        await show(cb, *await render_wallet(session, user))
    elif a == "top":
        await state.clear()
        kb = InlineKeyboardBuilder()
        for amount in await topups.presets(session):
            kb.button(text=fmt_money(amount), callback_data=WalletCB(action="amt", arg=amount))
        kb.button(text="✏️ مبلغ دلخواه", callback_data=WalletCB(action="custom"))
        kb.button(text=T.BACK, callback_data=WalletCB(action="view"))
        kb.adjust(2)
        await cb.answer()
        await show(cb, T.TOPUP_CHOOSE, kb.as_markup())
    elif a == "custom":
        await state.set_state(TopUp.amount)
        await cb.answer()
        await show(cb, T.TOPUP_CUSTOM, cancel_kb())
    elif a == "amt":
        await _ask_receipt(cb.message, session, state, callback_data.arg, edit=True)
        await cb.answer()
    elif a == "hist":
        rows = (
            await session.scalars(
                select(Transaction).where(Transaction.user_id == user.id).order_by(Transaction.id.desc()).limit(15)
            )
        ).all()
        kb = InlineKeyboardBuilder()
        kb.button(text=T.BACK, callback_data=WalletCB(action="view"))
        await cb.answer()
        await show(cb, T.tx_history(rows), kb.as_markup())
    else:
        await cb.answer()


async def _ask_receipt(message: Message, session: AsyncSession, state: FSMContext, amount: int, edit: bool) -> None:
    s = await load_settings(session)
    if amount < s.topup_min_amount:
        await message.answer(f"❗️ حداقل مبلغ شارژ {fmt_money(s.topup_min_amount)} است.")
        return
    await state.set_state(TopUp.receipt)
    await state.update_data(amount=amount)
    text = T.topup_pay(amount, s.topup_card_info)
    if edit:
        await message.edit_text(text, reply_markup=cancel_kb())
    else:
        await message.answer(text, reply_markup=cancel_kb())


@router.message(TopUp.amount, F.text, ~F.text.in_(T.MENU_BUTTONS))
async def custom_amount(message: Message, session: AsyncSession, state: FSMContext) -> None:
    amount = parse_amount(message.text)
    if not amount:
        await message.answer(T.TOPUP_INVALID, reply_markup=cancel_kb())
        return
    await _ask_receipt(message, session, state, amount, edit=False)


@router.message(TopUp.receipt, F.photo | F.document)
async def receipt(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    amount = int(data.get("amount", 0))
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    req = await topups.create_request(session, user, amount, file_id)
    await session.commit()
    await state.clear()
    await message.answer(T.topup_submitted(req.id), reply_markup=K.main_menu())

    caption = (
        f"💳 <b>Top-up request #{req.id}</b>\n"
        f"User: {user.display} (<code>{user.telegram_id}</code>)\n"
        f"Amount: <b>{amount:,} IRR</b>"
    )
    for admin_id in get_settings().admin_id_list:
        try:
            if message.photo:
                await message.bot.send_photo(admin_id, file_id, caption=caption, reply_markup=K.topup_admin(req.id))
            else:
                await message.bot.send_document(admin_id, file_id, caption=caption, reply_markup=K.topup_admin(req.id))
        except Exception:  # noqa: BLE001 - one unreachable admin must not break the flow
            pass


@router.message(TopUp.receipt, ~F.text.in_(T.MENU_BUTTONS))
async def receipt_expected(message: Message) -> None:
    await message.answer(T.TOPUP_SEND_PHOTO, reply_markup=cancel_kb())
