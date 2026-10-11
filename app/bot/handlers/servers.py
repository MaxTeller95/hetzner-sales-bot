"""Server list and per-server management."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import NavCB, SrvCB
from app.bot.handlers.common import show
from app.db.models import OSImage, Server, ServerStatus, User
from app.providers.base import ProviderError
from app.providers.registry import get_provider
from app.services import servers as server_svc
from app.services.errors import UserError
from app.services.runtime_settings import load_settings
from app.utils import decrypt

router = Router(name="servers")
log = logging.getLogger(__name__)


async def render_list(session: AsyncSession, user: User):
    servers = await server_svc.list_user_servers(session, user)
    if not servers:
        return T.NO_SERVERS, None
    kb = InlineKeyboardBuilder()
    for srv in servers:
        kb.button(text=T.server_button(srv), callback_data=SrvCB(action="v", id=srv.id))
    kb.adjust(1)
    return T.SERVERS_HEADER, kb.as_markup()


async def _refresh_live(server: Server) -> None:
    if server.status != ServerStatus.ACTIVE or not server.provider_server_id:
        return
    try:
        info = await asyncio.wait_for(get_provider(server.provider).get_server(server.provider_server_id), timeout=6)
    except (ProviderError, asyncio.TimeoutError) as e:
        log.info("live status for %s unavailable: %s", server.name, e)
        return
    if info:
        server.power_status = info.status


async def render_server(session: AsyncSession, server: Server):
    s = await load_settings(session)
    if server.status == ServerStatus.AWAITING:
        changes_left = max(0, s.max_purchase_ip_changes - server.purchase_ip_changes)
        summary, details = None, ""
        if server.check_summary and server.check_passed:
            summary, _, details = server.check_summary.partition("\n")
        return T.ready_for_confirm(server, details, summary, changes_left), K.purchase_ready(server, changes_left)
    await _refresh_live(server)
    when = server_svc.next_ip_change_at(server, s.ip_change_cooldown_hours)
    when_text = f"{when:%Y-%m-%d %H:%M} UTC" if when else None
    return T.server_card(server, when_text), K.server_actions(server)


@router.message(F.text == T.BTN_SERVERS)
async def servers_menu(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    text, kb = await render_list(session, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(NavCB.filter(F.to == "servers"))
async def nav_servers(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await cb.answer()
    text, kb = await render_list(session, user)
    await show(cb, text, kb)


@router.callback_query(SrvCB.filter())
async def server_action(cb: CallbackQuery, callback_data: SrvCB, session: AsyncSession, user: User) -> None:
    a = callback_data.action
    server = await server_svc.get_user_server(session, user, callback_data.id)
    back = SrvCB(action="v", id=server.id)

    if a == "v":
        await cb.answer()
        await show(cb, *await render_server(session, server))

    elif a == "cr":
        if server.status not in (ServerStatus.ACTIVE, ServerStatus.SUSPENDED):
            raise UserError("اطلاعات ورود پس از تحویل سرور در دسترس است.")
        password = decrypt(server.root_password_enc)
        await cb.answer()
        await cb.message.answer(T.credentials(server, password), reply_markup=K.delivered(server, password))

    elif a in ("rb", "sd", "on"):
        server_svc.ensure_manageable(server)
        prompt = {"rb": T.CONFIRM_REBOOT, "sd": T.CONFIRM_SHUTDOWN, "on": T.CONFIRM_POWERON}[a]
        await cb.answer()
        await show(cb, prompt, K.confirm(SrvCB(action=a + "Y", id=server.id), back))

    elif a in ("rbY", "sdY", "onY"):
        action = {"rbY": "reboot", "sdY": "shutdown", "onY": "poweron"}[a]
        await server_svc.request_power(session, server, action)
        await cb.answer(T.ACTION_QUEUED, show_alert=True)
        await show(cb, *await render_server(session, server))

    elif a == "rbd":
        server_svc.ensure_manageable(server)
        images = (
            await session.scalars(
                select(OSImage)
                .where(OSImage.provider_id == server.provider_id, OSImage.is_active.is_(True))
                .order_by(OSImage.sort_order, OSImage.name)
            )
        ).all()
        kb = InlineKeyboardBuilder()
        for img in images:
            if img.supports(server.plan.arch):
                kb.button(text=f"💿 {img.name}", callback_data=SrvCB(action="rbo", id=server.id, arg=img.id))
        kb.adjust(2)
        kb.row(*K.back(back))
        await cb.answer()
        await show(cb, T.CHOOSE_REBUILD_OS, kb.as_markup())

    elif a == "rbo":
        image = await session.get(OSImage, callback_data.arg)
        if image is None:
            raise UserError("سیستم‌عامل پیدا نشد.")
        await cb.answer()
        await show(
            cb,
            T.confirm_rebuild(image.name),
            K.confirm(SrvCB(action="rbc", id=server.id, arg=image.id), back, "⚠️ بله، نصب شود"),
        )

    elif a == "rbc":
        await server_svc.request_rebuild(session, server, callback_data.arg)
        await cb.answer(T.ACTION_QUEUED, show_alert=True)
        await show(cb, *await render_server(session, server))

    elif a == "ip":
        server_svc.ensure_manageable(server)
        s = await load_settings(session)
        if server_svc.next_ip_change_at(server, s.ip_change_cooldown_hours):
            raise UserError("⏱ تغییر IP روزی یک بار مجاز است.")
        await cb.answer()
        await show(cb, T.confirm_ip_change(server), K.confirm(SrvCB(action="ipY", id=server.id), back))

    elif a == "ipY":
        await server_svc.request_ip_change(session, server)
        await cb.answer(T.ACTION_QUEUED, show_alert=True)
        await show(cb, *await render_server(session, server))

    elif a == "del":
        await cb.answer()
        await show(cb, T.confirm_delete(server), K.confirm(SrvCB(action="delY", id=server.id), back, "🗑 بله، حذف شود"))

    elif a == "delY":
        await server_svc.request_delete(session, server)
        await cb.answer(T.ACTION_QUEUED, show_alert=True)
        text, kb = await render_list(session, user)
        await show(cb, text, kb)

    elif a == "pok":
        if not await server_svc.deliver(session, server):
            await cb.answer()
            await show(cb, *await render_server(session, server))
            return
        await session.commit()
        password = decrypt(server.root_password_enc)
        await cb.answer("🎉")
        await show(cb, T.delivery(server, password), K.delivered(server, password))

    elif a == "pip":
        await server_svc.request_purchase_ip_change(session, server)
        server.progress_chat_id = cb.message.chat.id
        server.progress_message_id = cb.message.message_id
        await cb.answer()
        await show(cb, "🔄 در حال دریافت IP جدید و تست مجدد اتصال...")
        await session.commit()

    else:
        await cb.answer()
