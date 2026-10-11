"""Management commands.

    python -m app.cli init       # create the Hetzner provider from HETZNER_API_TOKEN and import its catalog
    python -m app.cli sync       # re-import locations/plans/prices/images of all providers
    python -m app.cli demo       # add the fake provider (no real servers) for trying the bot locally
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.models import Provider
from app.db.session import SessionLocal
from app.providers.registry import close_all
from app.services import catalog


async def init() -> None:
    async with SessionLocal() as session:
        row = await catalog.ensure_default_provider(session)
        if row is None:
            print("A provider already exists or HETZNER_API_TOKEN is empty; nothing created.")
        else:
            print(f"Created provider {row.code}.")
        await session.commit()
    await sync()


async def sync() -> None:
    async with SessionLocal() as session:
        for row in (await session.scalars(select(Provider).where(Provider.is_active.is_(True)))).all():
            counts = await catalog.sync_provider(session, row)
            print(f"{row.code}: {counts}")
        await session.commit()


async def demo() -> None:
    async with SessionLocal() as session:
        if not await session.scalar(select(Provider).where(Provider.code == "demo")):
            session.add(Provider(code="demo", kind="fake", name="Demo (fake)", options={}))
            await session.commit()
            print("Added fake provider 'demo'.")
    await sync()


async def main(cmd: str) -> None:
    try:
        await {"init": init, "sync": sync, "demo": demo}[cmd]()
    finally:
        await close_all()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("init", "sync", "demo"):
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
