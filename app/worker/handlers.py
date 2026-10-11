"""Job handlers. Each handler must be safe to retry after a partial failure."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.bot import keyboards as K
from app.bot import texts as T
from app.db.models import IPAddress, IPStatus, Job, OSImage, Plan, Provider, Server, ServerStatus
from app.providers.base import OutOfStock, ProviderError, ServerInfo
from app.services import billing, catalog
from app.services import pool as pool_svc
from app.services.connectivity import ConnectivityUnavailable
from app.services.runtime_settings import load_settings
from app.utils import decrypt, encrypt, utcnow
from app.worker.context import WorkerContext

log = logging.getLogger(__name__)


class JobFailed(Exception):
    """Permanent failure: do not retry."""


def labels_for(server: Server) -> dict[str, str]:
    labels = {"managed-by": "hetzbot", "hb-id": str(server.id)}
    if server.user_id is None:
        labels["hb-pool"] = "1"
    return labels


def apply_info(server: Server, info: ServerInfo) -> None:
    server.provider_server_id = info.id
    server.ipv4 = info.ipv4
    server.ipv6 = info.ipv6
    server.provider_ip_id = info.ipv4_id
    server.datacenter = info.datacenter
    server.power_status = info.status


async def _get_server(session, job: Job) -> Server:
    server = await session.get(Server, job.server_id)
    if server is None:
        raise JobFailed(f"server {job.server_id} not found")
    return server


async def _change_ip(ctx: WorkerContext, session, server: Server, *, purchase: bool) -> str | None:
    provider = ctx.provider_for(server.provider)
    old = server.ipv4
    info = await provider.change_primary_ipv4(server.provider_server_id)
    server.ipv4 = info.address
    server.provider_ip_id = info.id
    if purchase:
        server.purchase_ip_changes += 1
    else:
        server.last_ip_change_at = utcnow()
    await billing.charge_ip_change(session, server, old)
    return old


# ---- purchase --------------------------------------------------------------------
async def h_create_server(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        resume_check = server.status == ServerStatus.CHECKING  # retry after a failure during the check
        if not resume_check and server.status not in (ServerStatus.PENDING, ServerStatus.CREATING):
            return
    if resume_check:
        await run_purchase_check(ctx, job.server_id)
        return

    async with ctx.sm() as session:
        server = await _get_server(session, job)
        server.status = ServerStatus.CREATING
        await session.commit()
        await ctx.notifier.edit(server.progress_chat_id, server.progress_message_id, T.progress_creating(server))

        provider = ctx.provider_for(server.provider)
        password: str | None = None
        if not server.provider_server_id:
            pooled = await pool_svc.claim(session, server)
            if pooled is not None:
                server.provider_server_id = pooled.provider_server_id
                pooled.status = ServerStatus.TRANSFERRED
                pooled.failure_reason = f"assigned to server #{server.id}"
                await session.commit()  # persist the claim before the (slow) rebuild
                log.info("server %s taken from pool (%s)", server.id, pooled.id)
                password = await provider.rebuild(server.provider_server_id, server.image.code)
            else:
                existing = await provider.find_servers({"managed-by": "hetzbot", "hb-id": str(server.id)})
                if existing:  # a previous attempt created it but crashed before saving
                    server.provider_server_id = existing[0].id
                    password = await provider.rebuild(existing[0].id, server.image.code)
                else:
                    created = await provider.create_server(
                        name=server.name,
                        plan_code=server.plan.code,
                        location_code=server.location.code,
                        image_code=server.image.code,
                        labels=labels_for(server),
                    )
                    server.provider_server_id = created.server.id
                    password = created.root_password
        elif not server.root_password_enc:
            password = await provider.rebuild(server.provider_server_id, server.image.code)

        info = await provider.get_server(server.provider_server_id)
        if info is None:
            raise ProviderError("server disappeared right after creation", retryable=True)
        apply_info(server, info)
        if password:
            server.root_password_enc = encrypt(password)
        server.status = ServerStatus.CHECKING
        await session.commit()
    await run_purchase_check(ctx, job.server_id)


async def run_purchase_check(ctx: WorkerContext, server_id: int) -> None:
    """Test the IP from Iran; swap IPs automatically on failure; hand over when it passes."""
    while True:
        async with ctx.sm() as session:
            s = await load_settings(session)
            server = await session.get(Server, server_id)
            if server is None or server.status != ServerStatus.CHECKING:
                return
            if not s.connectivity_enabled:
                result = None
                skipped = True
            else:
                skipped = False
                await ctx.notifier.edit(server.progress_chat_id, server.progress_message_id, T.progress_checking(server))
                await ctx.sleep(s.connectivity_initial_delay)
                try:
                    result = await ctx.checker.check(server.ipv4, s)
                except ConnectivityUnavailable as e:
                    log.warning("connectivity service unavailable for server %s: %s", server.id, e)
                    result = None
                    await ctx.notifier.send_admins(f"⚠️ check-host unavailable for {server.name} ({server.ipv4}): {e}")

            await session.refresh(server)
            if server.status != ServerStatus.CHECKING:
                return

            if result is None or result.passed:
                server.status = ServerStatus.AWAITING
                server.ready_at = utcnow()
                server.check_passed = None if result is None else True
                server.check_summary = None if result is None else f"{result.summary()}\n{result.details()}"
                changes_left = max(0, s.max_purchase_ip_changes - server.purchase_ip_changes)
                await session.commit()
                if skipped:
                    await deliver_and_notify(ctx, server_id)
                    return
                await send_ready(ctx, server, changes_left)
                return

            server.check_passed = False
            server.check_summary = f"{result.summary()}\n{result.details()}"
            if server.purchase_ip_changes < s.max_purchase_ip_changes:
                await ctx.notifier.edit(
                    server.progress_chat_id,
                    server.progress_message_id,
                    T.progress_ip_retry(server.ipv4, server.purchase_ip_changes + 1, s.max_purchase_ip_changes),
                )
                await _change_ip(ctx, session, server, purchase=True)
                await session.commit()
                continue

            # Out of attempts: cancel the order.
            provider = ctx.provider_for(server.provider)
            await provider.delete_server(server.provider_server_id)
            server.status = ServerStatus.FAILED
            server.deleted_at = utcnow()
            server.failure_reason = "connectivity check failed"
            refunded = 0
            if s.refund_failed_purchase:
                refunded = await billing.refund_server(session, server, f"بازگشت وجه سفارش ناموفق {server.name}")
            await session.commit()
            await ctx.notifier.edit(
                server.progress_chat_id, server.progress_message_id, T.purchase_check_failed(server, refunded)
            )
            await ctx.notifier.send_admins(f"ℹ️ Order {server.name} cancelled: IP never passed the Iran check.")
            return


async def send_ready(ctx: WorkerContext, server: Server, changes_left: int) -> None:
    summary, details = None, ""
    if server.check_summary and server.check_passed:
        summary, _, details = server.check_summary.partition("\n")
    await ctx.notifier.edit(
        server.progress_chat_id,
        server.progress_message_id,
        T.ready_for_confirm(server, details, summary, changes_left),
        K.purchase_ready(server, changes_left),
    )


async def deliver_and_notify(ctx: WorkerContext, server_id: int) -> bool:
    from app.services.servers import deliver

    async with ctx.sm() as session:
        server = await session.get(Server, server_id)
        if server is None or not await deliver(session, server):
            return False
        await session.commit()
        password = decrypt(server.root_password_enc)
        await ctx.notifier.edit(
            server.progress_chat_id,
            server.progress_message_id,
            T.delivery(server, password),
            K.delivered(server, password),
        )
    return True


async def h_purchase_change_ip(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        if server.status != ServerStatus.CHECKING:
            return
        s = await load_settings(session)
        await ctx.notifier.edit(
            server.progress_chat_id,
            server.progress_message_id,
            f"🔄 در حال دریافت IP جدید ({server.purchase_ip_changes + 1}/{s.max_purchase_ip_changes})...",
        )
        await _change_ip(ctx, session, server, purchase=True)
        await session.commit()
    await run_purchase_check(ctx, job.server_id)


async def f_create_server(ctx: WorkerContext, job: Job, error: Exception) -> None:
    async with ctx.sm() as session:
        server = await session.get(Server, job.server_id)
        if server is None or server.status in (ServerStatus.DELETED, ServerStatus.FAILED):
            return
        if server.provider_server_id:
            try:
                await ctx.provider_for(server.provider).delete_server(server.provider_server_id)
            except ProviderError:
                log.exception("cleanup of failed server %s failed", server.id)
        server.status = ServerStatus.FAILED
        server.deleted_at = utcnow()
        server.failure_reason = str(error)[:255]
        refunded = await billing.refund_server(session, server, f"بازگشت وجه سفارش ناموفق {server.name}")
        if isinstance(error, OutOfStock):
            plan = await session.get(Plan, server.plan_id)
            plan.is_available = False
        await session.commit()
        text = T.purchase_out_of_stock(refunded) if isinstance(error, OutOfStock) else T.purchase_failed(server, refunded)
        await ctx.notifier.edit(server.progress_chat_id, server.progress_message_id, text)
        if isinstance(error, OutOfStock):
            await ctx.notifier.send_admins(f"📦 {server.plan.name} @ {server.location.code} is out of stock; plan hidden until next sync.")


# ---- server management -------------------------------------------------------------
async def h_power(ctx: WorkerContext, job: Job) -> None:
    action = job.payload.get("action")
    silent = job.payload.get("silent", False)
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        if server.status not in ServerStatus.LIVE or not server.provider_server_id:
            return
        provider = ctx.provider_for(server.provider)
        pid = server.provider_server_id
        if action == "reboot":
            await provider.reboot(pid)
        elif action == "poweron":
            await provider.power_on(pid)
        elif action == "poweroff":
            await provider.power_off(pid)
        elif action == "shutdown":
            await provider.shutdown(pid)
            for _ in range(12):
                await ctx.sleep(5)
                info = await provider.get_server(pid)
                if info and info.status == "off":
                    break
            else:
                await provider.power_off(pid)
        else:
            raise JobFailed(f"unknown power action {action}")
        info = await provider.get_server(pid)
        if info:
            server.power_status = info.status
        await session.commit()
        if not silent and server.user:
            await ctx.notifier.send(
                server.user.telegram_id, T.POWER_DONE[action].format(name=server.name), K.to_server(server.id)
            )


async def h_rebuild(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        image = await session.get(OSImage, job.payload["image_id"])
        if server.status != ServerStatus.ACTIVE or image is None:
            return
        password = await ctx.provider_for(server.provider).rebuild(server.provider_server_id, image.code)
        server.image_id = image.id
        if password:
            server.root_password_enc = encrypt(password)
        await session.commit()
        await session.refresh(server, ["image"])
        if server.user:
            await ctx.notifier.send(server.user.telegram_id, T.rebuild_done(server, password), K.delivered(server, password))


async def h_change_ip(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        if server.status != ServerStatus.ACTIVE:
            return
        s = await load_settings(session)
        old = await _change_ip(ctx, session, server, purchase=False)
        await session.commit()
        summary = None
        if s.connectivity_enabled:
            await ctx.sleep(s.connectivity_initial_delay)
            try:
                result = await ctx.checker.check(server.ipv4, s)
                summary = f"{result.summary()} {'✅' if result.passed else '⚠️'}"
            except ConnectivityUnavailable:
                summary = None
        if server.user:
            await ctx.notifier.send(server.user.telegram_id, T.ip_changed(server, old or "-", summary), K.to_server(server.id))


async def h_delete_server(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        if server.status == ServerStatus.DELETED:
            return
        if server.provider_server_id:
            await ctx.provider_for(server.provider).delete_server(server.provider_server_id)
        server.status = ServerStatus.DELETED
        server.deleted_at = utcnow()
        await session.commit()
        if server.user and not job.payload.get("silent"):
            await ctx.notifier.send(server.user.telegram_id, T.server_deleted(server))


# ---- reserve IPs -------------------------------------------------------------------
async def _get_ip(session, job: Job) -> IPAddress:
    ip = await session.get(IPAddress, job.ip_id)
    if ip is None:
        raise JobFailed(f"ip {job.ip_id} not found")
    return ip


async def h_create_reserve_ip(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        ip = await _get_ip(session, job)
        if ip.status != IPStatus.CREATING or ip.provider_ip_id:
            return
        s = await load_settings(session)
        info = await ctx.provider_for(ip.provider).create_reserve_ip(
            location_code=ip.location.code, name=f"{s.server_name_prefix}-ip-{ip.id}"
        )
        ip.provider_ip_id, ip.address, ip.datacenter = info.id, info.address, info.datacenter
        ip.status = IPStatus.ACTIVE
        await session.commit()
        await ctx.notifier.send(ip.user.telegram_id, T.ip_created(ip))


async def f_create_reserve_ip(ctx: WorkerContext, job: Job, error: Exception) -> None:
    async with ctx.sm() as session:
        ip = await session.get(IPAddress, job.ip_id)
        if ip is None or ip.status != IPStatus.CREATING:
            return
        ip.status = IPStatus.FAILED
        refunded = await billing.refund_ip(session, ip, "بازگشت وجه IP رزرو ناموفق")
        await session.commit()
        await ctx.notifier.send(
            ip.user.telegram_id, "❌ ساخت IP رزرو ناموفق بود." + (f"\n💰 {refunded:,} ریال بازگردانده شد." if refunded else "")
        )


async def h_delete_reserve_ip(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        ip = await _get_ip(session, job)
        if ip.status == IPStatus.DELETED:
            return
        if ip.provider_ip_id:
            await ctx.provider_for(ip.provider).delete_ip(ip.provider_ip_id)
        ip.status = IPStatus.DELETED
        ip.deleted_at = utcnow()
        await session.commit()
        if not job.payload.get("silent"):
            await ctx.notifier.send(ip.user.telegram_id, T.ip_deleted(ip.address))


async def h_swap_ip(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        ip = await _get_ip(session, job)
        if server.status != ServerStatus.ACTIVE or ip.status != IPStatus.ACTIVE:
            return
        old = await ctx.provider_for(server.provider).swap_primary_ipv4(server.provider_server_id, ip.provider_ip_id)
        server.ipv4, server.provider_ip_id = ip.address, ip.provider_ip_id
        ip.address, ip.provider_ip_id = old.address, old.id
        server.last_ip_change_at = utcnow()
        await session.commit()
        await ctx.notifier.send(ip.user.telegram_id, T.ip_swapped(server, ip.address), K.to_server(server.id))


# ---- admin / automation ----------------------------------------------------------------
async def h_pool_create(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        server = await _get_server(session, job)
        if server.user_id is not None or server.status not in (ServerStatus.PENDING, ServerStatus.CREATING):
            return
        server.status = ServerStatus.CREATING
        await session.commit()
        provider = ctx.provider_for(server.provider)
        existing = await provider.find_servers({"managed-by": "hetzbot", "hb-id": str(server.id)})
        if existing:
            info, password = existing[0], None
        else:
            created = await provider.create_server(
                name=server.name,
                plan_code=server.plan.code,
                location_code=server.location.code,
                image_code=server.image.code,
                labels=labels_for(server),
            )
            info, password = created.server, created.root_password
        apply_info(server, info)
        if password:
            server.root_password_enc = encrypt(password)
        server.status = ServerStatus.POOLED
        await session.commit()


async def f_pool_create(ctx: WorkerContext, job: Job, error: Exception) -> None:
    async with ctx.sm() as session:
        server = await session.get(Server, job.server_id)
        if server is None or server.status == ServerStatus.POOLED:
            return
        server.status = ServerStatus.FAILED
        server.failure_reason = str(error)[:255]
        if isinstance(error, OutOfStock):
            # Stop retrying a sold-out plan until the admin re-enables pooling.
            plan = await session.get(Plan, server.plan_id)
            plan.pool_target = 0
        await session.commit()


async def h_sync_provider(ctx: WorkerContext, job: Job) -> None:
    async with ctx.sm() as session:
        row = await session.get(Provider, job.payload["provider_id"])
        if row is None:
            raise JobFailed("provider not found")
        counts = await catalog.sync_provider(session, row)
        await session.commit()
    await ctx.notifier.send_admins(f"🔄 Provider {row.code} synced: {counts}")


HANDLERS = {
    "create_server": h_create_server,
    "purchase_change_ip": h_purchase_change_ip,
    "power": h_power,
    "rebuild": h_rebuild,
    "change_ip": h_change_ip,
    "delete_server": h_delete_server,
    "create_reserve_ip": h_create_reserve_ip,
    "delete_reserve_ip": h_delete_reserve_ip,
    "swap_ip": h_swap_ip,
    "pool_create": h_pool_create,
    "sync_provider": h_sync_provider,
}

# Called once when a job fails permanently (after retries are exhausted).
FAILURE_HANDLERS = {
    "create_server": f_create_server,
    "create_reserve_ip": f_create_reserve_ip,
    "pool_create": f_pool_create,
}


async def notify_generic_failure(ctx: WorkerContext, job: Job) -> None:
    """Tell the user an operation failed (for jobs without a dedicated failure handler)."""
    if job.kind in FAILURE_HANDLERS or job.kind in ("sync_provider",) or job.payload.get("silent"):
        return
    async with ctx.sm() as session:
        server = await session.get(Server, job.server_id) if job.server_id else None
        if job.kind == "purchase_change_ip" and server is not None and server.status == ServerStatus.CHECKING:
            # Couldn't get a new IP: fall back to the current one and let the user confirm.
            server.status = ServerStatus.AWAITING
            server.ready_at = utcnow()
            await session.commit()
            s = await load_settings(session)
            await send_ready(ctx, server, max(0, s.max_purchase_ip_changes - server.purchase_ip_changes))
        if job.kind == "delete_server" and server is not None and server.status == ServerStatus.DELETING:
            return  # admins are alerted; the user sees "deleting" until it is sorted out
        if job.kind == "delete_reserve_ip" and job.ip_id:
            return
        if server is not None and server.user:
            await ctx.notifier.send(server.user.telegram_id, T.operation_failed(server.name), K.to_server(server.id))


async def find_awaiting_expired(session, minutes: int) -> list[int]:
    from datetime import timedelta

    cutoff = utcnow() - timedelta(minutes=minutes)
    return list(
        (
            await session.scalars(
                select(Server.id).where(Server.status == ServerStatus.AWAITING, Server.ready_at <= cutoff)
            )
        ).all()
    )
