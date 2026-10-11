"""Reserve IP management."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, CopyTextButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import IpCB
from app.bot.handlers.common import show
from app.db.models import Location, Plan, Provider, Server, User
from app.services import ips as ip_svc
from app.services.errors import UserError
from app.services.pricing import PricingNotConfigured, quote_reserve_ip
from app.services.runtime_settings import load_settings
from app.utils import flag

router = Router(name="ips")


async def render_ips(session: AsyncSession, user: User):
    s = await load_settings(session)
    ips = await ip_svc.list_user_ips(session, user)
    kb = InlineKeyboardBuilder()
    for ip in ips:
        kb.button(text=T.ip_button(ip), callback_data=IpCB(action="v", id=ip.id))
    if len(ips) < s.max_reserve_ips:
        kb.button(text="➕ ساخت IP رزرو", callback_data=IpCB(action="new"))
    kb.adjust(1)
    note = f"💰 هزینه هر IP رزرو {s.reserve_ip_multiplier:g} برابر IP عادی است و ساعتی محاسبه می‌شود."
    return T.ips_header(len(ips), s.max_reserve_ips, note), kb.as_markup()


async def _ip_locations(session: AsyncSession) -> list[Location]:
    ids = (
        await session.scalars(
            select(Plan.location_id)
            .join(Provider, Plan.provider_id == Provider.id)
            .where(Plan.is_active.is_(True), Provider.is_active.is_(True))
            .distinct()
        )
    ).all()
    if not ids:
        return []
    return list(
        (
            await session.scalars(
                select(Location).where(Location.id.in_(ids), Location.is_active.is_(True)).order_by(Location.sort_order)
            )
        ).all()
    )


@router.message(F.text == T.BTN_IPS)
async def ips_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    text, kb = await render_ips(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(IpCB.filter())
async def ip_action(cb: CallbackQuery, callback_data: IpCB, session: AsyncSession, user: User) -> None:
    a = callback_data.action
    to_list = IpCB(action="list")

    if a == "list":
        await cb.answer()
        await show(cb, *await render_ips(session, user))

    elif a == "new":
        s = await load_settings(session)
        kb = InlineKeyboardBuilder()
        try:
            for loc in await _ip_locations(session):
                price = quote_reserve_ip(loc, s)
                kb.button(text=f"{flag(loc.country)} {loc.name} — {price:,} ریال/ساعت", callback_data=IpCB(action="loc", arg=loc.id))
        except PricingNotConfigured:
            raise UserError("قیمت‌گذاری هنوز تنظیم نشده است.") from None
        kb.adjust(1)
        kb.row(*K.back(to_list))
        await cb.answer()
        await show(cb, T.IP_CHOOSE_LOCATION, kb.as_markup())

    elif a == "loc":
        loc = await session.get(Location, callback_data.arg)
        if loc is None:
            raise UserError("موقعیت پیدا نشد.")
        price = quote_reserve_ip(loc, await load_settings(session))
        await cb.answer()
        await show(cb, T.ip_confirm_create(loc.name, price), K.confirm(IpCB(action="mk", arg=loc.id), to_list))

    elif a == "mk":
        await ip_svc.create_reserve_ip(session, user, callback_data.arg)
        await session.commit()
        await cb.answer(T.ACTION_QUEUED, show_alert=True)
        await show(cb, *await render_ips(session, user))

    elif a == "v":
        ip = await ip_svc.get_user_ip(session, user, callback_data.id)
        kb = InlineKeyboardBuilder()
        if ip.status == "active":
            kb.button(text="🔁 جابجایی با IP سرور", callback_data=IpCB(action="sw", id=ip.id))
            kb.button(text="🗑 حذف", callback_data=IpCB(action="del", id=ip.id))
            kb.button(text="📋 کپی IP", copy_text=CopyTextButton(text=ip.address))
        kb.button(text=T.BACK, callback_data=to_list)
        kb.adjust(1)
        await cb.answer()
        await show(cb, T.ip_card(ip), kb.as_markup())

    elif a == "sw":
        ip = await ip_svc.get_user_ip(session, user, callback_data.id)
        servers = await ip_svc.swappable_servers(session, ip)
        kb = InlineKeyboardBuilder()
        for srv in servers:
            kb.button(text=f"🖥 {srv.name} — {srv.ipv4}", callback_data=IpCB(action="swS", id=ip.id, arg=srv.id))
        kb.adjust(1)
        kb.row(*K.back(IpCB(action="v", id=ip.id)))
        await cb.answer()
        await show(cb, T.IP_CHOOSE_SERVER if servers else T.IP_NO_SERVERS, kb.as_markup())

    elif a == "swS":
        ip = await ip_svc.get_user_ip(session, user, callback_data.id)
        server = await session.get(Server, callback_data.arg)
        if server is None or server.user_id != user.id:
            raise UserError("سرور پیدا نشد.")
        await cb.answer()
        await show(
            cb,
            f"🔁 IP سرور <b>{server.name}</b> از <code>{server.ipv4}</code> به <code>{ip.address}</code> تغییر کند؟\n"
            "سرور چند ثانیه خاموش و دوباره روشن می‌شود.",
            K.confirm(IpCB(action="swY", id=ip.id, arg=server.id), IpCB(action="v", id=ip.id)),
        )

    elif a == "swY":
        ip = await ip_svc.get_user_ip(session, user, callback_data.id)
        server = await session.get(Server, callback_data.arg)
        if server is None or server.user_id != user.id:
            raise UserError("سرور پیدا نشد.")
        await ip_svc.request_swap(session, ip, server)
        await cb.answer(T.ACTION_QUEUED, show_alert=True)
        await show(cb, *await render_ips(session, user))

    elif a == "del":
        ip = await ip_svc.get_user_ip(session, user, callback_data.id)
        await cb.answer()
        await show(
            cb,
            f"🗑 IP رزرو <code>{ip.address}</code> حذف شود؟ این IP دیگر قابل بازگشت نیست.",
            K.confirm(IpCB(action="delY", id=ip.id), IpCB(action="v", id=ip.id), "🗑 بله، حذف شود"),
        )

    elif a == "delY":
        ip = await ip_svc.get_user_ip(session, user, callback_data.id)
        await ip_svc.request_delete_ip(session, ip)
        await cb.answer(T.ACTION_QUEUED, show_alert=True)
        await show(cb, *await render_ips(session, user))

    else:
        await cb.answer()
