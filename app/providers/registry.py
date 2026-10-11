"""Maps Provider.kind to an implementation and caches client instances."""
from __future__ import annotations

from app.config import get_settings
from app.db.models import Provider
from app.providers.base import CloudProvider
from app.providers.fake import FakeProvider
from app.providers.hetzner import HetznerProvider
from app.utils import decrypt

# Register new provider implementations here.
PROVIDER_CLASSES: dict[str, type[CloudProvider]] = {
    HetznerProvider.kind: HetznerProvider,
    FakeProvider.kind: FakeProvider,
}

_instances: dict[int, tuple[str, CloudProvider]] = {}


def provider_token(row: Provider) -> str:
    token = decrypt(row.api_token_enc) or ""
    if not token and row.kind == "hetzner":
        token = get_settings().hetzner_api_token
    return token


def get_provider(row: Provider) -> CloudProvider:
    cls = PROVIDER_CLASSES.get(row.kind)
    if cls is None:
        raise ValueError(f"unknown provider kind {row.kind!r}")
    token = provider_token(row)
    cached = _instances.get(row.id)
    if cached and cached[0] == token:
        return cached[1]
    instance = cls(token, row.options or {})
    _instances[row.id] = (token, instance)
    return instance


async def close_all() -> None:
    for _, inst in _instances.values():
        await inst.close()
    _instances.clear()
