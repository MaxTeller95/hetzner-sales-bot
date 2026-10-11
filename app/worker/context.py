from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Provider
from app.providers.base import CloudProvider
from app.providers.registry import get_provider
from app.services.connectivity import ConnectivityChecker
from app.services.notifier import Notifier


@dataclass
class WorkerContext:
    sm: async_sessionmaker
    notifier: Notifier
    checker: ConnectivityChecker
    provider_for: Callable[[Provider], CloudProvider] = get_provider
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
