import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/test.db"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["BOT_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "pw"
os.environ["ADMIN_IDS"] = "1"
os.environ["HETZNER_API_TOKEN"] = ""

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.models import Provider  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.providers import fake  # noqa: E402
from app.services import catalog, runtime_settings  # noqa: E402
from app.services.connectivity import CheckResult, ConnectivityUnavailable, NodeResult  # noqa: E402
from app.services.notifier import RecordingNotifier  # noqa: E402
from app.services.wallet import get_or_create_user  # noqa: E402
from app.worker.context import WorkerContext  # noqa: E402


class FakeChecker:
    """Returns failures for the first `fail_times` calls, then passes."""

    def __init__(self, fail_times: int = 0, unavailable: bool = False):
        self.fail_times = fail_times
        self.unavailable = unavailable
        self.calls: list[str] = []

    async def check(self, ip, s):
        self.calls.append(ip)
        if self.unavailable:
            raise ConnectivityUnavailable("down")
        ok = len(self.calls) > self.fail_times
        nodes = [NodeResult(f"ir{i}", f"City{i}", 4 if ok else 0, 4, ok) for i in range(4)]
        return CheckResult(nodes, ok)


async def _no_sleep(_seconds):
    return None


@pytest.fixture(autouse=True)
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    runtime_settings.invalidate()
    fake.reset_state()
    fake.FakeProvider.out_of_stock = set()
    async with SessionLocal() as session:
        await runtime_settings.save_settings(
            session,
            {"exchange_rate_eur": "1000000", "profit_margin_percent": "35", "connectivity_initial_delay": "0", "price_round_to": "100"},
        )
        session.add(Provider(code="demo", kind="fake", name="Demo", options={}))
        await session.commit()
        row = await session.scalar(select(Provider))
        await catalog.sync_provider(session, row)
        await session.commit()
    yield


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def make_ctx(notifier):
    def _make(checker=None):
        return WorkerContext(sm=SessionLocal, notifier=notifier, checker=checker or FakeChecker(), sleep=_no_sleep)

    return _make


@pytest.fixture
async def rich_user():
    from app.services import wallet

    async with SessionLocal() as session:
        user = await get_or_create_user(session, 555, "alice", "Alice")
        await wallet.apply(session, user.id, 50_000_000, "topup", "test topup")
        await session.commit()
        return user
