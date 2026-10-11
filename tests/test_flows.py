"""End-to-end flows against the fake provider: purchase, billing, IP changes, enforcement, pool."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import (
    BillingEvent,
    IPAddress,
    IPStatus,
    Job,
    JobStatus,
    OSImage,
    Plan,
    Server,
    ServerStatus,
    Transaction,
    TxKind,
    User,
    Wallet,
)
from app.db.session import SessionLocal
from app.providers import fake
from app.services import billing, ips, servers, topups
from app.services import pool as pool_svc
from app.services.errors import InsufficientBalance, UserError
from app.services.runtime_settings import save_settings
from app.utils import utcnow
from app.worker.runner import drain_once
from app.worker.tasks import task_auto_deliver, task_enforce_balances, task_sync_servers
from tests.conftest import FakeChecker


async def _cheapest(session, code="cx22"):
    plan = await session.scalar(select(Plan).where(Plan.code == code).order_by(Plan.id))
    image = await session.scalar(select(OSImage).where(OSImage.code == "ubuntu-24.04"))
    return plan, image


async def _buy(user, mode="h", code="cx22") -> int:
    async with SessionLocal() as session:
        user = await session.get(User, user.id)
        plan, image = await _cheapest(session, code)
        server = await servers.start_purchase(session, user, plan_id=plan.id, image_id=image.id, mode=mode, chat_id=555, message_id=1)
        await session.commit()
        return server.id


async def _balance(user_id) -> int:
    async with SessionLocal() as session:
        return (await session.scalar(select(Wallet).where(Wallet.user_id == user_id))).balance


async def test_purchase_happy_path(rich_user, make_ctx, notifier):
    sid = await _buy(rich_user)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        hourly = server.hourly_price
        assert server.status == ServerStatus.PENDING
    assert await _balance(rich_user.id) == 50_000_000 - hourly  # first hour charged up front

    await drain_once(make_ctx())
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.status == ServerStatus.AWAITING
        assert server.ipv4 and server.root_password_enc
        assert server.check_passed is True
        assert server.check_summary.startswith("4/4")
        assert await servers.deliver(session, server)
        await session.commit()
        assert server.status == ServerStatus.ACTIVE
    assert any("آماده تحویل" in text for _, text in notifier.messages)


async def test_hourly_billing_and_minimum_hour(rich_user, make_ctx):
    sid = await _buy(rich_user)
    await drain_once(make_ctx())
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        hourly = server.hourly_price
        start = server.next_charge_at

    # Nothing more is due within the first hour.
    assert await billing.bill_due(SessionLocal, start - timedelta(minutes=1)) == 0
    # 2.5 hours later two more units are due.
    assert await billing.bill_due(SessionLocal, start + timedelta(hours=1, minutes=30)) == 2
    assert await _balance(rich_user.id) == 50_000_000 - 3 * hourly

    # Delete: no further charges.
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        server.status = ServerStatus.ACTIVE
        await servers.request_delete(session, server)
        await session.commit()
    await drain_once(make_ctx())
    assert await billing.bill_due(SessionLocal, start + timedelta(hours=10)) == 0
    async with SessionLocal() as session:
        assert (await session.get(Server, sid)).status == ServerStatus.DELETED
        events = await session.scalar(select(func.count(BillingEvent.id)).where(BillingEvent.server_id == sid))
        assert events == 3


async def test_monthly_billing_charges_upfront(rich_user, make_ctx):
    sid = await _buy(rich_user, mode="m")
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert await _balance(rich_user.id) == 50_000_000 - server.monthly_price
        assert server.next_charge_at - server.created_at >= timedelta(hours=719)


async def test_failed_checks_rotate_ip_then_pass(rich_user, make_ctx):
    checker = FakeChecker(fail_times=2)
    sid = await _buy(rich_user)
    await drain_once(make_ctx(checker))
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.status == ServerStatus.AWAITING
        assert server.purchase_ip_changes == 2
        assert len(set(checker.calls)) == 3  # a different IP each time
        ip_charges = await session.scalar(
            select(func.count(BillingEvent.id)).where(BillingEvent.server_id == sid, BillingEvent.resource_type == "ip_change")
        )
        assert ip_charges == 2


async def test_check_never_passes_cancels_and_refunds(rich_user, make_ctx):
    sid = await _buy(rich_user)
    await drain_once(make_ctx(FakeChecker(fail_times=99)))
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.status == ServerStatus.FAILED
        assert server.purchase_ip_changes == 5
    assert await _balance(rich_user.id) == 50_000_000  # everything refunded
    assert not fake._servers


async def test_connectivity_service_down_still_offers_delivery(rich_user, make_ctx, notifier):
    sid = await _buy(rich_user)
    await drain_once(make_ctx(FakeChecker(unavailable=True)))
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.status == ServerStatus.AWAITING
        assert server.check_passed is None
    assert notifier.admin_messages


async def test_user_ip_change_limits(rich_user, make_ctx):
    sid = await _buy(rich_user)
    ctx = make_ctx()
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        for _ in range(5):
            await servers.request_purchase_ip_change(session, server)
            await session.commit()
            await drain_once(ctx)
            await session.refresh(server)
        assert server.purchase_ip_changes == 5
        with pytest.raises(UserError):
            await servers.request_purchase_ip_change(session, server)

        await servers.deliver(session, server)
        await session.commit()
        old_ip = server.ipv4
        await servers.request_ip_change(session, server)
        await session.commit()
        await drain_once(ctx)
        await session.refresh(server)
        assert server.ipv4 != old_ip
        with pytest.raises(UserError):  # once per day after delivery
            await servers.request_ip_change(session, server)


async def test_insufficient_balance(make_ctx):
    async with SessionLocal() as session:
        from app.services.wallet import get_or_create_user

        user = await get_or_create_user(session, 777, "bob", "Bob")
        plan, image = await _cheapest(session)
        with pytest.raises(InsufficientBalance):
            await servers.start_purchase(session, user, plan_id=plan.id, image_id=image.id, mode="h")


async def test_out_of_stock_refunds_and_hides_plan(rich_user, make_ctx):
    fake.FakeProvider.out_of_stock = {"cx22"}
    sid = await _buy(rich_user)
    await drain_once(make_ctx())
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.status == ServerStatus.FAILED
        assert (await session.get(Plan, server.plan_id)).is_available is False
    assert await _balance(rich_user.id) == 50_000_000


async def test_negative_balance_suspends_then_deletes(rich_user, make_ctx):
    sid = await _buy(rich_user)
    ctx = make_ctx()
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        await servers.deliver(session, server)
        from app.services import wallet

        await wallet.apply(session, rich_user.id, -60_000_000, TxKind.ADJUST, "drain")
        await session.commit()

    await task_enforce_balances(ctx)
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.status == ServerStatus.SUSPENDED
        assert server.power_status == "off"

    await task_enforce_balances(ctx, now=utcnow() + timedelta(hours=25))
    await drain_once(ctx)
    async with SessionLocal() as session:
        assert (await session.get(Server, sid)).status == ServerStatus.DELETED


async def test_topup_resumes_suspended_servers(rich_user, make_ctx):
    sid = await _buy(rich_user)
    ctx = make_ctx()
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        await servers.deliver(session, server)
        from app.services import wallet

        await wallet.apply(session, rich_user.id, -60_000_000, TxKind.ADJUST, "drain")
        await session.commit()
    await task_enforce_balances(ctx)
    await drain_once(ctx)

    async with SessionLocal() as session:
        user = await session.get(User, rich_user.id)
        req = await topups.create_request(session, user, 20_000_000, "file")
        await session.commit()
        _, resumed = await topups.approve(session, req.id, "test")
        await session.commit()
        assert [s.id for s in resumed] == [sid]
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.status == ServerStatus.ACTIVE and server.power_status == "running"


async def test_reserve_ip_create_swap_delete(rich_user, make_ctx):
    sid = await _buy(rich_user)
    ctx = make_ctx()
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        await servers.deliver(session, server)
        user = await session.get(User, rich_user.id)
        ip1 = await ips.create_reserve_ip(session, user, server.location_id)
        ip2 = await ips.create_reserve_ip(session, user, server.location_id)
        await session.commit()
        ip1_id, ip2_id = ip1.id, ip2.id
        with pytest.raises(UserError):  # max 2
            await ips.create_reserve_ip(session, user, server.location_id)
        await session.rollback()
    await drain_once(ctx)

    async with SessionLocal() as session:
        ip1 = await session.get(IPAddress, ip1_id)
        server = await session.get(Server, sid)
        assert ip1.status == IPStatus.ACTIVE and ip1.address
        # reserve IP costs 2x the normal IP price
        assert ip1.hourly_price == 2 * server.ip_hourly_price or abs(ip1.hourly_price - 2 * server.ip_hourly_price) <= 100
        reserve_addr, server_addr = ip1.address, server.ipv4
        await ips.request_swap(session, ip1, server)
        await session.commit()
    await drain_once(ctx)

    async with SessionLocal() as session:
        ip1 = await session.get(IPAddress, ip1_id)
        server = await session.get(Server, sid)
        assert server.ipv4 == reserve_addr and ip1.address == server_addr
        ip2 = await session.get(IPAddress, ip2_id)
        await ips.request_delete_ip(session, ip2)
        await session.commit()
    await drain_once(ctx)
    async with SessionLocal() as session:
        assert (await session.get(IPAddress, ip2_id)).status == IPStatus.DELETED


async def test_pool_server_is_used_for_purchase(rich_user, make_ctx):
    ctx = make_ctx()
    async with SessionLocal() as session:
        plan, _ = await _cheapest(session)
        plan.pool_target = 1
        await session.commit()
        assert await pool_svc.maintain(session) == 1
        await session.commit()
    await drain_once(ctx)
    async with SessionLocal() as session:
        pooled = await session.scalar(select(Server).where(Server.status == ServerStatus.POOLED))
        assert pooled is not None
        pooled_pid = pooled.provider_server_id

    sid = await _buy(rich_user)
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.provider_server_id == pooled_pid
        assert server.status == ServerStatus.AWAITING
        assert (await session.get(Server, pooled.id)).status == ServerStatus.TRANSFERRED


async def test_auto_deliver_after_timeout(rich_user, make_ctx):
    sid = await _buy(rich_user)
    ctx = make_ctx()
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        server.ready_at = utcnow() - timedelta(hours=1)
        await session.commit()
    await task_auto_deliver(ctx)
    async with SessionLocal() as session:
        assert (await session.get(Server, sid)).status == ServerStatus.ACTIVE


async def test_provider_lock_marks_abuse_after_confirmation(rich_user, make_ctx, notifier, monkeypatch):
    from app.worker import tasks

    sid = await _buy(rich_user)
    ctx = make_ctx()
    await drain_once(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        await servers.deliver(session, server)
        await session.commit()
        fake._servers[server.provider_server_id].locked = True

    await task_sync_servers(ctx)  # first sighting only
    async with SessionLocal() as session:
        assert not (await session.get(Server, sid)).locked
    monkeypatch.setattr(tasks, "LOCK_CONFIRM_AFTER", timedelta(0))
    await task_sync_servers(ctx)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        assert server.locked
        with pytest.raises(UserError):
            await servers.request_power(session, server, "reboot")
    assert any("Abuse" in t for _, t in notifier.messages)


async def test_failed_job_is_marked(rich_user, make_ctx):
    async with SessionLocal() as session:
        from app.services import jobs

        await jobs.enqueue(session, "nonexistent_kind")
        await session.commit()
    await drain_once(make_ctx())
    async with SessionLocal() as session:
        job = await session.scalar(select(Job).where(Job.kind == "nonexistent_kind"))
        assert job.status == JobStatus.FAILED


async def test_settings_roundtrip():
    async with SessionLocal() as session:
        await save_settings(session, {"profit_margin_percent": "40", "connectivity_enabled": "off"})
        await session.commit()
        from app.services.runtime_settings import load_settings

        s = await load_settings(session, fresh=True)
        assert s.profit_margin_percent == 40.0 and s.connectivity_enabled is False


async def test_refund_math(rich_user, make_ctx):
    sid = await _buy(rich_user)
    async with SessionLocal() as session:
        server = await session.get(Server, sid)
        refunded = await billing.refund_server(session, server, "x")
        again = await billing.refund_server(session, server, "x")
        await session.commit()
        assert refunded == server.hourly_price and again == 0
        total = await session.scalar(select(func.sum(Transaction.amount)).where(Transaction.user_id == rich_user.id))
        assert total == 50_000_000
