"""Wallet top-ups (manual card-to-card with admin approval) and balance-driven unsuspension."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Server, ServerStatus, TopUpRequest, TopUpStatus, TxKind, User
from app.services import jobs
from app.services import wallet as wallet_svc
from app.services.errors import UserError
from app.services.runtime_settings import load_settings
from app.utils import utcnow


async def create_request(session: AsyncSession, user: User, amount: int, receipt_file_id: str | None) -> TopUpRequest:
    s = await load_settings(session)
    if amount < s.topup_min_amount:
        raise UserError(f"حداقل مبلغ شارژ {s.topup_min_amount:,} ریال است.")
    req = TopUpRequest(user_id=user.id, amount=amount, receipt_file_id=receipt_file_id)
    session.add(req)
    await session.flush()
    return req


async def approve(
    session: AsyncSession, request_id: int, reviewer: str, amount: int | None = None
) -> tuple[TopUpRequest, list[Server]] | None:
    """Credit the wallet. Returns (request, servers resumed from suspension) or None if not pending."""
    req = await session.get(TopUpRequest, request_id, with_for_update=True)
    if req is None or req.status != TopUpStatus.PENDING:
        return None
    if amount:
        req.amount = amount
    req.status = TopUpStatus.APPROVED
    req.reviewed_at = utcnow()
    req.reviewed_by = reviewer
    await wallet_svc.apply(session, req.user_id, req.amount, TxKind.TOPUP, f"شارژ کیف پول (درخواست #{req.id})")
    resumed = await resume_suspended(session, req.user_id)
    return req, resumed


async def reject(session: AsyncSession, request_id: int, reviewer: str, note: str | None = None) -> TopUpRequest | None:
    req = await session.get(TopUpRequest, request_id, with_for_update=True)
    if req is None or req.status != TopUpStatus.PENDING:
        return None
    req.status = TopUpStatus.REJECTED
    req.reviewed_at = utcnow()
    req.reviewed_by = reviewer
    req.admin_note = note
    return req


async def admin_adjust(session: AsyncSession, user_id: int, amount: int, note: str) -> list[Server]:
    kind = TxKind.TOPUP if amount > 0 else TxKind.ADJUST
    await wallet_svc.apply(session, user_id, amount, kind, note or "تنظیم دستی توسط مدیریت")
    if amount > 0:
        return await resume_suspended(session, user_id)
    return []


async def resume_suspended(session: AsyncSession, user_id: int) -> list[Server]:
    """Power suspended servers back on once the balance is non-negative again."""
    wallet = await wallet_svc.get_wallet(session, user_id)
    if wallet.balance < 0:
        return []
    servers = (
        await session.scalars(select(Server).where(Server.user_id == user_id, Server.status == ServerStatus.SUSPENDED))
    ).all()
    for server in servers:
        server.status = ServerStatus.ACTIVE
        server.suspended_at = None
        if not server.locked:
            await jobs.enqueue(session, "power", {"action": "poweron", "silent": True}, server_id=server.id)
    wallet.low_balance_notified_at = None
    return list(servers)


def parse_presets(raw: str) -> list[int]:
    out = []
    for part in raw.split(","):
        part = part.strip().replace("_", "")
        if part.isdigit():
            out.append(int(part))
    return out


async def presets(session: AsyncSession) -> list[int]:
    return parse_presets((await load_settings(session)).topup_presets)


async def pending_requests(session: AsyncSession) -> list[TopUpRequest]:
    return list(
        (
            await session.scalars(
                select(TopUpRequest).where(TopUpRequest.status == TopUpStatus.PENDING).order_by(TopUpRequest.id)
            )
        ).all()
    )
