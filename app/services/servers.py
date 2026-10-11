"""User-facing server operations: validation + enqueueing jobs for the worker."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OSImage, Plan, Server, ServerStatus, User
from app.services import billing, jobs
from app.services import wallet as wallet_svc
from app.services.errors import InsufficientBalance, UserError
from app.services.pricing import PricingNotConfigured, quote_plan
from app.services.runtime_settings import load_settings
from app.utils import utcnow

POWER_ACTIONS = ("reboot", "shutdown", "poweron", "poweroff")


async def purchasable_plan(session: AsyncSession, plan_id: int) -> Plan:
    plan = await session.get(Plan, plan_id)
    if (
        plan is None
        or not plan.is_active
        or not plan.is_available
        or not plan.location.is_active
        or not plan.provider.is_active
    ):
        raise UserError("این پلن در حال حاضر قابل خرید نیست. لطفاً پلن دیگری انتخاب کنید.")
    return plan


async def start_purchase(
    session: AsyncSession,
    user: User,
    *,
    plan_id: int,
    image_id: int,
    mode: str,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> Server:
    if mode not in ("h", "m"):
        raise UserError("نوع پرداخت نامعتبر است.")
    s = await load_settings(session)
    plan = await purchasable_plan(session, plan_id)
    image = await session.get(OSImage, image_id)
    if image is None or not image.is_active or image.provider_id != plan.provider_id or not image.supports(plan.arch):
        raise UserError("این سیستم‌عامل برای این پلن در دسترس نیست.")
    try:
        quote = quote_plan(plan, plan.location, s)
    except PricingNotConfigured:
        raise UserError("قیمت‌گذاری هنوز توسط مدیریت تنظیم نشده است. لطفاً بعداً تلاش کنید.") from None

    required = quote.hourly * (1 + s.purchase_buffer_hours) if mode == "h" else quote.monthly
    wallet = await wallet_svc.get_wallet(session, user.id, for_update=True)
    if wallet.balance < required:
        raise InsufficientBalance(required, wallet.balance)

    now = utcnow()
    server = Server(
        user_id=user.id,
        provider_id=plan.provider_id,
        plan_id=plan.id,
        location_id=plan.location_id,
        image_id=image.id,
        status=ServerStatus.PENDING,
        billing_mode=mode,
        hourly_price=quote.hourly,
        monthly_price=quote.monthly,
        ip_hourly_price=quote.ip_hourly,
        progress_chat_id=chat_id,
        progress_message_id=message_id,
    )
    session.add(server)
    await session.flush()
    await session.refresh(server, ["plan", "location", "provider", "image", "user"])
    server.name = f"{s.server_name_prefix}-{server.id}"
    await billing.charge_server_unit(session, server, now)  # first unit = the 1-hour minimum
    await jobs.enqueue(session, "create_server", server_id=server.id, user_id=user.id, max_attempts=4)
    return server


async def get_user_server(session: AsyncSession, user: User, server_id: int) -> Server:
    server = await session.get(Server, server_id)
    if server is None or server.user_id != user.id or server.status not in ServerStatus.VISIBLE:
        raise UserError("سرور پیدا نشد.")
    return server


async def ensure_idle(session: AsyncSession, server: Server) -> None:
    if await jobs.has_active_job(session, server_id=server.id):
        raise UserError("⏳ یک عملیات دیگر روی این سرور در حال انجام است. لطفاً چند لحظه صبر کنید.")


def ensure_manageable(server: Server) -> None:
    if server.locked:
        raise UserError("🔒 این سرور به دلیل گزارش تخلف قفل شده است. لطفاً با پشتیبانی تماس بگیرید.")
    if server.status == ServerStatus.SUSPENDED:
        raise UserError("⛔️ این سرور به دلیل کمبود موجودی معلق شده است. پس از شارژ کیف پول خودکار فعال می‌شود.")
    if server.status != ServerStatus.ACTIVE:
        raise UserError("این عملیات در وضعیت فعلی سرور امکان‌پذیر نیست.")


async def request_power(session: AsyncSession, server: Server, action: str) -> None:
    if action not in POWER_ACTIONS:
        raise UserError("عملیات نامعتبر است.")
    ensure_manageable(server)
    await ensure_idle(session, server)
    await jobs.enqueue(session, "power", {"action": action}, server_id=server.id, user_id=server.user_id)


async def request_rebuild(session: AsyncSession, server: Server, image_id: int) -> None:
    ensure_manageable(server)
    image = await session.get(OSImage, image_id)
    if image is None or not image.is_active or image.provider_id != server.provider_id or not image.supports(server.plan.arch):
        raise UserError("این سیستم‌عامل در دسترس نیست.")
    await ensure_idle(session, server)
    await jobs.enqueue(session, "rebuild", {"image_id": image.id}, server_id=server.id, user_id=server.user_id)


def next_ip_change_at(server: Server, cooldown_hours: int) -> datetime | None:
    """None when an IP change is allowed right now."""
    if server.last_ip_change_at is None:
        return None
    ready = server.last_ip_change_at + timedelta(hours=cooldown_hours)
    return ready if ready > utcnow() else None


async def _ensure_ip_change_affordable(session: AsyncSession, server: Server) -> None:
    wallet = await wallet_svc.get_wallet(session, server.user_id)
    if wallet.balance < server.ip_hourly_price:
        raise InsufficientBalance(server.ip_hourly_price, wallet.balance)


async def request_ip_change(session: AsyncSession, server: Server) -> None:
    ensure_manageable(server)
    s = await load_settings(session)
    if (when := next_ip_change_at(server, s.ip_change_cooldown_hours)) is not None:
        hours = max(1, int((when - utcnow()).total_seconds() // 3600) + 1)
        raise UserError(f"⏱ تغییر IP روزی یک بار مجاز است. حدود {hours} ساعت دیگر دوباره امتحان کنید.")
    await _ensure_ip_change_affordable(session, server)
    await ensure_idle(session, server)
    await jobs.enqueue(session, "change_ip", server_id=server.id, user_id=server.user_id)


async def request_purchase_ip_change(session: AsyncSession, server: Server) -> None:
    if server.status != ServerStatus.AWAITING:
        raise UserError("این سرور دیگر در مرحله خرید نیست.")
    s = await load_settings(session)
    if server.purchase_ip_changes >= s.max_purchase_ip_changes:
        raise UserError("سقف تغییر IP در مرحله خرید تمام شده است.")
    await _ensure_ip_change_affordable(session, server)
    await ensure_idle(session, server)
    server.status = ServerStatus.CHECKING
    await jobs.enqueue(session, "purchase_change_ip", server_id=server.id, user_id=server.user_id)


async def deliver(session: AsyncSession, server: Server) -> bool:
    """Mark a server as delivered. Returns False if it was not awaiting confirmation."""
    if server.status != ServerStatus.AWAITING:
        return False
    server.status = ServerStatus.ACTIVE
    server.delivered_at = utcnow()
    return True


async def request_delete(session: AsyncSession, server: Server, *, by_admin: bool = False) -> None:
    if server.status in (ServerStatus.DELETING, ServerStatus.DELETED):
        return
    if server.locked and not by_admin:
        raise UserError("🔒 سرور قفل است و فقط توسط پشتیبانی قابل حذف است.")
    if not by_admin and server.status not in (ServerStatus.ACTIVE, ServerStatus.SUSPENDED, ServerStatus.AWAITING):
        raise UserError("سرور در حال آماده‌سازی است؛ لطفاً پس از اتمام دوباره تلاش کنید.")
    await ensure_idle(session, server)
    server.status = ServerStatus.DELETING  # stops billing immediately
    await jobs.enqueue(session, "delete_server", {"by_admin": by_admin}, server_id=server.id, user_id=server.user_id, max_attempts=6)


async def list_user_servers(session: AsyncSession, user: User) -> list[Server]:
    return list(
        (
            await session.scalars(
                select(Server)
                .where(Server.user_id == user.id, Server.status.in_(ServerStatus.VISIBLE))
                .order_by(Server.id.desc())
            )
        ).all()
    )
