"""Abuse handling: lock servers on provider locks or abuse e-mails, let admins review/unlock."""
from __future__ import annotations

import email
import imaplib
import ipaddress
import logging
import re
from email.header import decode_header, make_header

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AbuseReport, AbuseStatus, Server, ServerStatus
from app.services import jobs
from app.services.runtime_settings import load_settings
from app.utils import utcnow

log = logging.getLogger(__name__)

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


async def lock_server(
    session: AsyncSession,
    server: Server,
    *,
    reason: str,
    source: str,
    subject: str | None = None,
    body: str | None = None,
    external_id: str | None = None,
) -> AbuseReport | None:
    """Create a report and lock the server. Returns None if this report was already recorded."""
    if external_id and await session.scalar(select(AbuseReport.id).where(AbuseReport.external_id == external_id)):
        return None
    report = AbuseReport(
        server_id=server.id,
        ip=server.ipv4,
        source=source,
        external_id=external_id,
        subject=(subject or reason)[:255],
        body=body,
    )
    session.add(report)
    server.locked = True
    server.lock_reason = reason[:255]
    s = await load_settings(session)
    if s.abuse_auto_poweroff and server.provider_server_id and server.status in ServerStatus.LIVE:
        await jobs.enqueue(session, "power", {"action": "poweroff", "silent": True}, server_id=server.id)
    await session.flush()
    return report


async def unlock_server(session: AsyncSession, server: Server) -> None:
    server.locked = False
    server.lock_reason = None


async def resolve(session: AsyncSession, report: AbuseReport, *, unlock: bool, note: str | None, dismiss: bool = False) -> None:
    report.status = AbuseStatus.DISMISSED if dismiss else AbuseStatus.RESOLVED
    report.resolved_at = utcnow()
    report.admin_note = note
    if unlock and report.server is not None:
        await unlock_server(session, report.server)


def extract_ips(text: str) -> set[str]:
    out = set()
    for candidate in IPV4_RE.findall(text or ""):
        try:
            ip = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if ip.is_global:
            out.add(str(ip))
    return out


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        return "\n".join(parts)
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def fetch_abuse_emails(cfg: Settings, limit: int = 50) -> list[tuple[str, str, str]]:
    """Blocking IMAP fetch of unseen abuse e-mails: [(message_id, subject, body)]. Marks them seen."""
    results = []
    with imaplib.IMAP4_SSL(cfg.abuse_imap_host, cfg.abuse_imap_port) as imap:
        imap.login(cfg.abuse_imap_user, cfg.abuse_imap_password)
        imap.select(cfg.abuse_imap_folder)
        criteria = f'(UNSEEN FROM "{cfg.abuse_sender_filter}")' if cfg.abuse_sender_filter else "(UNSEEN)"
        typ, data = imap.search(None, criteria)
        if typ != "OK":
            return results
        for num in data[0].split()[:limit]:
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            message_id = (msg.get("Message-ID") or f"imap-{num.decode()}").strip()
            results.append((message_id, _decode(msg.get("Subject")), _body(msg)))
            imap.store(num, "+FLAGS", "\\Seen")
    return results


async def servers_by_ip(session: AsyncSession, ips: set[str]) -> list[Server]:
    if not ips:
        return []
    return list(
        (
            await session.scalars(
                select(Server).where(Server.ipv4.in_(ips), Server.status.in_(ServerStatus.LIVE))
            )
        ).all()
    )
