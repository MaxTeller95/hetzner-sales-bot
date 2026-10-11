"""All user-facing Persian texts."""
from __future__ import annotations

from html import escape

from app.db.models import IPAddress, Plan, Server, ServerStatus
from app.services.pricing import Quote
from app.utils import flag, fmt_money

BTN_BUY = "🛒 خرید سرور"
BTN_SERVERS = "🖥 سرورهای من"
BTN_WALLET = "💰 کیف پول"
BTN_IPS = "🌐 IP رزرو"
BTN_HELP = "📖 راهنما و قوانین"
BTN_SUPPORT = "🆘 پشتیبانی"
MENU_BUTTONS = (BTN_BUY, BTN_SERVERS, BTN_WALLET, BTN_IPS, BTN_HELP, BTN_SUPPORT)

BACK = "🔙 بازگشت"
CANCEL = "✖️ انصراف"

STATUS_FA = {
    ServerStatus.PENDING: "⏳ در صف ساخت",
    ServerStatus.CREATING: "⚙️ در حال ساخت",
    ServerStatus.CHECKING: "📡 در حال تست اتصال",
    ServerStatus.AWAITING: "🕓 در انتظار تایید شما",
    ServerStatus.ACTIVE: "فعال",
    ServerStatus.SUSPENDED: "⛔️ معلق (کمبود موجودی)",
    ServerStatus.DELETING: "🗑 در حال حذف",
}
BILLING_FA = {"h": "ساعتی", "m": "ماهانه"}

WELCOME = "👋 به ربات فروش سرور مجازی خوش آمدید.\nاز منوی پایین یکی از گزینه‌ها را انتخاب کنید."
MAIN_MENU = "🏠 منوی اصلی"


def tos_message(tutorial: str, tos: str) -> str:
    return (
        "👋 <b>خوش آمدید!</b>\n"
        "پیش از شروع، لطفاً راهنما و قوانین را با دقت بخوانید.\n\n"
        f"📖 <b>راهنمای استفاده</b>\n<blockquote expandable>{escape(tutorial)}</blockquote>\n\n"
        f"📜 <b>قوانین و مقررات</b>\n<blockquote expandable>{escape(tos)}</blockquote>"
    )


def tos_wait_button(seconds: int) -> str:
    return f"⏳ لطفاً {seconds} ثانیه صبر کنید..."


TOS_AGREE = "✅ قوانین را خواندم و می‌پذیرم"
TOS_TOO_EARLY = "لطفاً ابتدا راهنما و قوانین را کامل مطالعه کنید."
TOS_ACCEPTED = "✅ قوانین پذیرفته شد. خوش آمدید!"


def join_channel(channel: str) -> str:
    return f"📢 برای استفاده از ربات ابتدا باید عضو کانال {escape(channel)} شوید.\nپس از عضویت روی «✅ عضو شدم» بزنید."


JOIN_BTN = "📢 عضویت در کانال"
JOINED_BTN = "✅ عضو شدم"
NOT_JOINED = "هنوز عضو کانال نشده‌اید."
BANNED = "⛔️ دسترسی شما به ربات مسدود شده است."
SLOW_DOWN = "لطفاً کمی آهسته‌تر 🙏"
ERROR_GENERIC = "⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید."


# ---- purchase ---------------------------------------------------------------
BUY_CHOOSE_PROVIDER = "🛒 <b>خرید سرور</b>\n\nارائه‌دهنده را انتخاب کنید:"
BUY_CHOOSE_LOCATION = "📍 <b>موقعیت سرور</b> را انتخاب کنید:"
BUY_NOTHING = "در حال حاضر پلنی برای فروش موجود نیست. لطفاً بعداً سر بزنید."
SORT_ALL = "📋 همه پلن‌ها"
SORT_CHEAP = "💸 ارزان‌ترین"


def buy_plans_header(location_name: str, cheapest: bool) -> str:
    order = "از ارزان‌ترین به گران‌ترین" if cheapest else "همه پلن‌ها"
    return f"📍 {escape(location_name)}\n🖥 <b>انتخاب پلن</b> ({order})\n\nقیمت‌ها برای هر ساعت است:"


def plan_button(plan: Plan, quote: Quote) -> str:
    return f"{plan.name} | {plan.specs} | {quote.hourly:,} ریال"


def plan_detail(plan: Plan, quote: Quote) -> str:
    cpu = "اختصاصی" if plan.cpu_type == "dedicated" else "اشتراکی"
    arch = "ARM" if plan.arch == "arm" else "x86"
    traffic = f"\n📶 ترافیک: {plan.traffic_tb:g} ترابایت" if plan.traffic_tb else ""
    mem = int(plan.memory_gb) if float(plan.memory_gb).is_integer() else plan.memory_gb
    return (
        f"🖥 <b>پلن {escape(plan.name)}</b>\n"
        f"{flag(plan.location.country)} {escape(plan.location.name)}\n\n"
        f"⚙️ پردازنده: {plan.cores} هسته ({cpu}، {arch})\n"
        f"🧠 رم: {mem} گیگابایت\n"
        f"💾 دیسک: {plan.disk_gb} گیگابایت{traffic}\n\n"
        f"💰 ساعتی: <b>{fmt_money(quote.hourly)}</b>\n"
        f"💰 ماهانه: <b>{fmt_money(quote.monthly)}</b>\n\n"
        "نوع پرداخت را انتخاب کنید:"
    )


def billing_button(mode: str, quote: Quote) -> str:
    if mode == "h":
        return f"⏱ ساعتی — {quote.hourly:,} ریال/ساعت"
    return f"📅 ماهانه — {quote.monthly:,} ریال/ماه"


BUY_CHOOSE_OS = "💿 <b>سیستم‌عامل</b> را انتخاب کنید:"


def buy_confirm(plan: Plan, os_name: str, mode: str, quote: Quote, balance: int, required: int) -> str:
    first = quote.hourly if mode == "h" else quote.monthly
    lines = [
        "🧾 <b>خلاصه سفارش</b>\n",
        f"🖥 پلن: {escape(plan.name)} ({plan.specs})",
        f"{flag(plan.location.country)} موقعیت: {escape(plan.location.name)}",
        f"💿 سیستم‌عامل: {escape(os_name)}",
        f"💳 نوع پرداخت: {BILLING_FA[mode]}",
        f"💰 مبلغ: {fmt_money(first)}" + (" برای هر ساعت" if mode == "h" else " برای ۳۰ روز"),
        "",
        f"👛 موجودی شما: {fmt_money(balance)}",
    ]
    if balance < required:
        lines.append(f"\n❗️ برای این خرید حداقل {fmt_money(required)} موجودی لازم است.")
    else:
        lines.append("\nℹ️ حداقل یک ساعت هزینه محاسبه می‌شود، حتی اگر سرور را زودتر حذف کنید.")
    return "\n".join(lines)


BUY_CONFIRM_BTN = "✅ تایید و ساخت سرور"
BUY_TOPUP_BTN = "➕ افزایش موجودی"


def progress_queued(server: Server) -> str:
    return f"⏳ سفارش شما ثبت شد (سرور <code>{server.name}</code>).\nساخت سرور معمولاً ۱ تا ۲ دقیقه طول می‌کشد..."


def progress_creating(server: Server) -> str:
    return f"⚙️ در حال ساخت سرور <code>{server.name}</code>..."


def progress_checking(server: Server, attempt_note: str = "") -> str:
    return (
        f"📡 سرور ساخته شد. در حال تست اتصال IP <code>{server.ipv4}</code> از داخل ایران..."
        + (f"\n{attempt_note}" if attempt_note else "")
    )


def progress_ip_retry(old_ip: str, used: int, maximum: int) -> str:
    return f"❌ IP <code>{old_ip}</code> تست اتصال را پاس نکرد. در حال دریافت IP جدید ({used}/{maximum})..."


def ready_for_confirm(server: Server, details: str, summary: str | None, changes_left: int) -> str:
    check = (
        f"📡 تست اتصال از ایران: <b>{summary}</b> ✅\n<blockquote expandable>{escape(details)}</blockquote>"
        if summary
        else "📡 سرویس تست اتصال موقتاً در دسترس نبود."
    )
    return (
        "✅ <b>سرور شما آماده تحویل است</b>\n\n"
        f"🌐 IP: <code>{server.ipv4}</code>\n{check}\n\n"
        f"اگر IP را نمی‌پسندید می‌توانید آن را تغییر دهید ({changes_left} بار باقی‌مانده؛ "
        f"هر تغییر {fmt_money(server.ip_hourly_price)}).\n"
        "در غیر این صورت روی «تایید و تحویل» بزنید."
    )


def purchase_failed(server: Server, refunded: int) -> str:
    refund = f"\n💰 مبلغ {fmt_money(refunded)} به کیف پول شما بازگردانده شد." if refunded else ""
    return f"❌ متاسفانه ساخت سرور <code>{server.name}</code> ناموفق بود.{refund}\nلطفاً دوباره تلاش کنید یا پلن دیگری انتخاب کنید."


def purchase_out_of_stock(refunded: int) -> str:
    refund = f"\n💰 مبلغ {fmt_money(refunded)} به کیف پول شما بازگردانده شد." if refunded else ""
    return f"😔 این پلن در این موقعیت موقتاً ناموجود است.{refund}\nلطفاً موقعیت یا پلن دیگری انتخاب کنید."


def purchase_check_failed(server: Server, refunded: int) -> str:
    refund = f"\n💰 مبلغ {fmt_money(refunded)} به کیف پول شما بازگردانده شد." if refunded else ""
    return (
        f"❌ پس از چند بار تعویض IP، اتصال سرور <code>{server.name}</code> از ایران برقرار نشد و سفارش لغو شد.{refund}\n"
        "لطفاً موقعیت دیگری را امتحان کنید."
    )


def delivery(server: Server, password: str | None) -> str:
    return (
        "🎉 <b>سرور شما تحویل داده شد</b>\n\n"
        f"🖥 نام: <code>{server.name}</code>\n"
        f"🌐 IP: <code>{server.ipv4}</code>\n"
        + (f"🌐 IPv6: <code>{server.ipv6}</code>\n" if server.ipv6 else "")
        + f"👤 کاربر: <code>{server.username}</code>\n"
        f"🔑 رمز عبور: <tg-spoiler><code>{escape(password or '—')}</code></tg-spoiler>\n"
        f"💿 سیستم‌عامل: {escape(server.image.name if server.image else '-')}\n\n"
        f"اتصال: <code>ssh {server.username}@{server.ipv4}</code>\n\n"
        "⚠️ لطفاً پس از اولین ورود رمز عبور را تغییر دهید."
    )


# ---- servers ----------------------------------------------------------------
NO_SERVERS = "شما هنوز سروری ندارید. از «🛒 خرید سرور» شروع کنید."
SERVERS_HEADER = "🖥 <b>سرورهای من</b>\nبرای مدیریت روی سرور بزنید:"


def online_badge(server: Server) -> str:
    if server.locked:
        return "🔒"
    if server.status != ServerStatus.ACTIVE:
        return "⏳"
    return "🟢" if server.is_online else "🔴"


def server_button(server: Server) -> str:
    return f"{online_badge(server)} {server.name} — {server.ipv4 or '...'}"


def server_card(server: Server, next_ip_change: str | None) -> str:
    if server.status == ServerStatus.ACTIVE:
        state = "🟢 آنلاین" if server.is_online else "🔴 آفلاین"
    else:
        state = STATUS_FA.get(server.status, server.status)
    lock = f"\n🔒 <b>قفل شده:</b> {escape(server.lock_reason or 'گزارش تخلف')}" if server.locked else ""
    price = (
        f"{fmt_money(server.hourly_price)} / ساعت"
        if server.billing_mode == "h"
        else f"{fmt_money(server.monthly_price)} / ماه"
    )
    renew = (
        f"\n🔄 تمدید بعدی: {server.next_charge_at:%Y-%m-%d %H:%M} UTC"
        if server.billing_mode == "m" and server.next_charge_at
        else ""
    )
    ipc = f"\n⏱ تغییر IP بعدی: {next_ip_change}" if next_ip_change else ""
    return (
        f"🖥 <b>{server.name}</b>\n"
        f"وضعیت: {state}{lock}\n\n"
        f"🌐 IP: <code>{server.ipv4 or '-'}</code>\n"
        f"📦 پلن: {escape(server.plan.name)} ({server.plan.specs})\n"
        f"{flag(server.location.country)} موقعیت: {escape(server.location.name)}\n"
        f"💿 سیستم‌عامل: {escape(server.image.name if server.image else '-')}\n"
        f"💳 {BILLING_FA[server.billing_mode]}: {price}{renew}{ipc}"
    )


def credentials(server: Server, password: str | None) -> str:
    return (
        f"🔑 <b>اطلاعات ورود {server.name}</b>\n"
        f"<blockquote expandable>IP: <code>{server.ipv4}</code>\n"
        f"User: <code>{server.username}</code>\n"
        f"Password: <code>{escape(password or '—')}</code>\n"
        f"SSH: <code>ssh {server.username}@{server.ipv4}</code></blockquote>"
    )


CONFIRM_REBOOT = "🔁 سرور ریبوت شود؟"
CONFIRM_SHUTDOWN = "⏻ سرور خاموش شود؟\nℹ️ سرور خاموش همچنان هزینه دارد."
CONFIRM_POWERON = "▶️ سرور روشن شود؟"
CHOOSE_REBUILD_OS = "🧩 <b>نصب مجدد سیستم‌عامل</b>\n⚠️ تمام اطلاعات سرور پاک می‌شود.\nسیستم‌عامل جدید را انتخاب کنید:"


def confirm_rebuild(os_name: str) -> str:
    return f"⚠️ سیستم‌عامل <b>{escape(os_name)}</b> نصب شود؟\nتمام اطلاعات فعلی سرور برای همیشه پاک می‌شود."


def confirm_ip_change(server: Server) -> str:
    return (
        f"🌐 IP فعلی: <code>{server.ipv4}</code>\n\n"
        "IP جدید دریافت شود؟\n"
        f"• هزینه: {fmt_money(server.ip_hourly_price)} (حداقل یک ساعت IP قبلی)\n"
        "• سرور برای چند ثانیه خاموش و دوباره روشن می‌شود.\n"
        "• تغییر IP روزی یک بار مجاز است."
    )


def confirm_delete(server: Server) -> str:
    return f"🗑 سرور <b>{server.name}</b> ({server.ipv4}) حذف شود؟\n⚠️ این کار برگشت‌پذیر نیست و تمام اطلاعات پاک می‌شود."


ACTION_QUEUED = "⏳ درخواست ثبت شد؛ نتیجه را به شما اطلاع می‌دهیم."
POWER_DONE = {
    "reboot": "🔁 سرور {name} ریبوت شد.",
    "shutdown": "⏻ سرور {name} خاموش شد.",
    "poweroff": "⏻ سرور {name} خاموش شد.",
    "poweron": "▶️ سرور {name} روشن شد.",
}


def rebuild_done(server: Server, password: str | None) -> str:
    return "🧩 نصب مجدد سیستم‌عامل انجام شد.\n\n" + delivery(server, password).split("\n\n", 1)[1]


def ip_changed(server: Server, old_ip: str, check: str | None) -> str:
    extra = f"\n📡 تست اتصال از ایران: {check}" if check else ""
    return f"🌐 IP سرور <b>{server.name}</b> تغییر کرد.\nقبلی: <code>{old_ip}</code>\nجدید: <code>{server.ipv4}</code>{extra}"


def server_deleted(server: Server) -> str:
    return f"🗑 سرور <b>{server.name}</b> حذف شد."


def operation_failed(server_name: str) -> str:
    return f"⚠️ عملیات روی سرور {server_name} ناموفق بود. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."


# ---- reserve IPs ---------------------------------------------------------------
def ips_header(count: int, maximum: int, unit_price_note: str) -> str:
    return (
        f"🌐 <b>IP های رزرو</b> ({count}/{maximum})\n\n"
        "IP رزرو یک IP اضافه است که می‌توانید هر زمان با IP یکی از سرورهای همان دیتاسنتر جابجا کنید.\n"
        f"{unit_price_note}"
    )


def ip_button(ip: IPAddress) -> str:
    state = "⏳" if ip.status != "active" else "🌐"
    return f"{state} {ip.address or 'در حال ساخت...'} — {ip.location.name}"


def ip_card(ip: IPAddress) -> str:
    return (
        f"🌐 <b>IP رزرو</b>\n\n"
        f"آدرس: <code>{ip.address or '-'}</code>\n"
        f"{flag(ip.location.country)} موقعیت: {escape(ip.location.name)} ({ip.datacenter or '-'})\n"
        f"💰 هزینه: {fmt_money(ip.hourly_price)} / ساعت"
    )


IP_CHOOSE_LOCATION = "📍 IP رزرو در کدام موقعیت ساخته شود؟\n(فقط روی سرورهای همان موقعیت قابل استفاده است)"


def ip_confirm_create(location_name: str, price: int) -> str:
    return (
        f"🌐 ساخت IP رزرو در {escape(location_name)}\n"
        f"💰 هزینه: {fmt_money(price)} برای هر ساعت (حداقل یک ساعت)\n\nتایید می‌کنید؟"
    )


IP_CHOOSE_SERVER = "🔁 IP رزرو با IP کدام سرور جابجا شود؟\nIP فعلی آن سرور به‌عنوان IP رزرو نگه داشته می‌شود."
IP_NO_SERVERS = "سرور فعالی در این دیتاسنتر ندارید."


def ip_created(ip: IPAddress) -> str:
    return f"✅ IP رزرو <code>{ip.address}</code> ساخته شد."


def ip_deleted(address: str | None) -> str:
    return f"🗑 IP رزرو <code>{address or '-'}</code> حذف شد."


def ip_swapped(server: Server, new_reserve: str) -> str:
    return (
        f"🔁 جابجایی انجام شد.\nIP جدید سرور <b>{server.name}</b>: <code>{server.ipv4}</code>\n"
        f"IP رزرو شما اکنون: <code>{new_reserve}</code>"
    )


# ---- wallet -----------------------------------------------------------------
def wallet_card(balance: int, burn: int) -> str:
    est = ""
    if burn > 0:
        hours = max(balance, 0) // burn
        est = f"\n🔥 مصرف فعلی: {fmt_money(burn)} در ساعت\n⏳ موجودی برای حدود {hours:,} ساعت کافی است."
    return f"💰 <b>کیف پول</b>\n\n👛 موجودی: <b>{fmt_money(balance)}</b>{est}"


TOPUP_CHOOSE = "➕ <b>افزایش موجودی</b>\nمبلغ را انتخاب کنید یا «مبلغ دلخواه» را بزنید:"
TOPUP_CUSTOM = "💬 مبلغ دلخواه را به ریال و فقط با عدد وارد کنید:"
TOPUP_INVALID = "❗️ لطفاً فقط عدد وارد کنید (به ریال)."


def topup_pay(amount: int, card_info: str) -> str:
    return (
        f"💳 لطفاً مبلغ <b>{fmt_money(amount)}</b> را به حساب زیر واریز کنید:\n\n"
        f"<blockquote>{escape(card_info)}</blockquote>\n\n"
        "📸 سپس <b>تصویر رسید</b> را همین‌جا ارسال کنید."
    )


TOPUP_SEND_PHOTO = "📸 لطفاً تصویر رسید پرداخت را ارسال کنید."


def topup_submitted(req_id: int) -> str:
    return f"✅ رسید شما ثبت شد (درخواست #{req_id}).\nپس از بررسی، کیف پول شما شارژ می‌شود."


def topup_approved(amount: int, balance: int) -> str:
    return f"✅ کیف پول شما {fmt_money(amount)} شارژ شد.\n👛 موجودی فعلی: {fmt_money(balance)}"


def topup_rejected(req_id: int, note: str | None) -> str:
    return f"❌ درخواست شارژ #{req_id} رد شد." + (f"\nتوضیح: {escape(note)}" if note else "")


def low_balance(balance: int, hours: int) -> str:
    return (
        f"⚠️ <b>موجودی کیف پول شما رو به اتمام است</b>\n👛 موجودی: {fmt_money(balance)}\n"
        f"⏳ تقریباً {hours} ساعت دیگر کافی است.\n"
        "برای جلوگیری از خاموش شدن سرورها، لطفاً کیف پول را شارژ کنید."
    )


def suspended(names: list[str], grace_hours: int) -> str:
    return (
        "⛔️ <b>موجودی کیف پول شما منفی شد</b> و سرورهای زیر خاموش شدند:\n"
        + "\n".join(f"• {n}" for n in names)
        + f"\n\nاگر تا {grace_hours} ساعت کیف پول شارژ نشود، سرورها و IPها حذف خواهند شد."
    )


def terminated(names: list[str]) -> str:
    return "🗑 به دلیل منفی ماندن موجودی، منابع زیر حذف شدند:\n" + "\n".join(f"• {n}" for n in names)


RESUMED = "✅ کیف پول شارژ شد و سرورهای معلق شما دوباره روشن می‌شوند."


def tx_history(rows) -> str:
    if not rows:
        return "📜 هنوز تراکنشی ندارید."
    lines = ["📜 <b>آخرین تراکنش‌ها</b>\n<blockquote expandable>"]
    for t in rows:
        sign = "➕" if t.amount > 0 else "➖"
        lines.append(f"{sign} {abs(t.amount):,} — {escape(t.description)}\n<i>{t.created_at:%Y-%m-%d %H:%M}</i>")
    lines.append("</blockquote>")
    return "\n".join(lines)


# ---- abuse ------------------------------------------------------------------
def abuse_locked(server: Server) -> str:
    return (
        f"🚨 <b>گزارش تخلف (Abuse)</b> برای سرور <b>{server.name}</b> (<code>{server.ipv4}</code>) دریافت شد.\n"
        "سرور برای بررسی قفل شد. لطفاً فوراً با پشتیبانی تماس بگیرید."
    )


def abuse_unlocked(server: Server) -> str:
    return f"✅ قفل سرور <b>{server.name}</b> برداشته شد."


# ---- help -------------------------------------------------------------------
def help_text(tutorial: str, tos: str) -> str:
    return (
        f"📖 <b>راهنمای استفاده</b>\n<blockquote expandable>{escape(tutorial)}</blockquote>\n\n"
        f"📜 <b>قوانین</b>\n<blockquote expandable>{escape(tos)}</blockquote>"
    )


def support_text(username: str) -> str:
    if not username:
        return "🆘 پشتیبانی به‌زودی فعال می‌شود."
    return f"🆘 برای پشتیبانی به @{escape(username.lstrip('@'))} پیام دهید."
