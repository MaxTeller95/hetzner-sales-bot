from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IPAddress, IPStatus, Server, ServerStatus, Transaction, User, Wallet
from app.utils import utcnow


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None, first_name: str | None) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, username=username, first_name=first_name)
        session.add(user)
        await session.flush()
        session.add(Wallet(user_id=user.id, balance=0))
        await session.flush()
        await session.refresh(user, ["wallet"])
    elif user.username != username or user.first_name != first_name:
        user.username, user.first_name = username, first_name
    return user


async def get_wallet(session: AsyncSession, user_id: int, *, for_update: bool = False) -> Wallet:
    q = select(Wallet).where(Wallet.user_id == user_id)
    if for_update:
        q = q.with_for_update()
    wallet = await session.scalar(q)
    if wallet is None:
        wallet = Wallet(user_id=user_id, balance=0)
        session.add(wallet)
        await session.flush()
    return wallet


async def apply(
    session: AsyncSession,
    user_id: int,
    amount: int,
    kind: str,
    description: str,
    *,
    server_id: int | None = None,
    ip_id: int | None = None,
) -> Transaction:
    """Credit (amount > 0) or debit (amount < 0) a wallet and record the transaction.

    Balances may go negative: usage that already happened is always billed.
    """
    wallet = await get_wallet(session, user_id, for_update=True)
    wallet.balance += amount
    if wallet.balance < 0 and wallet.negative_since is None:
        wallet.negative_since = utcnow()
    elif wallet.balance >= 0:
        wallet.negative_since = None
    tx = Transaction(
        user_id=user_id,
        amount=amount,
        balance_after=wallet.balance,
        kind=kind,
        description=description[:255],
        server_id=server_id,
        ip_id=ip_id,
    )
    session.add(tx)
    await session.flush()
    return tx


async def hourly_burn(session: AsyncSession, user_id: int) -> int:
    """Approximate IRR the user spends per hour on current resources."""
    hourly = await session.scalar(
        select(func.coalesce(func.sum(Server.hourly_price), 0)).where(
            Server.user_id == user_id, Server.status.in_(ServerStatus.BILLABLE), Server.billing_mode == "h"
        )
    )
    monthly = await session.scalar(
        select(func.coalesce(func.sum(Server.monthly_price), 0)).where(
            Server.user_id == user_id, Server.status.in_(ServerStatus.BILLABLE), Server.billing_mode == "m"
        )
    )
    ips = await session.scalar(
        select(func.coalesce(func.sum(IPAddress.hourly_price), 0)).where(
            IPAddress.user_id == user_id, IPAddress.status.in_(IPStatus.BILLABLE)
        )
    )
    return int(hourly or 0) + int(monthly or 0) // 720 + int(ips or 0)
