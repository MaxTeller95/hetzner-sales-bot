"""Purchase flow: provider → location → plan → billing → OS → summary → create."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import BuyCB, WalletCB
from app.bot.handlers.common import show
from app.db.models import Location, OSImage, Plan, Provider, User
from app.services import servers as server_svc
from app.services import wallet as wallet_svc
from app.services.errors import UserError
from app.services.pricing import PricingNotConfigured, quote_plan
from app.services.runtime_settings import load_settings
from app.utils import flag

router = Router(name="buy")
PAGE_SIZE = 8


def _purchasable():
    return (
        select(Plan)
        .join(Location, Plan.location_id == Location.id)
        .join(Provider, Plan.provider_id == Provider.id)
        .where(
            Plan.is_active.is_(True),
            Plan.is_available.is_(True),
            Location.is_active.is_(True),
            Provider.is_active.is_(True),
        )
    )


async def _providers(session: AsyncSession) -> list[Provider]:
    ids = set((await session.scalars(_purchasable().with_only_columns(Plan.provider_id).distinct())).all())
    if not ids:
        return []
    return list((await session.scalars(select(Provider).where(Provider.id.in_(ids)).order_by(Provider.sort_order))).all())


async def _locations(session: AsyncSession, provider_id: int) -> list[Location]:
    ids = set(
        (
            await session.scalars(
                _purchasable().where(Plan.provider_id == provider_id).with_only_columns(Plan.location_id).distinct()
            )
        ).all()
    )
    if not ids:
        return []
    return list((await session.scalars(select(Location).where(Location.id.in_(ids)).order_by(Location.sort_order))).all())


async def render_entry(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup | None]:
    providers = await _providers(session)
    if not providers:
        return T.BUY_NOTHING, None
    if len(providers) == 1:
        return await render_locations(session, providers[0].id, single_provider=True)
    kb = InlineKeyboardBuilder()
    for p in providers:
        kb.button(text=f"☁️ {p.name}", callback_data=BuyCB(step="loc", prov=p.id))
    kb.adjust(1)
    return T.BUY_CHOOSE_PROVIDER, kb.as_markup()


async def render_locations(session: AsyncSession, prov: int, single_provider: bool) -> tuple[str, InlineKeyboardMarkup | None]:
    locations = await _locations(session, prov)
    if not locations:
        return T.BUY_NOTHING, None
    kb = InlineKeyboardBuilder()
    for loc in locations:
        kb.button(text=f"{flag(loc.country)} {loc.name}", callback_data=BuyCB(step="plans", prov=prov, loc=loc.id))
    kb.adjust(1)
    if not single_provider:
        kb.row(K.buy_back("prov"))
    return T.BUY_CHOOSE_LOCATION, kb.as_markup()


async def render_plans(session: AsyncSession, cb: BuyCB) -> tuple[str, InlineKeyboardMarkup]:
    s = await load_settings(session)
    plans = (await session.scalars(_purchasable().where(Plan.location_id == cb.loc))).all()
    location = await session.get(Location, cb.loc)
    try:
        quoted = [(p, quote_plan(p, p.location, s)) for p in plans]
    except PricingNotConfigured:
        raise UserError("قیمت‌گذاری هنوز توسط مدیریت تنظیم نشده است.") from None
    if cb.sort == "c":
        quoted.sort(key=lambda pq: (pq[1].hourly, pq[0].name))
    else:
        quoted.sort(key=lambda pq: (pq[0].sort_order, pq[0].cpu_type, pq[0].arch, pq[1].hourly))

    pages = max(1, (len(quoted) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(cb.page, 0), pages - 1)
    kb = InlineKeyboardBuilder()
    base = dict(prov=cb.prov, loc=cb.loc)
    kb.row(
        InlineKeyboardButton(
            text=("✔️ " if cb.sort == "a" else "") + T.SORT_ALL,
            callback_data=BuyCB(step="plans", sort="a", **base).pack(),
        ),
        InlineKeyboardButton(
            text=("✔️ " if cb.sort == "c" else "") + T.SORT_CHEAP,
            callback_data=BuyCB(step="plans", sort="c", **base).pack(),
        ),
    )
    for plan, quote in quoted[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        kb.row(
            InlineKeyboardButton(
                text=T.plan_button(plan, quote),
                callback_data=BuyCB(step="plan", plan=plan.id, sort=cb.sort, page=page, **base).pack(),
            )
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=BuyCB(step="plans", sort=cb.sort, page=page - 1, **base).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=BuyCB(step="plans", sort=cb.sort, page=page, **base).pack()))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=BuyCB(step="plans", sort=cb.sort, page=page + 1, **base).pack()))
        kb.row(*nav)
    kb.row(K.buy_back("loc", prov=cb.prov))
    return T.buy_plans_header(location.name if location else "", cb.sort == "c"), kb.as_markup()


async def render_plan(session: AsyncSession, cb: BuyCB) -> tuple[str, InlineKeyboardMarkup]:
    s = await load_settings(session)
    plan = await server_svc.purchasable_plan(session, cb.plan)
    quote = quote_plan(plan, plan.location, s)
    kb = InlineKeyboardBuilder()
    for mode in ("h", "m"):
        kb.row(
            InlineKeyboardButton(
                text=T.billing_button(mode, quote),
                callback_data=BuyCB(step="os", prov=cb.prov, loc=cb.loc, plan=plan.id, mode=mode, sort=cb.sort, page=cb.page).pack(),
            )
        )
    kb.row(K.buy_back("plans", prov=cb.prov, loc=cb.loc, sort=cb.sort, page=cb.page))
    return T.plan_detail(plan, quote), kb.as_markup()


async def render_os(session: AsyncSession, cb: BuyCB) -> tuple[str, InlineKeyboardMarkup]:
    plan = await server_svc.purchasable_plan(session, cb.plan)
    images = (
        await session.scalars(
            select(OSImage)
            .where(OSImage.provider_id == plan.provider_id, OSImage.is_active.is_(True))
            .order_by(OSImage.sort_order, OSImage.name)
        )
    ).all()
    kb = InlineKeyboardBuilder()
    for img in images:
        if img.supports(plan.arch):
            kb.button(
                text=f"💿 {img.name}",
                callback_data=BuyCB(step="sum", prov=cb.prov, loc=cb.loc, plan=plan.id, mode=cb.mode, img=img.id, sort=cb.sort, page=cb.page),
            )
    kb.adjust(2)
    kb.row(K.buy_back("plan", prov=cb.prov, loc=cb.loc, plan=plan.id, sort=cb.sort, page=cb.page))
    return T.BUY_CHOOSE_OS, kb.as_markup()


async def render_summary(session: AsyncSession, user: User, cb: BuyCB) -> tuple[str, InlineKeyboardMarkup]:
    s = await load_settings(session)
    plan = await server_svc.purchasable_plan(session, cb.plan)
    image = await session.get(OSImage, cb.img)
    if image is None or not image.is_active:
        raise UserError("این سیستم‌عامل در دسترس نیست.")
    quote = quote_plan(plan, plan.location, s)
    required = quote.hourly * (1 + s.purchase_buffer_hours) if cb.mode == "h" else quote.monthly
    wallet = await wallet_svc.get_wallet(session, user.id)
    kb = InlineKeyboardBuilder()
    if wallet.balance >= required:
        kb.row(InlineKeyboardButton(text=T.BUY_CONFIRM_BTN, callback_data=BuyCB(step="ok", prov=cb.prov, loc=cb.loc, plan=plan.id, mode=cb.mode, img=image.id).pack()))
    else:
        kb.row(InlineKeyboardButton(text=T.BUY_TOPUP_BTN, callback_data=WalletCB(action="top").pack()))
    kb.row(K.buy_back("os", prov=cb.prov, loc=cb.loc, plan=plan.id, mode=cb.mode, sort=cb.sort, page=cb.page))
    return T.buy_confirm(plan, image.name, cb.mode, quote, wallet.balance, required), kb.as_markup()


@router.message(F.text == T.BTN_BUY)
async def buy_entry(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    text, kb = await render_entry(session)
    await message.answer(text, reply_markup=kb)


@router.callback_query(BuyCB.filter())
async def buy_step(cb: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User) -> None:
    step = callback_data.step
    if step == "prov":
        text, kb = await render_entry(session)
    elif step == "loc":
        single = len(await _providers(session)) <= 1
        text, kb = await render_locations(session, callback_data.prov, single)
    elif step == "plans":
        text, kb = await render_plans(session, callback_data)
    elif step == "plan":
        text, kb = await render_plan(session, callback_data)
    elif step == "os":
        text, kb = await render_os(session, callback_data)
    elif step == "sum":
        text, kb = await render_summary(session, user, callback_data)
    elif step == "ok":
        server = await server_svc.start_purchase(
            session,
            user,
            plan_id=callback_data.plan,
            image_id=callback_data.img,
            mode=callback_data.mode,
            chat_id=cb.message.chat.id,
            message_id=cb.message.message_id,
        )
        await cb.answer("✅")
        await show(cb, T.progress_queued(server))
        await session.commit()  # the worker edits this same message as the order progresses
        return
    else:
        await cb.answer()
        return
    await cb.answer()
    await show(cb, text, kb)
