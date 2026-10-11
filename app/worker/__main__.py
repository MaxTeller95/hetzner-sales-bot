"""Worker process: runs jobs and periodic tasks.  Usage: python -m app.worker"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.db.session import SessionLocal
from app.providers.registry import close_all
from app.services import jobs
from app.services.connectivity import CheckHostChecker
from app.services.notifier import TelegramNotifier, create_bot
from app.worker.context import WorkerContext
from app.worker.runner import job_loop
from app.worker.tasks import PERIODIC, periodic

log = logging.getLogger("worker")


async def main() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg.check_production()
    bot = create_bot()
    ctx = WorkerContext(sm=SessionLocal, notifier=TelegramNotifier(bot), checker=CheckHostChecker())

    async with SessionLocal() as session:
        reset = await jobs.reset_stale(session)
    if reset:
        log.info("requeued %d jobs left running by a previous worker", reset)

    tasks = [asyncio.create_task(job_loop(ctx, cfg.worker_concurrency))]
    tasks += [asyncio.create_task(periodic(name, interval, fn, ctx)) for name, interval, fn in PERIODIC]
    log.info("worker started")
    try:
        await asyncio.gather(*tasks)
    finally:
        await close_all()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
