"""Admin shortcuts inside Telegram (the full admin panel is the web dashboard)."""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts as T
from app.bot.callbacks import TopupAdminCB
from app.config import get_settings
from app.services import stats as stats_svc
from app.services import topups
from app.services import wallet as wallet_svc

router = Router(name="admin")


def is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_list


@router.callback_query(TopupAdminCB.filter())
async def topup_decision(cb: CallbackQuery, callback_data: TopupAdminCB, session: AsyncSession) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Not allowed", show_alert=True)
        return
    reviewer = f"tg:{cb.from_user.id}"
    if callback_data.action == "ok":
        result = await topups.approve(session, callback_data.id, reviewer)
        if result is None:
            await cb.answer("Already processed", show_alert=True)
            return
        req, resumed = result
        await session.commit()
        wallet = await wallet_svc.get_wallet(session, req.user_id)
        await cb.bot.send_message(req.user.telegram_id, T.topup_approved(req.amount, wallet.balance))
        if resumed:
            await cb.bot.send_message(req.user.telegram_id, T.RESUMED)
        status = f"\n\n✅ Approved by {cb.from_user.full_name}"
    else:
        req = await topups.reject(session, callback_data.id, reviewer)
        if req is None:
            await cb.answer("Already processed", show_alert=True)
            return
        await session.commit()
        await cb.bot.send_message(req.user.telegram_id, T.topup_rejected(req.id, None))
        status = f"\n\n❌ Rejected by {cb.from_user.full_name}"
    await cb.answer("Done")
    try:
        await cb.message.edit_caption(caption=(cb.message.caption or "") + status, reply_markup=None)
    except TelegramBadRequest:
        pass


@router.message(Command("admin"))
async def admin_stats(message: Message, session: AsyncSession) -> None:
    if not is_admin(message.from_user.id):
        return
    st = await stats_svc.overview(session)
    costs = ", ".join(f"{v:.2f} {k}" for k, v in st["provider_monthly_cost"].items()) or "0"
    await message.answer(
        "📊 <b>Admin</b>\n"
        f"Users: {st['users']}\n"
        f"Active servers: {st['active_servers']} (pool: {st['pooled_servers']})\n"
        f"Reserve IPs: {st['reserve_ips']}\n"
        f"Est. provider invoice / month: {costs}\n"
        f"Revenue run-rate / month: {st['revenue_monthly']:,} IRR\n"
        f"Pending top-ups: {st['pending_topups']} · Open abuse: {st['open_abuse']}\n\n"
        f"Dashboard: {get_settings().web_base_url}"
    )
