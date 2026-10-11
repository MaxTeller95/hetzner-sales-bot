from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.db.models import Job, JobStatus
from app.providers.base import ProviderError
from app.services import jobs as jobs_svc
from app.utils import utcnow
from app.worker.context import WorkerContext
from app.worker.handlers import FAILURE_HANDLERS, HANDLERS, JobFailed, notify_generic_failure

log = logging.getLogger(__name__)


def is_retryable(error: Exception) -> bool:
    if isinstance(error, JobFailed):
        return False
    if isinstance(error, ProviderError):
        return error.retryable
    return True  # network blips, DB hiccups, bugs we may have fixed on restart


async def run_job(ctx: WorkerContext, job: Job) -> None:
    handler = HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise JobFailed(f"no handler for job kind {job.kind!r}")
        await handler(ctx, job)
    except Exception as error:  # noqa: BLE001 - the job runner is the last line of defence
        retry = job.attempts < job.max_attempts and is_retryable(error)
        log.warning("job #%s %s failed (attempt %s, retry=%s): %r", job.id, job.kind, job.attempts, retry, error)
        async with ctx.sm() as session:
            row = await session.get(Job, job.id)
            row.last_error = repr(error)[:4000]
            if retry:
                row.status = JobStatus.PENDING
                row.run_at = utcnow() + timedelta(seconds=min(300, 15 * 2 ** (job.attempts - 1)))
            else:
                row.status = JobStatus.FAILED
                row.finished_at = utcnow()
            await session.commit()
        if not retry:
            try:
                on_fail = FAILURE_HANDLERS.get(job.kind)
                if on_fail:
                    await on_fail(ctx, job, error)
                else:
                    await notify_generic_failure(ctx, job)
            except Exception:
                log.exception("failure handler for job #%s crashed", job.id)
            await ctx.notifier.send_admins(f"⚠️ Job #{job.id} <code>{job.kind}</code> failed: {str(error)[:300]}")
        return

    async with ctx.sm() as session:
        row = await session.get(Job, job.id)
        row.status = JobStatus.DONE
        row.finished_at = utcnow()
        await session.commit()


async def drain_once(ctx: WorkerContext) -> int:
    """Run all currently due jobs sequentially (used by tests and the CLI)."""
    count = 0
    while True:
        async with ctx.sm() as session:
            job = await jobs_svc.claim_next(session)
        if job is None:
            return count
        await run_job(ctx, job)
        count += 1


async def job_loop(ctx: WorkerContext, concurrency: int) -> None:
    sem = asyncio.Semaphore(concurrency)
    running: set[asyncio.Task] = set()
    while True:
        await sem.acquire()
        try:
            async with ctx.sm() as session:
                job = await jobs_svc.claim_next(session)
        except Exception:
            log.exception("claiming job failed")
            job = None
        if job is None:
            sem.release()
            await asyncio.sleep(2)
            continue

        async def _run(j=job):
            try:
                await run_job(ctx, j)
            finally:
                sem.release()

        task = asyncio.create_task(_run())
        running.add(task)
        task.add_done_callback(running.discard)
