"""Durable job queue on top of the `jobs` table."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Job, JobStatus
from app.utils import utcnow


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict | None = None,
    *,
    server_id: int | None = None,
    ip_id: int | None = None,
    user_id: int | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    job = Job(
        kind=kind,
        payload=payload or {},
        server_id=server_id,
        ip_id=ip_id,
        user_id=user_id,
        run_at=run_at or utcnow(),
        max_attempts=max_attempts,
        status=JobStatus.PENDING,
    )
    session.add(job)
    await session.flush()
    return job


async def has_active_job(session: AsyncSession, *, server_id: int | None = None, ip_id: int | None = None) -> bool:
    q = select(Job.id).where(Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)))
    if server_id is not None:
        q = q.where(Job.server_id == server_id)
    if ip_id is not None:
        q = q.where(Job.ip_id == ip_id)
    return (await session.scalar(q.limit(1))) is not None


async def claim_next(session: AsyncSession) -> Job | None:
    q = (
        select(Job)
        .where(Job.status == JobStatus.PENDING, Job.run_at <= utcnow())
        .order_by(Job.run_at, Job.id)
        .limit(1)
    )
    if not get_settings().is_sqlite:
        q = q.with_for_update(skip_locked=True)
    job = await session.scalar(q)
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    await session.commit()
    return job


async def reset_stale(session: AsyncSession) -> int:
    """Jobs left RUNNING by a crashed worker go back to the queue."""
    res = await session.execute(
        update(Job).where(Job.status == JobStatus.RUNNING).values(status=JobStatus.PENDING, run_at=utcnow())
    )
    await session.commit()
    return res.rowcount or 0
