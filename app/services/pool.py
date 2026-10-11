"""Pre-created ("pooled") servers for high-demand cheap plans.

Cheap Hetzner plans are often sold out. When the admin sets `pool_target` on a plan, the
worker keeps that many servers created and unowned. A purchase of that plan takes one from
the pool (and reinstalls the chosen OS) instead of creating a new server.
Pool servers are billed to the admin by the provider, not to any customer.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import OSImage, Plan, Server, ServerStatus
from app.services import jobs
from app.services.runtime_settings import load_settings

POOL_FILLING = (ServerStatus.PENDING, ServerStatus.CREATING, ServerStatus.POOLED)


async def claim(session: AsyncSession, server: Server) -> Server | None:
    q = (
        select(Server)
        .where(Server.plan_id == server.plan_id, Server.status == ServerStatus.POOLED, Server.user_id.is_(None))
        .order_by(Server.id)
        .limit(1)
    )
    if not get_settings().is_sqlite:
        q = q.with_for_update(skip_locked=True)
    return await session.scalar(q)


async def pool_image(session: AsyncSession, plan: Plan) -> OSImage | None:
    s = await load_settings(session)
    images = (
        await session.scalars(select(OSImage).where(OSImage.provider_id == plan.provider_id).order_by(OSImage.sort_order))
    ).all()
    preferred = [i for i in images if i.code == s.pool_default_image and i.supports(plan.arch)]
    fallback = [i for i in images if i.is_active and i.supports(plan.arch)]
    return (preferred or fallback or [None])[0]


async def create_pool_server(session: AsyncSession, plan: Plan) -> Server | None:
    image = await pool_image(session, plan)
    if image is None:
        return None
    s = await load_settings(session)
    server = Server(
        user_id=None,
        provider_id=plan.provider_id,
        plan_id=plan.id,
        location_id=plan.location_id,
        image_id=image.id,
        status=ServerStatus.PENDING,
        billing_mode="h",
    )
    session.add(server)
    await session.flush()
    server.name = f"{s.server_name_prefix}-pool-{server.id}"
    await jobs.enqueue(session, "pool_create", server_id=server.id, max_attempts=3)
    return server


async def maintain(session: AsyncSession) -> int:
    """Queue creation of pool servers for every plan below its target. Returns servers queued."""
    plans = (await session.scalars(select(Plan).where(Plan.pool_target > 0, Plan.is_active.is_(True)))).all()
    queued = 0
    for plan in plans:
        have = await session.scalar(
            select(func.count(Server.id)).where(
                Server.plan_id == plan.id, Server.user_id.is_(None), Server.status.in_(POOL_FILLING)
            )
        )
        for _ in range(max(0, plan.pool_target - (have or 0))):
            if await create_pool_server(session, plan):
                queued += 1
    return queued
