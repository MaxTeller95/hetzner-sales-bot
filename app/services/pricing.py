"""Price conversion: provider price (EUR/USD) → IRR with profit margin.

    sell_hourly  = ceil((plan_hourly + ipv4_hourly) × rate × (1 + margin))
    sell_monthly = ceil(sell_hourly × 720 × (1 − monthly_discount))
    ip_hourly    = ceil(ipv4_hourly × rate × (1 + margin))          # one-off IP-change charge
    reserve_ip   = ceil(ipv4_hourly × rate × (1 + margin) × multiplier)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.db.models import Location, Plan
from app.services.runtime_settings import RuntimeSettings
from app.utils import ceil_to

HOURS_PER_MONTH = 720


class PricingNotConfigured(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    hourly: int
    monthly: int
    ip_hourly: int


def rate_for(currency: str, s: RuntimeSettings) -> Decimal:
    rates = {"EUR": s.exchange_rate_eur, "USD": s.exchange_rate_usd, "IRR": 1}
    rate = rates.get(currency.upper(), 0)
    if not rate:
        raise PricingNotConfigured(f"exchange rate for {currency} is not set")
    return Decimal(rate)


def _sell(amount: Decimal, currency: str, s: RuntimeSettings, multiplier: float = 1) -> Decimal:
    margin = Decimal(str(s.profit_margin_percent)) / 100
    return Decimal(amount) * rate_for(currency, s) * (1 + margin) * Decimal(str(multiplier))


def quote_plan(plan: Plan, location: Location, s: RuntimeSettings) -> Quote:
    base = _sell(plan.sell_base_hourly, plan.currency, s) + _sell(location.ipv4_hourly or 0, location.currency, s)
    hourly = ceil_to(base, s.price_round_to)
    discount = 1 - Decimal(str(s.monthly_discount_percent)) / 100
    monthly = ceil_to(Decimal(hourly) * HOURS_PER_MONTH * discount, s.price_round_to)
    ip_hourly = ceil_to(_sell(location.ipv4_hourly or 0, location.currency, s), s.price_round_to)
    return Quote(hourly=hourly, monthly=monthly, ip_hourly=ip_hourly)


def quote_reserve_ip(location: Location, s: RuntimeSettings) -> int:
    return ceil_to(_sell(location.ipv4_hourly or 0, location.currency, s, s.reserve_ip_multiplier), s.price_round_to)


def provider_hourly_cost(plan: Plan, location: Location) -> Decimal:
    return Decimal(plan.base_hourly) + Decimal(location.ipv4_hourly or 0)


def provider_monthly_cost(plan: Plan, location: Location) -> Decimal:
    """Hetzner bills hourly but caps at the monthly price."""
    server = min(Decimal(plan.base_hourly) * HOURS_PER_MONTH, Decimal(plan.base_monthly))
    ip = min(Decimal(location.ipv4_hourly or 0) * HOURS_PER_MONTH, Decimal(location.ipv4_monthly or 0) or Decimal("Infinity"))
    return server + ip
