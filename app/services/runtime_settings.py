"""Admin-editable settings stored in the `settings` table, with typed defaults."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import SettingKV

_cfg = get_settings()

DEFAULT_TUTORIAL = """۱. از منوی «🛒 خرید سرور» موقعیت و پلن دلخواه را انتخاب کنید.
۲. سیستم‌عامل را انتخاب و خرید را تایید کنید.
۳. پیش از تحویل، اتصال سرور از چند نقطه داخل ایران تست می‌شود تا مطمئن شوید IP در دسترس است.
۴. اگر IP را نپسندیدید، پیش از تحویل تا ۵ بار و پس از تحویل روزی یک بار می‌توانید آن را عوض کنید.
۵. هزینه‌ها ساعتی از کیف پول کسر می‌شود. هر سرور و هر IP حداقل یک ساعت محاسبه می‌شود.
۶. از «🖥 سرورهای من» می‌توانید سرور را ریبوت، خاموش، بازسازی یا حذف کنید."""

DEFAULT_TOS = """• استفاده از سرورها برای اسپم، حمله، اسکن، فیشینگ، ماینینگ و هر فعالیت غیرقانونی ممنوع است.
• در صورت دریافت گزارش Abuse، سرور فوراً قفل می‌شود و مسئولیت آن با کاربر است.
• هزینه هر سرور و هر IP حداقل یک ساعت محاسبه می‌شود، حتی اگر بلافاصله حذف شود.
• در صورت منفی شدن موجودی، سرورها خاموش و پس از مهلت تعیین‌شده حذف می‌شوند و اطلاعات قابل بازیابی نیست.
• مسئولیت پشتیبان‌گیری از اطلاعات بر عهده کاربر است.
• مبالغ شارژ شده به کیف پول قابل استرداد نقدی نیست."""


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: type
    default: Any
    label: str
    group: str
    help: str = ""
    multiline: bool = False


DEFS: list[SettingDef] = [
    # Pricing
    SettingDef("exchange_rate_eur", int, _cfg.default_exchange_rate_eur, "Exchange rate EUR → IRR", "Pricing", "Rials per 1 EUR"),
    SettingDef("exchange_rate_usd", int, _cfg.default_exchange_rate_usd, "Exchange rate USD → IRR", "Pricing", "Rials per 1 USD"),
    SettingDef("profit_margin_percent", float, _cfg.default_profit_margin_percent, "Profit margin %", "Pricing"),
    SettingDef("monthly_discount_percent", float, 0.0, "Monthly discount %", "Pricing", "Monthly = hourly × 720 × (1 − discount)"),
    SettingDef("price_round_to", int, 100, "Round prices up to (IRR)", "Pricing"),
    SettingDef("reserve_ip_multiplier", float, 2.0, "Reserve IP price multiplier", "Pricing", "× the normal IP price"),
    # Wallet & billing
    SettingDef("low_balance_threshold", int, _cfg.default_low_balance_threshold, "Low balance warning threshold (IRR)", "Wallet"),
    SettingDef("low_balance_notify_hours", int, 12, "Repeat low-balance warning every N hours", "Wallet"),
    SettingDef("purchase_buffer_hours", int, 2, "Extra hours of balance required to buy (hourly)", "Wallet"),
    SettingDef("suspend_grace_hours", int, 24, "Delete resources after N hours of negative balance", "Wallet"),
    SettingDef("topup_min_amount", int, 500_000, "Minimum top-up (IRR)", "Wallet"),
    SettingDef("topup_presets", str, "1000000,2000000,5000000,10000000", "Top-up preset amounts", "Wallet", "Comma-separated IRR"),
    SettingDef("topup_card_info", str, "6037-0000-0000-0000\nبه نام: ...", "Card / payment info shown to users", "Wallet", multiline=True),
    # Servers & IPs
    SettingDef("max_purchase_ip_changes", int, 5, "IP changes allowed during purchase", "Servers"),
    SettingDef("ip_change_cooldown_hours", int, 24, "Hours between IP changes after delivery", "Servers"),
    SettingDef("max_reserve_ips", int, 2, "Reserve IPs per user", "Servers"),
    SettingDef("auto_deliver_minutes", int, 15, "Auto-deliver after N minutes without confirmation", "Servers"),
    SettingDef("refund_failed_purchase", bool, True, "Refund when a purchase cannot pass the connectivity check", "Servers"),
    SettingDef("server_name_prefix", str, "srv", "Server hostname prefix", "Servers"),
    SettingDef("pool_default_image", str, "ubuntu-24.04", "Image used for pre-created pool servers", "Servers"),
    # Connectivity check
    SettingDef("connectivity_enabled", bool, True, "Run connectivity check before delivery", "Connectivity"),
    SettingDef("connectivity_nodes", str, "", "check-host.net nodes (comma-separated)", "Connectivity", "Empty = auto-pick Iranian nodes"),
    SettingDef("connectivity_required_nodes", int, 4, "Nodes that must pass", "Connectivity"),
    SettingDef("connectivity_min_packets", int, 4, "Ping packets (of 4) a node must get back", "Connectivity"),
    SettingDef("connectivity_initial_delay", int, 20, "Seconds to wait after boot before testing", "Connectivity"),
    # Abuse
    SettingDef("abuse_auto_poweroff", bool, True, "Power off servers when an abuse report arrives", "Abuse"),
    # Bot
    SettingDef("required_channel", str, "", "Required channel (@username or -100… id)", "Bot", "Bot must be admin there. Empty = disabled"),
    SettingDef("required_channel_link", str, "", "Channel invite link", "Bot"),
    SettingDef("support_username", str, "", "Support username (without @)", "Bot"),
    SettingDef("tos_lock_seconds", int, 10, "Seconds before the ToS can be accepted", "Bot"),
    SettingDef("tutorial_text", str, DEFAULT_TUTORIAL, "Usage tutorial (Persian)", "Bot", multiline=True),
    SettingDef("tos_text", str, DEFAULT_TOS, "Terms of service (Persian)", "Bot", multiline=True),
]
DEFS_BY_KEY = {d.key: d for d in DEFS}


def _parse(d: SettingDef, raw: str | None) -> Any:
    if raw is None:
        return d.default
    try:
        if d.type is bool:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if d.type is int:
            return int(float(raw.replace(",", "")))
        if d.type is float:
            return float(raw.replace(",", ""))
        return raw
    except ValueError:
        return d.default


class RuntimeSettings:
    def __init__(self, values: dict[str, Any]):
        self._values = values

    def __getattr__(self, item: str) -> Any:
        try:
            return self._values[item]
        except KeyError:
            raise AttributeError(item) from None

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)


_cache: tuple[float, RuntimeSettings] | None = None
CACHE_TTL = 15.0


def invalidate() -> None:
    global _cache
    _cache = None


async def load_settings(session: AsyncSession, *, fresh: bool = False) -> RuntimeSettings:
    global _cache
    if not fresh and _cache and time.monotonic() - _cache[0] < CACHE_TTL:
        return _cache[1]
    rows = {r.key: r.value for r in (await session.scalars(select(SettingKV))).all()}
    values = {d.key: _parse(d, rows.get(d.key)) for d in DEFS}
    rs = RuntimeSettings(values)
    _cache = (time.monotonic(), rs)
    return rs


async def save_settings(session: AsyncSession, values: dict[str, str]) -> None:
    for key, raw in values.items():
        d = DEFS_BY_KEY.get(key)
        if d is None:
            continue
        if d.type is bool:
            raw = "true" if str(raw).lower() in ("1", "true", "on", "yes") else "false"
        row = await session.get(SettingKV, key)
        if row is None:
            session.add(SettingKV(key=key, value=str(raw)))
        else:
            row.value = str(raw)
    await session.flush()
    invalidate()
