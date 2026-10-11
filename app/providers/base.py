"""Provider abstraction. Every datacenter integration implements CloudProvider.

See docs/ADDING_A_PROVIDER.md for a walkthrough.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar


class ProviderError(Exception):
    """Raised for any provider API failure.

    `retryable` tells the job runner whether trying again later can help.
    `code` is the provider's machine-readable error code, if any.
    """

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OutOfStock(ProviderError):
    """The requested plan is not currently available in this location."""


@dataclass
class ProviderLocation:
    code: str
    name: str
    country: str | None
    city: str | None
    currency: str
    ipv4_hourly: Decimal = Decimal(0)
    ipv4_monthly: Decimal = Decimal(0)


@dataclass
class ProviderPlan:
    code: str
    name: str
    cores: int
    memory_gb: float
    disk_gb: int
    cpu_type: str
    arch: str
    currency: str
    # location code -> (hourly, monthly) in `currency`
    prices: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    # locations where the plan can be ordered right now
    available_in: set[str] = field(default_factory=set)
    traffic_tb: float | None = None


@dataclass
class ProviderImage:
    code: str
    name: str
    flavor: str | None
    arch: str  # comma-separated, e.g. "x86,arm"


@dataclass
class ServerInfo:
    id: str
    name: str
    status: str  # normalized: running | off | starting | stopping | rebuilding | migrating | unknown
    ipv4: str | None
    ipv6: str | None
    ipv4_id: str | None
    datacenter: str | None
    locked: bool = False
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class CreatedServer:
    server: ServerInfo
    root_password: str | None


@dataclass
class IPInfo:
    id: str
    address: str
    datacenter: str | None
    assignee_id: str | None = None


class CloudProvider(ABC):
    kind: ClassVar[str]
    display_name: ClassVar[str]

    def __init__(self, api_token: str, options: dict | None = None):
        self.api_token = api_token
        self.options = options or {}

    # ---- catalog -------------------------------------------------------
    @abstractmethod
    async def list_locations(self) -> list[ProviderLocation]: ...

    @abstractmethod
    async def list_plans(self) -> list[ProviderPlan]: ...

    @abstractmethod
    async def list_images(self) -> list[ProviderImage]: ...

    # ---- servers -------------------------------------------------------
    @abstractmethod
    async def create_server(
        self, *, name: str, plan_code: str, location_code: str, image_code: str, labels: dict[str, str]
    ) -> CreatedServer:
        """Create a server, wait until it is running and return it with its root password.

        Must raise OutOfStock when the plan cannot be ordered in that location.
        """

    @abstractmethod
    async def get_server(self, server_id: str) -> ServerInfo | None: ...

    @abstractmethod
    async def find_servers(self, labels: dict[str, str]) -> list[ServerInfo]: ...

    @abstractmethod
    async def delete_server(self, server_id: str) -> None: ...

    @abstractmethod
    async def power_on(self, server_id: str) -> None: ...

    @abstractmethod
    async def power_off(self, server_id: str) -> None:
        """Hard power off."""

    @abstractmethod
    async def shutdown(self, server_id: str) -> None:
        """Graceful (ACPI) shutdown; returns once the request is accepted."""

    @abstractmethod
    async def reboot(self, server_id: str) -> None: ...

    @abstractmethod
    async def rebuild(self, server_id: str, image_code: str) -> str | None:
        """Reinstall the OS. Returns the new root password."""

    # ---- IPs -----------------------------------------------------------
    @abstractmethod
    async def change_primary_ipv4(self, server_id: str) -> IPInfo:
        """Give the server a brand-new IPv4 and release the old one."""

    @abstractmethod
    async def create_reserve_ip(self, *, location_code: str, name: str) -> IPInfo:
        """Allocate an unassigned IPv4 that a customer can later swap onto a server."""

    @abstractmethod
    async def swap_primary_ipv4(self, server_id: str, new_ip_id: str) -> IPInfo:
        """Move the reserve IP `new_ip_id` onto the server; returns the server's former IP,
        which is kept (unassigned) so it can become the new reserve IP."""

    @abstractmethod
    async def delete_ip(self, ip_id: str) -> None: ...

    async def close(self) -> None:
        return None
