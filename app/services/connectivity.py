"""Reachability test from inside Iran using check-host.net's public API.

A check sends 4 ping packets from each selected Iranian node. A node passes when at least
`connectivity_min_packets` replies come back; the check passes when at least
`connectivity_required_nodes` nodes pass (default 4 nodes × 4/4 packets).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.services.runtime_settings import RuntimeSettings

log = logging.getLogger(__name__)


class ConnectivityUnavailable(Exception):
    """The checking service itself failed; says nothing about the server."""


@dataclass
class NodeResult:
    node: str
    city: str
    ok: int
    total: int
    passed: bool


@dataclass
class CheckResult:
    nodes: list[NodeResult] = field(default_factory=list)
    passed: bool = False

    @property
    def passed_nodes(self) -> int:
        return sum(1 for n in self.nodes if n.passed)

    def summary(self) -> str:
        return f"{self.passed_nodes}/{len(self.nodes)}"

    def details(self) -> str:
        return "\n".join(f"{'✅' if n.passed else '❌'} {n.city}: {n.ok}/{n.total}" for n in self.nodes)


class ConnectivityChecker(Protocol):
    async def check(self, ip: str, s: RuntimeSettings) -> CheckResult: ...


def parse_ping_result(raw: dict, cities: dict[str, str], min_packets: int) -> list[NodeResult]:
    results = []
    for node, value in raw.items():
        pings = value[0] if value and isinstance(value, list) and value[0] else []
        ok = sum(1 for p in pings if p and p[0] == "OK")
        total = len(pings) or 4
        results.append(NodeResult(node, cities.get(node, node.split(".")[0]), ok, total, ok >= min_packets))
    return results


class CheckHostChecker:
    base_url = "https://check-host.net"

    def __init__(self, timeout: float = 45):
        self.timeout = timeout
        self._nodes_cache: tuple[float, dict[str, str]] | None = None

    async def iran_nodes(self, client: httpx.AsyncClient) -> dict[str, str]:
        if self._nodes_cache and time.monotonic() - self._nodes_cache[0] < 3600:
            return self._nodes_cache[1]
        r = await client.get(f"{self.base_url}/nodes/hosts")
        r.raise_for_status()
        nodes = {
            name: (info.get("location") or ["", "", name])[2] or name
            for name, info in r.json().get("nodes", {}).items()
            if (info.get("location") or [""])[0].lower() == "ir"
        }
        self._nodes_cache = (time.monotonic(), nodes)
        return nodes

    async def check(self, ip: str, s: RuntimeSettings) -> CheckResult:
        headers = {"Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as client:
                all_iran = await self.iran_nodes(client)
                configured = [n.strip() for n in s.connectivity_nodes.split(",") if n.strip()]
                nodes = configured or sorted(all_iran)[: max(s.connectivity_required_nodes, 1)]
                if not nodes:
                    raise ConnectivityUnavailable("no Iranian check-host nodes available")
                params = [("host", ip)] + [("node", n) for n in nodes]
                r = await client.get(f"{self.base_url}/check-ping", params=params)
                r.raise_for_status()
                request_id = r.json()["request_id"]

                deadline = time.monotonic() + self.timeout
                raw: dict = {}
                while time.monotonic() < deadline:
                    await asyncio.sleep(3)
                    r = await client.get(f"{self.base_url}/check-result/{request_id}")
                    r.raise_for_status()
                    raw = r.json()
                    if raw and all(v is not None for v in raw.values()):
                        break
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise ConnectivityUnavailable(str(e)) from e

        raw = {n: raw.get(n) for n in nodes}
        node_results = parse_ping_result(raw, all_iran, s.connectivity_min_packets)
        required = min(s.connectivity_required_nodes, len(nodes))
        passed = sum(1 for n in node_results if n.passed) >= required
        return CheckResult(node_results, passed)
