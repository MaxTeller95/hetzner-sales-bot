from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def utcnow() -> datetime:
    """Naive UTC datetime. All timestamps in the database are naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ceil_to(value: Decimal, step: int) -> int:
    """Round a positive amount up to the next multiple of `step`."""
    if value <= 0:
        return 0
    if step <= 1:
        return int(value.to_integral_value(rounding=ROUND_CEILING))
    return int((value / step).to_integral_value(rounding=ROUND_CEILING)) * step


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_amount(text: str) -> int | None:
    """Parse an integer typed by a user, accepting Persian/Arabic digits and separators."""
    cleaned = "".join(ch for ch in text.translate(_DIGITS) if ch.isdigit())
    return int(cleaned) if cleaned else None


def fmt_money(amount: int) -> str:
    return f"{amount:,} ریال"


def flag(country: str | None) -> str:
    if not country or len(country) != 2:
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country.upper())


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode()).digest())
    return Fernet(key)


def encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None
