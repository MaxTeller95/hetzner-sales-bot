"""Hetzner Cloud provider (https://docs.hetzner.cloud)."""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

import httpx

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

log = logging.getLogger(__name__)

RETRYABLE_CODES = {"locked", "conflict", "rate_limit_exceeded", "timeout", "server_error", "maintenance"}
OUT_OF_STOCK_CODES = {"resource_unavailable", "placement_error"}
STATUS_MAP = {
    "running": "running",
    "off": "off",
    "initializing": "starting",
    "starting": "starting",
    "stopping": "stopping",
    "deleting": "stopping",
    "rebuilding": "rebuilding",
    "migrating": "migrating",
}


class HetznerProvider(CloudProvider):
    kind = "hetzner"
    display_name = "Hetzner Cloud"
    base_url = "https://api.hetzner.cloud/v1"

    def __init__(self, api_token: str, options: dict | None = None):
        super().__init__(api_token, options)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=httpx.Timeout(30, connect=10),
        )
        # Hetzner allows 3600 requests/hour per project; keep concurrency modest.
        self._sem = asyncio.Semaphore(int(self.options.get("max_concurrency", 6)))

    async def close(self) -> None:
        await self._client.aclose()

    # ---- HTTP plumbing ---------------------------------------------------
    async def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        last_error = "unknown error"
        for attempt in range(5):
            try:
                async with self._sem:
                    r = await self._client.request(method, path, params=params, json=json)
            except httpx.TransportError as e:
                last_error = f"network error: {e!r}"
                await asyncio.sleep(2**attempt)
                continue

            if r.status_code == 429:
                reset = r.headers.get("RateLimit-Reset", "")
                wait = int(reset) - int(time.time()) if reset.isdigit() else 2**attempt
                log.warning("Hetzner rate limit hit, sleeping %ss", wait)
                await asyncio.sleep(max(1, min(wait, 60)))
                last_error = "rate limited"
                continue
            if r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code < 400:
                return r.json() if r.content else {}

            try:
                err = r.json().get("error") or {}
            except ValueError:
                err = {}
            code = err.get("code")
            message = err.get("message") or r.text[:300]
            if code in OUT_OF_STOCK_CODES:
                raise OutOfStock(message, code=code)
            raise ProviderError(message, code=code, retryable=code in RETRYABLE_CODES)
        raise ProviderError(f"Hetzner API unavailable ({last_error})", retryable=True)

    async def _get_all(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            data = await self._request("GET", path, params={**(params or {}), "page": page, "per_page": 50})
            items.extend(data.get(key, []))
            next_page = ((data.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not next_page:
                return items
            page = next_page

    async def _wait_action(self, action: dict | None, timeout: float = 600) -> None:
        if not action:
            return
        deadline = time.monotonic() + timeout
        while action["status"] == "running":
            if time.monotonic() > deadline:
                raise ProviderError(f"action {action['id']} ({action.get('command')}) timed out", retryable=True)
            await asyncio.sleep(2)
            action = (await self._request("GET", f"/actions/{action['id']}"))["action"]
        if action["status"] == "error":
            err = action.get("error") or {}
            raise ProviderError(err.get("message", "action failed"), code=err.get("code"))

    async def _action(self, server_id: str, command: str, body: dict | None = None) -> dict:
        data = await self._request("POST", f"/servers/{server_id}/actions/{command}", json=body or {})
        await self._wait_action(data.get("action"))
        return data

    @staticmethod
    def _location_of(obj: dict) -> str | None:
        """Location code of a server / primary IP.

        Hetzner removed `datacenter` from Servers and Primary IPs on 2026-07-01 in favour of
        `location`; older payloads are still understood.
        """
        loc = obj.get("location")
        if isinstance(loc, dict):
            return loc.get("name")
        if isinstance(loc, str):
            return loc
        dc = obj.get("datacenter") or {}
        return (dc.get("location") or {}).get("name") or dc.get("name")

    @staticmethod
    def _parse_server(s: dict) -> ServerInfo:
        net = s.get("public_net") or {}
        v4 = net.get("ipv4") or {}
        v6 = net.get("ipv6") or {}
        ipv6 = v6.get("ip")
        if ipv6 and ipv6.endswith("::/64"):
            ipv6 = ipv6[:-3] + "1"
        return ServerInfo(
            id=str(s["id"]),
            name=s["name"],
            status=STATUS_MAP.get(s.get("status", ""), "unknown"),
            ipv4=v4.get("ip"),
            ipv6=ipv6,
            ipv4_id=str(v4["id"]) if v4.get("id") else None,
            datacenter=HetznerProvider._location_of(s),
            locked=bool(s.get("locked")),
            labels=s.get("labels") or {},
        )

    @staticmethod
    def _parse_ip(p: dict) -> IPInfo:
        return IPInfo(
            id=str(p["id"]),
            address=p["ip"],
            datacenter=HetznerProvider._location_of(p),
            assignee_id=str(p["assignee_id"]) if p.get("assignee_id") else None,
        )

    # ---- catalog ---------------------------------------------------------
    async def list_locations(self) -> list[ProviderLocation]:
        locations = (await self._request("GET", "/locations"))["locations"]
        pricing = (await self._request("GET", "/pricing"))["pricing"]
        currency = pricing.get("currency", "EUR")
        ip_prices: dict[str, tuple[Decimal, Decimal]] = {}
        for entry in pricing.get("primary_ips", []):
            if entry.get("type") == "ipv4":
                for p in entry.get("prices", []):
                    ip_prices[p["location"]] = (
                        Decimal(p["price_hourly"]["net"]),
                        Decimal(p["price_monthly"]["net"]),
                    )
        result = []
        for loc in locations:
            hourly, monthly = ip_prices.get(loc["name"], (Decimal(0), Decimal(0)))
            result.append(
                ProviderLocation(
                    code=loc["name"],
                    name=loc.get("description") or loc["name"],
                    country=loc.get("country"),
                    city=loc.get("city"),
                    currency=currency,
                    ipv4_hourly=hourly,
                    ipv4_monthly=monthly,
                )
            )
        return result

    async def list_plans(self) -> list[ProviderPlan]:
        currency = (await self._request("GET", "/pricing"))["pricing"].get("currency", "EUR")
        types = await self._get_all("/server_types", "server_types")

        plans = []
        for t in types:
            if t.get("deprecated") or t.get("deprecation"):
                continue
            prices: dict[str, tuple[Decimal, Decimal]] = {}
            traffic = None
            for p in t.get("prices", []):
                loc = p["location"]["name"] if isinstance(p.get("location"), dict) else p["location"]
                prices[loc] = (Decimal(p["price_hourly"]["net"]), Decimal(p["price_monthly"]["net"]))
                if p.get("included_traffic"):
                    traffic = round(p["included_traffic"] / 1024**4, 1)
            # Per-location availability lives in server_types[].locations[] since 2026
            # (GET /datacenters returns 410 Gone from 2026-10-01).
            per_location = t.get("locations")
            if per_location is not None:
                avail = {
                    entry.get("name")
                    for entry in per_location
                    if entry.get("available", True) and not entry.get("deprecation")
                } & set(prices)
            else:
                avail = set(prices)
            plans.append(
                ProviderPlan(
                    code=t["name"],
                    name=t["name"].upper(),
                    cores=t["cores"],
                    memory_gb=float(t["memory"]),
                    disk_gb=int(t["disk"]),
                    cpu_type=t.get("cpu_type", "shared"),
                    arch=t.get("architecture", "x86"),
                    currency=currency,
                    prices=prices,
                    available_in=avail,
                    traffic_tb=traffic,
                )
            )
        return plans

    async def list_images(self) -> list[ProviderImage]:
        images = await self._get_all("/images", "images", {"type": "system", "status": "available"})
        merged: dict[str, ProviderImage] = {}
        for img in images:
            if img.get("deprecated"):
                continue
            name = img["name"]
            arch = img.get("architecture", "x86")
            if name in merged:
                archs = set(merged[name].arch.split(",")) | {arch}
                merged[name].arch = ",".join(sorted(archs, reverse=True))
            else:
                merged[name] = ProviderImage(
                    code=name, name=img.get("description") or name, flavor=img.get("os_flavor"), arch=arch
                )
        return list(merged.values())

    # ---- servers ---------------------------------------------------------
    async def create_server(self, *, name, plan_code, location_code, image_code, labels) -> CreatedServer:
        data = await self._request(
            "POST",
            "/servers",
            json={
                "name": name,
                "server_type": plan_code,
                "image": image_code,
                "location": location_code,
                "labels": labels,
                "start_after_create": True,
                "public_net": {"enable_ipv4": True, "enable_ipv6": True},
            },
        )
        await self._wait_action(data.get("action"))
        for nxt in data.get("next_actions") or []:
            await self._wait_action(nxt)
        info = await self.get_server(str(data["server"]["id"]))
        return CreatedServer(server=info or self._parse_server(data["server"]), root_password=data.get("root_password"))

    async def get_server(self, server_id: str) -> ServerInfo | None:
        try:
            data = await self._request("GET", f"/servers/{server_id}")
        except ProviderError as e:
            if e.code == "not_found":
                return None
            raise
        return self._parse_server(data["server"])

    async def find_servers(self, labels: dict[str, str]) -> list[ServerInfo]:
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        servers = await self._get_all("/servers", "servers", {"label_selector": selector})
        return [self._parse_server(s) for s in servers]

    async def delete_server(self, server_id: str) -> None:
        try:
            data = await self._request("DELETE", f"/servers/{server_id}")
        except ProviderError as e:
            if e.code == "not_found":
                return
            raise
        await self._wait_action(data.get("action"))

    async def power_on(self, server_id: str) -> None:
        await self._action(server_id, "poweron")

    async def power_off(self, server_id: str) -> None:
        await self._action(server_id, "poweroff")

    async def shutdown(self, server_id: str) -> None:
        await self._action(server_id, "shutdown")

    async def reboot(self, server_id: str) -> None:
        await self._action(server_id, "reboot")

    async def rebuild(self, server_id: str, image_code: str) -> str | None:
        data = await self._action(server_id, "rebuild", {"image": image_code})
        return data.get("root_password")

    # ---- IPs ---------------------------------------------------------------
    async def _ensure_off(self, server_id: str) -> bool:
        """Power the server off if needed. Returns True if it was running."""
        info = await self.get_server(server_id)
        if info is None:
            raise ProviderError("server not found", code="not_found")
        if info.status == "off":
            return False
        await self.power_off(server_id)
        return True

    async def change_primary_ipv4(self, server_id: str) -> IPInfo:
        info = await self.get_server(server_id)
        if info is None:
            raise ProviderError("server not found", code="not_found")
        old_ip_id = info.ipv4_id
        was_running = await self._ensure_off(server_id)
        try:
            if old_ip_id:
                data = await self._request("POST", f"/primary_ips/{old_ip_id}/actions/unassign")
                await self._wait_action(data.get("action"))
            # Create the new IP while the old one still exists so Hetzner can't hand the same one back.
            data = await self._request(
                "POST",
                "/primary_ips",
                json={
                    "name": f"{info.name}-v4-{int(time.time())}",
                    "type": "ipv4",
                    "assignee_type": "server",
                    "assignee_id": int(server_id),
                    "auto_delete": True,
                },
            )
            await self._wait_action(data.get("action"))
            new_ip = self._parse_ip(data["primary_ip"])
        except Exception:
            if old_ip_id:  # best effort: put the old IP back
                try:
                    data = await self._request(
                        "POST",
                        f"/primary_ips/{old_ip_id}/actions/assign",
                        json={"assignee_id": int(server_id), "assignee_type": "server"},
                    )
                    await self._wait_action(data.get("action"))
                except ProviderError:
                    log.exception("could not re-assign old primary IP %s", old_ip_id)
            if was_running:
                await self.power_on(server_id)
            raise
        if old_ip_id:
            await self.delete_ip(old_ip_id)
        if was_running:
            await self.power_on(server_id)
        return new_ip

    async def create_reserve_ip(self, *, location_code: str, name: str) -> IPInfo:
        data = await self._request(
            "POST",
            "/primary_ips",
            json={
                "name": name,
                "type": "ipv4",
                "assignee_type": "server",
                "location": location_code,
                "auto_delete": False,
            },
        )
        await self._wait_action(data.get("action"))
        return self._parse_ip(data["primary_ip"])

    async def swap_primary_ipv4(self, server_id: str, new_ip_id: str) -> IPInfo:
        info = await self.get_server(server_id)
        if info is None:
            raise ProviderError("server not found", code="not_found")
        old_ip_id = info.ipv4_id
        if not old_ip_id:
            raise ProviderError("server has no primary IPv4")
        old = self._parse_ip((await self._request("GET", f"/primary_ips/{old_ip_id}"))["primary_ip"])
        was_running = await self._ensure_off(server_id)
        try:
            data = await self._request("POST", f"/primary_ips/{old_ip_id}/actions/unassign")
            await self._wait_action(data.get("action"))
            data = await self._request(
                "POST",
                f"/primary_ips/{new_ip_id}/actions/assign",
                json={"assignee_id": int(server_id), "assignee_type": "server"},
            )
            await self._wait_action(data.get("action"))
        except Exception:
            try:
                data = await self._request(
                    "POST",
                    f"/primary_ips/{old_ip_id}/actions/assign",
                    json={"assignee_id": int(server_id), "assignee_type": "server"},
                )
                await self._wait_action(data.get("action"))
            except ProviderError:
                log.exception("could not restore primary IP %s", old_ip_id)
            if was_running:
                await self.power_on(server_id)
            raise
        # The new primary should die with the server; the old one is now a reserve IP and must survive.
        await self._request("PUT", f"/primary_ips/{new_ip_id}", json={"auto_delete": True})
        await self._request("PUT", f"/primary_ips/{old_ip_id}", json={"auto_delete": False})
        if was_running:
            await self.power_on(server_id)
        old.assignee_id = None
        return old

    async def delete_ip(self, ip_id: str) -> None:
        try:
            await self._request("DELETE", f"/primary_ips/{ip_id}")
        except ProviderError as e:
            if e.code != "not_found":
                raise
