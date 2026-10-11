"""In-memory provider for local development, demos and tests. Never touches a real API."""
from __future__ import annotations

import itertools
import random
import secrets
from decimal import Decimal

from app.providers.base import (
    CloudProvider,
    CreatedServer,
    IPInfo,
    OutOfStock,
    ProviderError,
    ProviderImage,
    ProviderLocation,
    ProviderPlan,
    ServerInfo,
)

_ids = itertools.count(1000)
_servers: dict[str, ServerInfo] = {}
_ips: dict[str, IPInfo] = {}


def reset_state() -> None:
    _servers.clear()
    _ips.clear()


def _random_ip() -> str:
    return f"{random.randint(5, 220)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


class FakeProvider(CloudProvider):
    kind = "fake"
    display_name = "Fake (development)"

    out_of_stock: set[str] = set()

    async def list_locations(self):
        return [
            ProviderLocation("fsn1", "Falkenstein DC Park 1", "DE", "Falkenstein", "EUR", Decimal("0.0008"), Decimal("0.50")),
            ProviderLocation("hel1", "Helsinki DC Park 1", "FI", "Helsinki", "EUR", Decimal("0.0008"), Decimal("0.50")),
        ]

    async def list_plans(self):
        both = {"fsn1", "hel1"}

        def p(code, cores, mem, disk, h, m, arch="x86", cpu="shared"):
            prices = {loc: (Decimal(h), Decimal(m)) for loc in both}
            return ProviderPlan(code, code.upper(), cores, mem, disk, cpu, arch, "EUR", prices, set(both), 20)

        return [
            p("cx22", 2, 4, 40, "0.0063", "3.79"),
            p("cx32", 4, 8, 80, "0.0111", "6.80"),
            p("cax11", 2, 4, 40, "0.0063", "3.79", arch="arm"),
            p("ccx13", 2, 8, 80, "0.0198", "12.49", cpu="dedicated"),
        ]

    async def list_images(self):
        return [
            ProviderImage("ubuntu-24.04", "Ubuntu 24.04", "ubuntu", "x86,arm"),
            ProviderImage("debian-12", "Debian 12", "debian", "x86,arm"),
        ]

    async def create_server(self, *, name, plan_code, location_code, image_code, labels):
        if plan_code in self.out_of_stock:
            raise OutOfStock("plan not available", code="resource_unavailable")
        sid = str(next(_ids))
        ip = IPInfo(str(next(_ids)), _random_ip(), location_code, sid)
        _ips[ip.id] = ip
        info = ServerInfo(sid, name, "running", ip.address, "2a01:4f8::1", ip.id, location_code, labels=dict(labels))
        _servers[sid] = info
        return CreatedServer(info, secrets.token_urlsafe(12))

    async def get_server(self, server_id):
        return _servers.get(server_id)

    async def find_servers(self, labels):
        return [s for s in _servers.values() if all(s.labels.get(k) == v for k, v in labels.items())]

    def _get(self, server_id) -> ServerInfo:
        if server_id not in _servers:
            raise ProviderError("server not found", code="not_found")
        return _servers[server_id]

    async def delete_server(self, server_id):
        s = _servers.pop(server_id, None)
        if s and s.ipv4_id:
            _ips.pop(s.ipv4_id, None)

    async def power_on(self, server_id):
        self._get(server_id).status = "running"

    async def power_off(self, server_id):
        self._get(server_id).status = "off"

    async def shutdown(self, server_id):
        self._get(server_id).status = "off"

    async def reboot(self, server_id):
        self._get(server_id).status = "running"

    async def rebuild(self, server_id, image_code):
        self._get(server_id)
        return secrets.token_urlsafe(12)

    async def change_primary_ipv4(self, server_id):
        s = self._get(server_id)
        _ips.pop(s.ipv4_id or "", None)
        ip = IPInfo(str(next(_ids)), _random_ip(), s.datacenter, server_id)
        _ips[ip.id] = ip
        s.ipv4, s.ipv4_id = ip.address, ip.id
        return ip

    async def create_reserve_ip(self, *, location_code, name):
        ip = IPInfo(str(next(_ids)), _random_ip(), location_code, None)
        _ips[ip.id] = ip
        return ip

    async def swap_primary_ipv4(self, server_id, new_ip_id):
        s = self._get(server_id)
        new = _ips.get(new_ip_id)
        if new is None:
            raise ProviderError("ip not found", code="not_found")
        old = _ips[s.ipv4_id]
        old.assignee_id, new.assignee_id = None, server_id
        s.ipv4, s.ipv4_id = new.address, new.id
        return old

    async def delete_ip(self, ip_id):
        _ips.pop(ip_id, None)
