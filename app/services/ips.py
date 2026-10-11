"""Reserve IPs: spare IPv4s a customer keeps and can swap onto their servers."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IPAddress, IPStatus, Location, Server, ServerStatus, User
from app.services import billing, jobs
from app.services import wallet as wallet_svc
from app.services.errors import InsufficientBalance, UserError
from app.services.pricing import PricingNotConfigured, quote_reserve_ip
from app.services.runtime_settings import load_settings
from app.services.servers import ensure_idle, ensure_manageable, next_ip_change_at
from app.utils import utcnow


async def list_user_ips(session: AsyncSession, user: User) -> list[IPAddress]:
    return list(
        (
            await session.scalars(
                select(IPAddress)
                .where(IPAddress.user_id == user.id, IPAddress.status.in_(IPStatus.VISIBLE))
                .order_by(IPAddress.id)
            )
        ).all()
    )


async def get_user_ip(session: AsyncSession, user: User, ip_id: int) -> IPAddress:
    ip = await session.get(IPAddress, ip_id)
    if ip is None or ip.user_id != user.id or ip.status not in IPStatus.VISIBLE:
        raise UserError("IP پیدا نشد.")
    return ip


async def create_reserve_ip(session: AsyncSession, user: User, location_id: int) -> IPAddress:
    s = await load_settings(session)
    count = await session.scalar(
        select(func.count(IPAddress.id)).where(IPAddress.user_id == user.id, IPAddress.status.in_(IPStatus.VISIBLE))
    )
    if count >= s.max_reserve_ips:
        raise UserError(f"حداکثر {s.max_reserve_ips} IP رزرو مجاز است.")
    location = await session.get(Location, location_id)
    if location is None or not location.is_active or not location.provider.is_active:
        raise UserError("این موقعیت در دسترس نیست.")
    try:
        price = quote_reserve_ip(location, s)
    except PricingNotConfigured:
        raise UserError("قیمت‌گذاری هنوز تنظیم نشده است.") from None
    required = price * (1 + s.purchase_buffer_hours)
    wallet = await wallet_svc.get_wallet(session, user.id, for_update=True)
    if wallet.balance < required:
        raise InsufficientBalance(required, wallet.balance)

    ip = IPAddress(
        user_id=user.id,
        provider_id=location.provider_id,
        location_id=location.id,
        status=IPStatus.CREATING,
        hourly_price=price,
    )
    session.add(ip)
    await session.flush()
    await session.refresh(ip, ["location", "provider", "user"])
    await billing.charge_ip_unit(session, ip, utcnow())
    await jobs.enqueue(session, "create_reserve_ip", ip_id=ip.id, user_id=user.id)
    return ip


async def request_delete_ip(session: AsyncSession, ip: IPAddress) -> None:
    if ip.status != IPStatus.ACTIVE:
        raise UserError("این IP در حال حاضر قابل حذف نیست.")
    if await jobs.has_active_job(session, ip_id=ip.id):
        raise UserError("⏳ عملیات دیگری روی این IP در حال انجام است.")
    ip.status = IPStatus.DELETING
    await jobs.enqueue(session, "delete_reserve_ip", ip_id=ip.id, user_id=ip.user_id, max_attempts=6)


async def swappable_servers(session: AsyncSession, ip: IPAddress) -> list[Server]:
    q = select(Server).where(
        Server.user_id == ip.user_id,
        Server.status == ServerStatus.ACTIVE,
        Server.location_id == ip.location_id,
        Server.locked.is_(False),
    )
    servers = (await session.scalars(q)).all()
    # A primary IP can only be attached to a server in the same location (`datacenter` holds the
    # provider's placement code, which for Hetzner is the location since the 2026 API change).
    return [s for s in servers if not ip.datacenter or not s.datacenter or s.datacenter == ip.datacenter]


async def request_swap(session: AsyncSession, ip: IPAddress, server: Server) -> None:
    ensure_manageable(server)
    if ip.status != IPStatus.ACTIVE:
        raise UserError("این IP هنوز آماده نیست.")
    if server.id not in {s.id for s in await swappable_servers(session, ip)}:
        raise UserError("این IP فقط روی سرورهای همان دیتاسنتر قابل استفاده است.")
    s = await load_settings(session)
    if next_ip_change_at(server, s.ip_change_cooldown_hours) is not None:
        raise UserError("⏱ تغییر IP این سرور روزی یک بار مجاز است.")
    await ensure_idle(session, server)
    if await jobs.has_active_job(session, ip_id=ip.id):
        raise UserError("⏳ عملیات دیگری روی این IP در حال انجام است.")
    await jobs.enqueue(session, "swap_ip", server_id=server.id, ip_id=ip.id, user_id=ip.user_id)
