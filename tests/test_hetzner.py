"""HetznerProvider against a mocked HTTP API (request shapes, parsing, retries, IP dance)."""
import json

import httpx
import pytest

from app.providers.base import OutOfStock
from app.providers.hetzner import HetznerProvider

DONE = {"id": 1, "status": "success", "command": "x"}


def make(handler) -> tuple[HetznerProvider, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request)

    p = HetznerProvider("tok")
    p._client = httpx.AsyncClient(base_url=p.base_url, transport=httpx.MockTransport(wrapped))
    return p, calls


def server_json(sid=10, ip="1.1.1.1", ip_id=50, status="running"):
    return {
        "id": sid, "name": "srv-1", "status": status, "locked": False, "labels": {},
        "location": {"id": 1, "name": "fsn1"},
        "public_net": {"ipv4": {"id": ip_id, "ip": ip}, "ipv6": {"ip": "2a01:4f8:1:2::/64"}},
    }


async def test_list_plans_uses_per_location_availability():
    def handler(req):
        if req.url.path.endswith("/pricing"):
            return httpx.Response(200, json={"pricing": {"currency": "EUR", "primary_ips": []}})
        assert not req.url.path.endswith("/datacenters"), "datacenters endpoint is gone (410) since 2026-10"
        return httpx.Response(200, json={"server_types": [{
            "id": 1, "name": "cx23", "cores": 2, "memory": 4, "disk": 40, "cpu_type": "shared", "architecture": "x86",
            "prices": [
                {"location": "fsn1", "price_hourly": {"net": "0.006"}, "price_monthly": {"net": "3.5"}, "included_traffic": 21990232555520},
                {"location": "hel1", "price_hourly": {"net": "0.006"}, "price_monthly": {"net": "3.5"}},
                {"location": "nbg1", "price_hourly": {"net": "0.006"}, "price_monthly": {"net": "3.5"}},
            ],
            "locations": [
                {"id": 1, "name": "fsn1", "available": True, "deprecation": None},
                {"id": 2, "name": "hel1", "available": False, "deprecation": None},
                {"id": 3, "name": "nbg1", "available": True, "deprecation": {"announced": "x"}},
            ],
        }], "meta": {"pagination": {"next_page": None}}})

    p, _ = make(handler)
    [plan] = await p.list_plans()
    assert plan.available_in == {"fsn1"}
    assert set(plan.prices) == {"fsn1", "hel1", "nbg1"}
    assert plan.traffic_tb == 20.0


async def test_server_parsing_and_location():
    p, _ = make(lambda req: httpx.Response(200, json={"server": server_json()}))
    info = await p.get_server("10")
    assert info.datacenter == "fsn1" and info.ipv4 == "1.1.1.1" and info.ipv6 == "2a01:4f8:1:2::1"


async def test_reserve_ip_sends_location_not_datacenter():
    def handler(req):
        body = json.loads(req.content)
        assert body["location"] == "hel1" and "datacenter" not in body and body["auto_delete"] is False
        return httpx.Response(201, json={"primary_ip": {"id": 7, "ip": "9.9.9.9", "location": {"name": "hel1"}}, "action": None})

    p, _ = make(handler)
    ip = await p.create_reserve_ip(location_code="hel1", name="x")
    assert ip.address == "9.9.9.9" and ip.datacenter == "hel1"


async def test_rate_limit_is_retried(monkeypatch):
    import app.providers.hetzner as mod

    async def no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    responses = iter([httpx.Response(429, headers={"RateLimit-Reset": "0"}), httpx.Response(200, json={"server": server_json()})])
    p, calls = make(lambda req: next(responses))
    assert (await p.get_server("10")).id == "10"
    assert len(calls) == 2


async def test_out_of_stock_maps_to_exception():
    p, _ = make(lambda req: httpx.Response(412, json={"error": {"code": "resource_unavailable", "message": "sold out"}}))
    with pytest.raises(OutOfStock):
        await p.create_server(name="a", plan_code="cx23", location_code="fsn1", image_code="ubuntu-24.04", labels={})


async def test_change_ip_sequence():
    state = {"off": False}

    def handler(req):
        path, method = req.url.path, req.method
        if method == "GET" and path.endswith("/servers/10"):
            return httpx.Response(200, json={"server": server_json(status="off" if state["off"] else "running")})
        if path.endswith("/actions/poweroff"):
            state["off"] = True
            return httpx.Response(201, json={"action": DONE})
        if path.endswith("/actions/poweron"):
            state["off"] = False
            return httpx.Response(201, json={"action": DONE})
        if path.endswith("/primary_ips/50/actions/unassign"):
            return httpx.Response(201, json={"action": DONE})
        if method == "POST" and path.endswith("/primary_ips"):
            body = json.loads(req.content)
            assert body["assignee_id"] == 10 and body["auto_delete"] is True
            return httpx.Response(201, json={"primary_ip": {"id": 51, "ip": "2.2.2.2", "location": {"name": "fsn1"}}, "action": DONE})
        if method == "DELETE" and path.endswith("/primary_ips/50"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected {method} {path}")

    p, calls = make(handler)
    ip = await p.change_primary_ipv4("10")
    assert ip.address == "2.2.2.2"
    sequence = [f"{c.method} {c.url.path.split('/v1')[1]}" for c in calls if c.method != "GET"]
    assert sequence == [
        "POST /servers/10/actions/poweroff",
        "POST /primary_ips/50/actions/unassign",
        "POST /primary_ips",
        "DELETE /primary_ips/50",
        "POST /servers/10/actions/poweron",
    ]
