from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

_settings = get_settings()

if _settings.is_sqlite:  # local development / tests
    engine = create_async_engine(_settings.database_url, poolclass=NullPool)
else:
    engine = create_async_engine(_settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
