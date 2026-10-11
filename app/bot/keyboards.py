from __future__ import annotations

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts as T
from app.bot.callbacks import BuyCB, IpCB, NavCB, SrvCB, TopupAdminCB, WalletCB
from app.db.models import Server


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T.BTN_BUY), KeyboardButton(text=T.BTN_SERVERS)],
            [KeyboardButton(text=T.BTN_WALLET), KeyboardButton(text=T.BTN_IPS)],
            [KeyboardButton(text=T.BTN_HELP), KeyboardButton(text=T.BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def tos_wait(seconds: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=T.tos_wait_button(seconds), callback_data="tos:wait")]]
    )


def tos_agree() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=T.TOS_AGREE, callback_data="tos:agree")]])


def join_channel(link: str) -> InlineKeyboardMarkup:
    rows = []
    if link:
        rows.append([InlineKeyboardButton(text=T.JOIN_BTN, url=link)])
    rows.append([InlineKeyboardButton(text=T.JOINED_BTN, callback_data="gate:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(cb) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=T.BACK, callback_data=cb.pack())]


# ---- purchase ---------------------------------------------------------------
def purchase_ready(server: Server, changes_left: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تایید و تحویل سرور", callback_data=SrvCB(action="pok", id=server.id))
    if changes_left > 0:
        kb.button(text=f"🔄 تغییر IP ({changes_left} بار)", callback_data=SrvCB(action="pip", id=server.id))
    kb.button(text="📋 کپی IP", copy_text=CopyTextButton(text=server.ipv4 or "-"))
    kb.adjust(1)
    return kb.as_markup()


def delivered(server: Server, password: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 کپی IP", copy_text=CopyTextButton(text=server.ipv4 or "-"))
    if password:
        kb.button(text="📋 کپی رمز", copy_text=CopyTextButton(text=password))
    kb.button(text="🖥 مدیریت سرور", callback_data=SrvCB(action="v", id=server.id))
    kb.adjust(2, 1)
    return kb.as_markup()


def to_server(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🖥 مدیریت سرور", callback_data=SrvCB(action="v", id=server_id).pack())]]
    )


def to_wallet() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=T.BUY_TOPUP_BTN, callback_data=WalletCB(action="top").pack())]]
    )


def topup_admin(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تایید", callback_data=TopupAdminCB(action="ok", id=req_id).pack()),
                InlineKeyboardButton(text="❌ رد", callback_data=TopupAdminCB(action="no", id=req_id).pack()),
            ]
        ]
    )


def server_actions(server: Server) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    sid = server.id
    kb.button(text="🔄 بروزرسانی", callback_data=SrvCB(action="v", id=sid))
    kb.button(text="🔑 اطلاعات ورود", callback_data=SrvCB(action="cr", id=sid))
    if server.status == "active" and not server.locked:
        kb.button(text="🔁 ریبوت", callback_data=SrvCB(action="rb", id=sid))
        if server.is_online:
            kb.button(text="⏻ خاموش", callback_data=SrvCB(action="sd", id=sid))
        else:
            kb.button(text="▶️ روشن", callback_data=SrvCB(action="on", id=sid))
        kb.button(text="🧩 نصب مجدد OS", callback_data=SrvCB(action="rbd", id=sid))
        kb.button(text="🌐 تغییر IP", callback_data=SrvCB(action="ip", id=sid))
    if server.ipv4:
        kb.button(text="📋 کپی IP", copy_text=CopyTextButton(text=server.ipv4))
    if server.status in ("active", "suspended") and not server.locked:
        kb.button(text="🗑 حذف سرور", callback_data=SrvCB(action="del", id=sid))
    kb.button(text=T.BACK, callback_data=NavCB(to="servers"))
    kb.adjust(2)
    return kb.as_markup()


def confirm(yes_cb, no_cb, yes_text: str = "✅ بله") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=yes_text, callback_data=yes_cb.pack()),
                InlineKeyboardButton(text=T.CANCEL, callback_data=no_cb.pack()),
            ]
        ]
    )


def buy_back(step: str, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=T.BACK, callback_data=BuyCB(step=step, **kw).pack())
