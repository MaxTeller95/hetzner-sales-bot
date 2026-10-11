"""Numbers for the admin dashboard."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AbuseReport,
    AbuseStatus,
    IPAddress,
    IPStatus,
    Job,
    JobStatus,
    Server,
    ServerStatus,
    TopUpRequest,
    TopUpStatus,
    Transaction,
    TxKind,
    User,
    Wallet,
)
from app.services.pricing import HOURS_PER_MONTH, PricingNotConfigured, provider_hourly_cost, provider_monthly_cost, rate_for
from app.services.runtime_settings import load_settings
from app.utils import utcnow

COSTED_STATUSES = ServerStatus.BILLABLE + (ServerStatus.POOLED, ServerStatus.DELETING)


async def overview(session: AsyncSession) -> dict:
    s = await load_settings(session)
    now = utcnow()

    servers = (await session.scalars(select(Server).where(Server.status.in_(COSTED_STATUSES)))).all()
    ips = (await session.scalars(select(IPAddress).where(IPAddress.status.in_(IPStatus.BILLABLE)))).all()

    hourly_cost: dict[str, Decimal] = defaultdict(Decimal)
    monthly_cost: dict[str, Decimal] = defaultdict(Decimal)
    revenue_hourly = 0
    for srv in servers:
        cur = srv.plan.currency
        hourly_cost[cur] += provider_hourly_cost(srv.plan, srv.location)
        monthly_cost[cur] += provider_monthly_cost(srv.plan, srv.location)
        if srv.user_id is not None and srv.status in ServerStatus.BILLABLE:
            revenue_hourly += srv.hourly_price if srv.billing_mode == "h" else srv.monthly_price // HOURS_PER_MONTH
    for ip in ips:
        cur = ip.location.currency
        hourly_cost[cur] += Decimal(ip.location.ipv4_hourly or 0)
        monthly_cost[cur] += min(
            Decimal(ip.location.ipv4_hourly or 0) * HOURS_PER_MONTH, Decimal(ip.location.ipv4_monthly or 0) or Decimal("Infinity")
        )
        revenue_hourly += ip.hourly_price

    cost_monthly_irr = None
    try:
        cost_monthly_irr = int(sum(v * rate_for(cur, s) for cur, v in monthly_cost.items()))
    except PricingNotConfigured:
        pass

    charges_24h = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.kind == TxKind.CHARGE, Transaction.created_at >= now - timedelta(hours=24)
        )
    )
    topups_30d = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.kind == TxKind.TOPUP, Transaction.created_at >= now - timedelta(days=30)
        )
    )

    return {
        "users": await session.scalar(select(func.count(User.id))),
        "active_servers": sum(1 for x in servers if x.user_id is not None and x.status in ServerStatus.BILLABLE),
        "pooled_servers": sum(1 for x in servers if x.status == ServerStatus.POOLED),
        "reserve_ips": len(ips),
        "provider_hourly_cost": {k: float(v) for k, v in hourly_cost.items()},
        "provider_monthly_cost": {k: float(v) for k, v in monthly_cost.items()},
        "provider_monthly_cost_irr": cost_monthly_irr,
        "revenue_hourly": revenue_hourly,
        "revenue_monthly": revenue_hourly * HOURS_PER_MONTH,
        "profit_monthly_irr": (revenue_hourly * HOURS_PER_MONTH - cost_monthly_irr) if cost_monthly_irr is not None else None,
        "charges_24h": -int(charges_24h or 0),
        "topups_30d": int(topups_30d or 0),
        "wallet_total": int(await session.scalar(select(func.coalesce(func.sum(Wallet.balance), 0))) or 0),
        "negative_wallets": await session.scalar(select(func.count(Wallet.id)).where(Wallet.balance < 0)),
        "pending_topups": await session.scalar(
            select(func.count(TopUpRequest.id)).where(TopUpRequest.status == TopUpStatus.PENDING)
        ),
        "open_abuse": await session.scalar(
            select(func.count(AbuseReport.id)).where(AbuseReport.status == AbuseStatus.OPEN)
        ),
        "failed_jobs_24h": await session.scalar(
            select(func.count(Job.id)).where(Job.status == JobStatus.FAILED, Job.updated_at >= now - timedelta(hours=24))
        ),
        "pending_jobs": await session.scalar(
            select(func.count(Job.id)).where(Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)))
        ),
        "exchange_rate_eur": s.exchange_rate_eur,
        "profit_margin_percent": s.profit_margin_percent,
        "generated_at": now.isoformat(timespec="seconds") + "Z",
    }
