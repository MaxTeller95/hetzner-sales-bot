"""Synchronise locations, plans and OS images from a provider into the database."""
from __future__ import annotations

import fnmatch
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Location, OSImage, Plan, Provider
from app.providers.registry import get_provider
from app.utils import encrypt, utcnow

log = logging.getLogger(__name__)

LOCATION_NAMES_FA = {
    "fsn1": "فالکن‌اشتاین، آلمان",
    "nbg1": "نورنبرگ، آلمان",
    "hel1": "هلسینکی، فنلاند",
    "ash": "اشبرن، آمریکا",
    "hil": "هیلزبورو، آمریکا",
    "sin": "سنگاپور",
}
# Images enabled automatically the first time they are seen.
DEFAULT_IMAGE_PATTERNS = ("ubuntu-24.04", "ubuntu-22.04", "debian-12", "debian-13", "rocky-9", "alma-9", "centos-stream-9")


async def sync_provider(session: AsyncSession, provider_row: Provider) -> dict[str, int]:
    impl = get_provider(provider_row)
    p_locations = await impl.list_locations()
    p_plans = await impl.list_plans()
    p_images = await impl.list_images()

    existing_locs = {
        loc.code: loc
        for loc in (await session.scalars(select(Location).where(Location.provider_id == provider_row.id))).all()
    }
    for i, pl in enumerate(p_locations):
        loc = existing_locs.get(pl.code)
        if loc is None:
            loc = Location(
                provider_id=provider_row.id,
                code=pl.code,
                name=LOCATION_NAMES_FA.get(pl.code, pl.name),
                is_active=True,
                sort_order=i,
            )
            session.add(loc)
            existing_locs[pl.code] = loc
        loc.country, loc.city, loc.currency = pl.country, pl.city, pl.currency
        loc.ipv4_hourly, loc.ipv4_monthly = pl.ipv4_hourly, pl.ipv4_monthly
    await session.flush()

    existing_plans = {
        (p.location_id, p.code): p
        for p in (await session.scalars(select(Plan).where(Plan.provider_id == provider_row.id))).all()
    }
    seen: set[tuple[int, str]] = set()
    for pp in p_plans:
        for loc_code, (hourly, monthly) in pp.prices.items():
            loc = existing_locs.get(loc_code)
            if loc is None:
                continue
            key = (loc.id, pp.code)
            seen.add(key)
            plan = existing_plans.get(key)
            if plan is None:
                plan = Plan(provider_id=provider_row.id, location_id=loc.id, code=pp.code, is_active=True, sort_order=0)
                session.add(plan)
                existing_plans[key] = plan
            plan.name = pp.name
            plan.cores, plan.memory_gb, plan.disk_gb = pp.cores, pp.memory_gb, pp.disk_gb
            plan.cpu_type, plan.arch, plan.traffic_tb, plan.currency = pp.cpu_type, pp.arch, pp.traffic_tb, pp.currency
            plan.base_hourly, plan.base_monthly = hourly, monthly
            plan.is_available = loc_code in pp.available_in
    for key, plan in existing_plans.items():
        if key not in seen:
            plan.is_available = False

    existing_images = {
        i.code: i for i in (await session.scalars(select(OSImage).where(OSImage.provider_id == provider_row.id))).all()
    }
    for idx, pi in enumerate(sorted(p_images, key=lambda x: x.code)):
        img = existing_images.get(pi.code)
        if img is None:
            img = OSImage(
                provider_id=provider_row.id,
                code=pi.code,
                is_active=any(fnmatch.fnmatch(pi.code, pat) for pat in DEFAULT_IMAGE_PATTERNS),
                sort_order=idx,
            )
            session.add(img)
        img.name, img.flavor, img.arch = pi.name, pi.flavor, pi.arch

    provider_row.last_synced_at = utcnow()
    await session.flush()
    counts = {"locations": len(p_locations), "plans": len(seen), "images": len(p_images)}
    log.info("synced provider %s: %s", provider_row.code, counts)
    return counts


async def ensure_default_provider(session: AsyncSession) -> Provider | None:
    """Create the Hetzner provider row from HETZNER_API_TOKEN if no provider exists yet."""
    if await session.scalar(select(Provider.id).limit(1)):
        return None
    token = get_settings().hetzner_api_token
    if not token:
        return None
    row = Provider(code="hetzner", kind="hetzner", name="Hetzner", api_token_enc=encrypt(token), options={})
    session.add(row)
    await session.flush()
    return row
