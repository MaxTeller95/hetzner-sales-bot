"""Bot-side checks that don't need Telegram: callback sizes, dispatcher wiring, menu rendering."""
from sqlalchemy import select

from app.bot.callbacks import BuyCB, IpCB, SrvCB
from app.bot.handlers import buy, ips, servers, wallet
from app.db.models import OSImage, Plan, Server, User
from app.db.session import SessionLocal
from app.services import servers as server_svc
from app.worker.runner import drain_once


def test_callback_data_fits_telegram_limit():
    big = 2**31 - 1
    assert len(BuyCB(step="sum", prov=big, loc=big, plan=big, mode="m", img=big, sort="c", page=999).pack()) <= 64
    assert len(SrvCB(action="rbc", id=big, arg=big).pack()) <= 64
    assert len(IpCB(action="swY", id=big, arg=big).pack()) <= 64


def test_dispatcher_builds():
    from app.bot.__main__ import build_dispatcher

    dp = build_dispatcher()
    assert "message" in dp.resolve_used_update_types()
    assert "callback_query" in dp.resolve_used_update_types()


async def test_purchase_menus_render(rich_user):
    async with SessionLocal() as session:
        text, kb = await buy.render_entry(session)
        assert "موقعیت" in text and kb.inline_keyboard
        loc_cb = BuyCB.unpack(kb.inline_keyboard[0][0].callback_data)

        text, kb = await buy.render_plans(session, BuyCB(step="plans", prov=loc_cb.prov, loc=loc_cb.loc, sort="c"))
        plan_buttons = [row[0] for row in kb.inline_keyboard[1:-1]]
        prices = [int(b.text.split("|")[-1].replace("ریال", "").replace(",", "").strip()) for b in plan_buttons]
        assert prices == sorted(prices), "cheapest tab must be sorted by price"

        plan_cb = BuyCB.unpack(plan_buttons[0].callback_data)
        text, kb = await buy.render_plan(session, plan_cb)
        assert "ساعتی" in text
        os_cb = BuyCB.unpack(kb.inline_keyboard[0][0].callback_data)
        text, kb = await buy.render_os(session, os_cb)
        sum_cb = BuyCB.unpack(kb.inline_keyboard[0][0].callback_data)
        user = await session.get(User, rich_user.id)
        text, kb = await buy.render_summary(session, user, sum_cb)
        assert BuyCB.unpack(kb.inline_keyboard[0][0].callback_data).step == "ok"


async def test_server_and_wallet_screens(rich_user, make_ctx):
    async with SessionLocal() as session:
        user = await session.get(User, rich_user.id)
        plan = await session.scalar(select(Plan).where(Plan.code == "cx22"))
        image = await session.scalar(select(OSImage).where(OSImage.code == "debian-12"))
        server = await server_svc.start_purchase(session, user, plan_id=plan.id, image_id=image.id, mode="h")
        await session.commit()
    await drain_once(make_ctx())
    async with SessionLocal() as session:
        user = await session.get(User, rich_user.id)
        server = await session.get(Server, server.id)
        text, kb = await servers.render_server(session, server)
        assert "آماده تحویل" in text  # awaiting confirmation shows the purchase screen
        await server_svc.deliver(session, server)
        text, kb = await servers.render_server(session, server)
        assert server.ipv4 in text and "آنلاین" in text
        text, kb = await servers.render_list(session, user)
        assert server.name in kb.inline_keyboard[0][0].text
        text, _ = await wallet.render_wallet(session, user)
        assert "موجودی" in text
        text, kb = await ips.render_ips(session, user)
        assert "IP" in text
