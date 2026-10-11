"""Usage billing.

Every resource is charged one unit *in advance* when it is created (that is the 1-hour
minimum) and again each time `next_charge_at` passes. Deleting a resource simply stops
future charges, so a server deleted after 5 minutes costs exactly one hour.

Hourly servers: unit = 1 hour at `hourly_price`.
Monthly servers: unit = 720 hours at `monthly_price`.
Reserve IPs: unit = 1 hour at `hourly_price`.
IP changes: one-off charge of `ip_hourly_price` (the discarded IP's minimum hour).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BillingEvent, IPAddress, IPStatus, Server, ServerStatus, Transaction, TxKind
from app.services import wallet as wallet_svc
from app.utils import utcnow

log = logging.getLogger(__name__)

PERIODS = {"h": timedelta(hours=1), "m": timedelta(hours=720)}
MAX_CATCHUP_UNITS = 800


async def charge(
    session: AsyncSession,
    *,
    user_id: int,
    amount: int,
    description: str,
    resource_type: str,
    period_start: datetime,
    period_end: datetime,
    hours: int,
    provider_cost: Decimal = Decimal(0),
    currency: str = "EUR",
    server_id: int | None = None,
    ip_id: int | None = None,
) -> Transaction:
    tx = await wallet_svc.apply(
        session, user_id, -amount, TxKind.CHARGE, description, server_id=server_id, ip_id=ip_id
    )
    session.add(
        BillingEvent(
            user_id=user_id,
            resource_type=resource_type,
            server_id=server_id,
            ip_id=ip_id,
            period_start=period_start,
            period_end=period_end,
            hours=hours,
            amount=amount,
            provider_cost=provider_cost,
            currency=currency,
            transaction_id=tx.id,
        )
    )
    return tx


def server_unit(server: Server) -> tuple[int, Decimal, int]:
    """(customer amount, provider cost, hours) for one billing unit of this server."""
    plan, loc = server.plan, server.location
    if server.billing_mode == "m":
        cost = Decimal(plan.base_monthly) + Decimal(loc.ipv4_monthly or 0)
        return server.monthly_price, cost, 720
    cost = Decimal(plan.base_hourly) + Decimal(loc.ipv4_hourly or 0)
    return server.hourly_price, cost, 1


async def charge_server_unit(session: AsyncSession, server: Server, start: datetime) -> None:
    amount, cost, hours = server_unit(server)
    end = start + PERIODS[server.billing_mode]
    label = "ماهانه" if server.billing_mode == "m" else "ساعتی"
    await charge(
        session,
        user_id=server.user_id,
        amount=amount,
        description=f"هزینه {label} سرور {server.name}",
        resource_type="server",
        period_start=start,
        period_end=end,
        hours=hours,
        provider_cost=cost,
        currency=server.plan.currency,
        server_id=server.id,
    )
    server.next_charge_at = end


async def charge_ip_unit(session: AsyncSession, ip: IPAddress, start: datetime) -> None:
    end = start + PERIODS["h"]
    await charge(
        session,
        user_id=ip.user_id,
        amount=ip.hourly_price,
        description=f"هزینه ساعتی IP رزرو {ip.address or ''}".strip(),
        resource_type="ip",
        period_start=start,
        period_end=end,
        hours=1,
        provider_cost=Decimal(ip.location.ipv4_hourly or 0),
        currency=ip.location.currency,
        ip_id=ip.id,
    )
    ip.next_charge_at = end


async def charge_ip_change(session: AsyncSession, server: Server, old_ip: str | None) -> None:
    if not server.ip_hourly_price or server.user_id is None:
        return
    now = utcnow()
    await charge(
        session,
        user_id=server.user_id,
        amount=server.ip_hourly_price,
        description=f"حداقل یک ساعت هزینه IP قبلی ({old_ip or '-'}) سرور {server.name}",
        resource_type="ip_change",
        period_start=now,
        period_end=now + PERIODS["h"],
        hours=1,
        provider_cost=Decimal(server.location.ipv4_hourly or 0),
        currency=server.location.currency,
        server_id=server.id,
    )


async def refund_server(session: AsyncSession, server: Server, reason: str) -> int:
    """Credit back everything charged for this server (minus earlier refunds)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.server_id == server.id, Transaction.kind.in_((TxKind.CHARGE, TxKind.REFUND))
        )
    )
    amount = -int(total or 0)
    if amount > 0 and server.user_id:
        await wallet_svc.apply(session, server.user_id, amount, TxKind.REFUND, reason, server_id=server.id)
    return max(amount, 0)


async def refund_ip(session: AsyncSession, ip: IPAddress, reason: str) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.ip_id == ip.id, Transaction.kind.in_((TxKind.CHARGE, TxKind.REFUND))
        )
    )
    amount = -int(total or 0)
    if amount > 0:
        await wallet_svc.apply(session, ip.user_id, amount, TxKind.REFUND, reason, ip_id=ip.id)
    return max(amount, 0)


async def bill_due(sm: async_sessionmaker, now: datetime | None = None) -> int:
    """Charge every resource whose next billing unit has started. Returns units charged."""
    now = now or utcnow()
    units = 0
    async with sm() as session:
        server_ids = (
            await session.scalars(
                select(Server.id).where(
                    Server.status.in_(ServerStatus.BILLABLE),
                    Server.user_id.is_not(None),
                    Server.next_charge_at <= now,
                )
            )
        ).all()
        ip_ids = (
            await session.scalars(
                select(IPAddress.id).where(IPAddress.status.in_(IPStatus.BILLABLE), IPAddress.next_charge_at <= now)
            )
        ).all()

    for sid in server_ids:
        async with sm() as session:
            server = await session.get(Server, sid, with_for_update=True)
            if not server or server.status not in ServerStatus.BILLABLE or not server.next_charge_at:
                continue
            n = 0
            while server.next_charge_at <= now and n < MAX_CATCHUP_UNITS:
                await charge_server_unit(session, server, server.next_charge_at)
                n += 1
            await session.commit()
            units += n

    for iid in ip_ids:
        async with sm() as session:
            ip = await session.get(IPAddress, iid, with_for_update=True)
            if not ip or ip.status not in IPStatus.BILLABLE or not ip.next_charge_at:
                continue
            n = 0
            while ip.next_charge_at <= now and n < MAX_CATCHUP_UNITS:
                await charge_ip_unit(session, ip, ip.next_charge_at)
                n += 1
            await session.commit()
            units += n

    if units:
        log.info("billed %d units", units)
    return units
