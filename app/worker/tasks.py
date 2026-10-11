"""Periodic tasks run by the worker."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.bot import keyboards as K
from app.bot import texts as T
from app.config import get_settings
from app.db.models import IPAddress, IPStatus, Provider, Server, ServerStatus, Wallet
from app.providers.base import ProviderError
from app.services import abuse as abuse_svc
from app.services import billing, jobs
from app.services import pool as pool_svc
from app.services import wallet as wallet_svc
from app.services.runtime_settings import load_settings
from app.utils import utcnow
from app.worker.context import WorkerContext
from app.worker.handlers import deliver_and_notify, find_awaiting_expired

log = logging.getLogger(__name__)


async def task_billing(ctx: WorkerContext) -> None:
    await billing.bill_due(ctx.sm)


async def task_enforce_balances(ctx: WorkerContext, now: datetime | None = None) -> None:
    """Negative balance → power off (suspend). Still negative after the grace period → delete."""
    now = now or utcnow()
    outbox: list[tuple[int, str]] = []
    async with ctx.sm() as session:
        s = await load_settings(session)
        wallets = (await session.scalars(select(Wallet).where(Wallet.balance < 0))).all()
        for wallet in wallets:
            user = wallet.user
            servers = (
                await session.scalars(
                    select(Server).where(Server.user_id == user.id, Server.status.in_(ServerStatus.BILLABLE))
                )
            ).all()
            ips = (
                await session.scalars(
                    select(IPAddress).where(IPAddress.user_id == user.id, IPAddress.status.in_(IPStatus.BILLABLE))
                )
            ).all()
            grace_over = wallet.negative_since and wallet.negative_since <= now - timedelta(hours=s.suspend_grace_hours)
            if grace_over:
                removed = []
                for server in servers:
                    if await jobs.has_active_job(session, server_id=server.id):
                        continue  # finish the running operation first; next round picks it up
                    server.status = ServerStatus.DELETING
                    await jobs.enqueue(session, "delete_server", {"silent": True}, server_id=server.id, max_attempts=6)
                    removed.append(f"سرور {server.name} ({server.ipv4 or '-'})")
                for ip in ips:
                    if ip.status != IPStatus.ACTIVE:
                        continue
                    ip.status = IPStatus.DELETING
                    await jobs.enqueue(session, "delete_reserve_ip", {"silent": True}, ip_id=ip.id, max_attempts=6)
                    removed.append(f"IP {ip.address}")
                if removed:
                    outbox.append((user.telegram_id, T.terminated(removed)))
            else:
                newly = []
                for server in servers:
                    if server.status == ServerStatus.ACTIVE:
                        server.status = ServerStatus.SUSPENDED
                        server.suspended_at = now
                        await jobs.enqueue(session, "power", {"action": "poweroff", "silent": True}, server_id=server.id)
                        newly.append(server.name)
                if newly:
                    outbox.append((user.telegram_id, T.suspended(newly, s.suspend_grace_hours)))
        await session.commit()
    for chat_id, text in outbox:
        await ctx.notifier.send(chat_id, text, K.to_wallet())


async def task_low_balance(ctx: WorkerContext, now: datetime | None = None) -> None:
    now = now or utcnow()
    outbox = []
    async with ctx.sm() as session:
        s = await load_settings(session)
        cutoff = now - timedelta(hours=s.low_balance_notify_hours)
        wallets = (
            await session.scalars(
                select(Wallet).where(
                    Wallet.balance < s.low_balance_threshold,
                    Wallet.balance >= 0,
                    (Wallet.low_balance_notified_at.is_(None)) | (Wallet.low_balance_notified_at <= cutoff),
                )
            )
        ).all()
        for wallet in wallets:
            burn = await wallet_svc.hourly_burn(session, wallet.user_id)
            if burn <= 0:
                continue
            wallet.low_balance_notified_at = now
            outbox.append((wallet.user.telegram_id, T.low_balance(wallet.balance, wallet.balance // burn)))
        await session.commit()
    for chat_id, text in outbox:
        await ctx.notifier.send(chat_id, text, K.to_wallet())


async def task_auto_deliver(ctx: WorkerContext) -> None:
    async with ctx.sm() as session:
        s = await load_settings(session)
        ids = await find_awaiting_expired(session, s.auto_deliver_minutes)
    for sid in ids:
        await deliver_and_notify(ctx, sid)


_locked_seen: dict[int, datetime] = {}
LOCK_CONFIRM_AFTER = timedelta(minutes=10)


async def task_sync_servers(ctx: WorkerContext) -> None:
    """Refresh power state, detect servers deleted outside the bot and provider-side locks."""
    async with ctx.sm() as session:
        providers = (await session.scalars(select(Provider).where(Provider.is_active.is_(True)))).all()
    for prow in providers:
        impl = ctx.provider_for(prow)
        try:
            infos = {i.id: i for i in await impl.find_servers({"managed-by": "hetzbot"})}
        except ProviderError as e:
            log.warning("sync of %s failed: %s", prow.code, e)
            continue
        alerts, user_msgs = [], []
        async with ctx.sm() as session:
            servers = (
                await session.scalars(
                    select(Server).where(
                        Server.provider_id == prow.id,
                        Server.provider_server_id.is_not(None),
                        Server.status.in_(
                            (ServerStatus.AWAITING, ServerStatus.ACTIVE, ServerStatus.SUSPENDED, ServerStatus.POOLED)
                        ),
                    )
                )
            ).all()
            for server in servers:
                info = infos.get(server.provider_server_id)
                if info is None:
                    try:
                        info = await impl.get_server(server.provider_server_id)
                    except ProviderError:
                        continue
                    if info is None:
                        server.status = ServerStatus.DELETED
                        server.deleted_at = utcnow()
                        server.failure_reason = "missing at provider"
                        alerts.append(f"⚠️ Server {server.name} ({server.ipv4}) no longer exists at {prow.code}; marked deleted.")
                        continue
                server.power_status = info.status
                if info.ipv4 and info.ipv4 != server.ipv4:
                    server.ipv4, server.provider_ip_id = info.ipv4, info.ipv4_id
                if info.locked and not server.locked:
                    first = _locked_seen.setdefault(server.id, utcnow())
                    if utcnow() - first >= LOCK_CONFIRM_AFTER and not await jobs.has_active_job(session, server_id=server.id):
                        await abuse_svc.lock_server(
                            session,
                            server,
                            reason="Locked by provider (possible abuse report)",
                            source="provider",
                            external_id=f"{prow.code}-lock-{server.provider_server_id}-{first:%Y%m%d%H%M}",
                        )
                        _locked_seen.pop(server.id, None)
                        alerts.append(f"🚨 {server.name} ({server.ipv4}) was locked by {prow.code}. Locked in bot.")
                        if server.user:
                            user_msgs.append((server.user.telegram_id, T.abuse_locked(server)))
                elif not info.locked:
                    _locked_seen.pop(server.id, None)
            await session.commit()
        for text in alerts:
            await ctx.notifier.send_admins(text)
        for chat_id, text in user_msgs:
            await ctx.notifier.send(chat_id, text)


async def task_abuse_mailbox(ctx: WorkerContext) -> None:
    cfg = get_settings()
    if not cfg.abuse_imap_host:
        return
    try:
        emails = await asyncio.to_thread(abuse_svc.fetch_abuse_emails, cfg)
    except Exception as e:  # noqa: BLE001 - IMAP errors are many and varied
        log.warning("abuse mailbox check failed: %s", e)
        return
    for message_id, subject, body in emails:
        alerts, user_msgs = [], []
        async with ctx.sm() as session:
            ips = abuse_svc.extract_ips(subject + "\n" + body)
            servers = await abuse_svc.servers_by_ip(session, ips)
            if not servers:
                alerts.append(f"📧 Abuse e-mail with no matching server: {subject[:150]}")
            for server in servers:
                report = await abuse_svc.lock_server(
                    session,
                    server,
                    reason=f"Abuse report: {subject[:200]}",
                    source="email",
                    subject=subject,
                    body=body[:20000],
                    external_id=f"{message_id}#{server.id}",
                )
                if report is None:
                    continue
                alerts.append(f"🚨 Abuse report for {server.name} ({server.ipv4}): {subject[:150]}\nServer locked.")
                if server.user:
                    user_msgs.append((server.user.telegram_id, T.abuse_locked(server)))
            await session.commit()
        for text in alerts:
            await ctx.notifier.send_admins(text)
        for chat_id, text in user_msgs:
            await ctx.notifier.send(chat_id, text)


async def task_pool(ctx: WorkerContext) -> None:
    async with ctx.sm() as session:
        queued = await pool_svc.maintain(session)
        await session.commit()
    if queued:
        log.info("queued %d pool servers", queued)


async def periodic(name: str, interval: float, fn, ctx: WorkerContext) -> None:
    while True:
        try:
            await fn(ctx)
        except Exception:
            log.exception("periodic task %s failed", name)
        await asyncio.sleep(interval)


PERIODIC = [
    ("billing", 60, task_billing),
    ("enforce_balances", 120, task_enforce_balances),
    ("low_balance", 600, task_low_balance),
    ("auto_deliver", 60, task_auto_deliver),
    ("sync_servers", 300, task_sync_servers),
    ("abuse_mailbox", 300, task_abuse_mailbox),
    ("pool", 600, task_pool),
]
