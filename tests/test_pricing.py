from decimal import Decimal

import pytest

from app.db.models import Location, Plan
from app.services.connectivity import parse_ping_result
from app.services.pricing import PricingNotConfigured, quote_plan, quote_reserve_ip
from app.services.runtime_settings import DEFS, RuntimeSettings
from app.utils import ceil_to, parse_amount


def settings(**over):
    values = {d.key: d.default for d in DEFS}
    values.update(exchange_rate_eur=1_000_000, profit_margin_percent=35, price_round_to=100, monthly_discount_percent=0)
    values.update(over)
    return RuntimeSettings(values)


def plan_loc():
    loc = Location(code="fsn1", name="x", currency="EUR", ipv4_hourly=Decimal("0.0008"), ipv4_monthly=Decimal("0.5"))
    plan = Plan(code="cx22", name="CX22", currency="EUR", base_hourly=Decimal("0.0063"), base_monthly=Decimal("3.79"), cores=2, memory_gb=4, disk_gb=40)
    return plan, loc


def test_quote_applies_rate_margin_and_rounding():
    plan, loc = plan_loc()
    q = quote_plan(plan, loc, settings())
    # (0.0063 + 0.0008) * 1,000,000 * 1.35 = 9585 -> rounded up to 9600
    assert q.hourly == 9600
    assert q.monthly == 9600 * 720
    # 0.0008 * 1,000,000 * 1.35 = 1080 -> 1100
    assert q.ip_hourly == 1100


def test_override_and_monthly_discount():
    plan, loc = plan_loc()
    plan.override_hourly = Decimal("0.01")
    q = quote_plan(plan, loc, settings(monthly_discount_percent=10))
    # (0.01 + 0.0008) * 1e6 * 1.35 = 14580 -> 14600
    assert q.hourly == 14600
    assert q.monthly == ceil_to(Decimal(14600 * 720) * Decimal("0.9"), 100)


def test_reserve_ip_is_double():
    _, loc = plan_loc()
    assert quote_reserve_ip(loc, settings()) == 2200  # 1080 * 2 = 2160 -> 2200


def test_missing_rate_raises():
    plan, loc = plan_loc()
    with pytest.raises(PricingNotConfigured):
        quote_plan(plan, loc, settings(exchange_rate_eur=0))


def test_parse_amount_persian_digits():
    assert parse_amount("۲٬۰۰۰٬۰۰۰ ریال") == 2_000_000
    assert parse_amount("abc") is None


def test_parse_check_host_ping():
    raw = {
        "ir1.node.check-host.net": [[["OK", 0.05, "1.2.3.4"], ["OK", 0.05], ["OK", 0.05], ["OK", 0.05]]],
        "ir2.node.check-host.net": [[["OK", 0.05, "1.2.3.4"], ["TIMEOUT", 3], ["OK", 0.05], ["OK", 0.05]]],
        "ir3.node.check-host.net": [None],
    }
    res = {r.node: r for r in parse_ping_result(raw, {"ir1.node.check-host.net": "Tehran"}, 4)}
    assert res["ir1.node.check-host.net"].passed and res["ir1.node.check-host.net"].city == "Tehran"
    assert res["ir2.node.check-host.net"].ok == 3 and not res["ir2.node.check-host.net"].passed
    assert not res["ir3.node.check-host.net"].passed
