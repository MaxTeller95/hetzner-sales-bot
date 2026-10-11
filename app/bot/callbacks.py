"""Callback-data schemas. All purchase state lives in callback data, so menus are stateless."""
from aiogram.filters.callback_data import CallbackData


class BuyCB(CallbackData, prefix="b"):
    step: str  # prov | loc | plans | plan | bill | os | ok
    prov: int = 0
    loc: int = 0
    plan: int = 0
    mode: str = "h"
    img: int = 0
    sort: str = "a"  # a = all plans, c = cheapest first
    page: int = 0


class SrvCB(CallbackData, prefix="s"):
    action: str
    id: int = 0
    arg: int = 0


class IpCB(CallbackData, prefix="i"):
    action: str
    id: int = 0
    arg: int = 0


class WalletCB(CallbackData, prefix="w"):
    action: str
    arg: int = 0


class TopupAdminCB(CallbackData, prefix="ta"):
    action: str  # ok | no
    id: int


class NavCB(CallbackData, prefix="n"):
    to: str  # menu | servers | wallet | ips | buy
