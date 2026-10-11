"""Database models.

Money conventions:
  * Customer-facing amounts (wallets, transactions, sell prices) are integers in IRR (rial).
  * Provider costs are Decimals in the provider's currency (EUR/USD), as returned by its API.
All datetimes are naive UTC.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils import utcnow

Money = BigInteger
Price = Numeric(16, 8)


class ServerStatus:
    PENDING = "pending"  # paid, waiting for the worker
    CREATING = "creating"
    CHECKING = "checking"  # connectivity test from Iran running
    AWAITING = "awaiting_confirm"  # passed the test, user may still change IP before delivery
    ACTIVE = "active"  # delivered
    SUSPENDED = "suspended"  # powered off because wallet went negative
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"
    POOLED = "pooled"  # pre-created by admin/automation, not yet owned by a customer
    TRANSFERRED = "transferred"  # pool row whose server was handed to a customer row

    BILLABLE = (PENDING, CREATING, CHECKING, AWAITING, ACTIVE, SUSPENDED)
    VISIBLE = BILLABLE + (DELETING,)
    LIVE = (CREATING, CHECKING, AWAITING, ACTIVE, SUSPENDED, DELETING, POOLED)


class IPStatus:
    CREATING = "creating"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"

    BILLABLE = (CREATING, ACTIVE)
    VISIBLE = (CREATING, ACTIVE, DELETING)


class TxKind:
    TOPUP = "topup"
    CHARGE = "charge"
    REFUND = "refund"
    ADJUST = "adjust"


class TopUpStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class AbuseStatus:
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    tos_shown_at: Mapped[datetime | None] = mapped_column(DateTime)
    tos_accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    wallet: Mapped[Wallet] = relationship(back_populates="user", uselist=False, lazy="selectin")

    @property
    def display(self) -> str:
        return f"@{self.username}" if self.username else (self.first_name or str(self.telegram_id))


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    balance: Mapped[int] = mapped_column(Money, default=0)
    negative_since: Mapped[datetime | None] = mapped_column(DateTime)
    low_balance_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="wallet", lazy="selectin")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Money)  # signed: + credit, - debit
    balance_after: Mapped[int] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    ip_id: Mapped[int | None] = mapped_column(ForeignKey("ips.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class BillingEvent(Base):
    """One billed unit of usage (an hour, a month, or a one-off minimum hour)."""

    __tablename__ = "billing_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    resource_type: Mapped[str] = mapped_column(String(16))  # server | ip | ip_change
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    ip_id: Mapped[int | None] = mapped_column(ForeignKey("ips.id", ondelete="SET NULL"))
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    hours: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Money)  # IRR charged to the customer
    provider_cost: Mapped[Decimal] = mapped_column(Price, default=0)  # what the provider bills us
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class TopUpRequest(Base):
    __tablename__ = "topup_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(16), default=TopUpStatus.PENDING, index=True)
    method: Mapped[str] = mapped_column(String(32), default="manual")
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    admin_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(lazy="selectin")


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. "hetzner-main"
    kind: Mapped[str] = mapped_column(String(32))  # implementation key in the provider registry
    name: Mapped[str] = mapped_column(String(64))  # shown to users
    api_token_enc: Mapped[str | None] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("provider_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    ipv4_hourly: Mapped[Decimal] = mapped_column(Price, default=0)
    ipv4_monthly: Mapped[Decimal] = mapped_column(Price, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    provider: Mapped[Provider] = relationship(lazy="selectin")


class Plan(Base):
    """A server type in a specific location (prices differ per location)."""

    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("provider_id", "location_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    cores: Mapped[int] = mapped_column(Integer)
    memory_gb: Mapped[float] = mapped_column(Float)
    disk_gb: Mapped[int] = mapped_column(Integer)
    cpu_type: Mapped[str] = mapped_column(String(16), default="shared")
    arch: Mapped[str] = mapped_column(String(8), default="x86")
    traffic_tb: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    base_hourly: Mapped[Decimal] = mapped_column(Price)  # provider price, synced
    base_monthly: Mapped[Decimal] = mapped_column(Price)  # provider monthly cap, synced
    override_hourly: Mapped[Decimal | None] = mapped_column(Price)  # admin-set base price (same currency)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # admin switch
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)  # provider stock
    pool_target: Mapped[int] = mapped_column(Integer, default=0)  # servers to keep pre-created
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    provider: Mapped[Provider] = relationship(lazy="selectin")
    location: Mapped[Location] = relationship(lazy="selectin")

    @property
    def sell_base_hourly(self) -> Decimal:
        return self.override_hourly if self.override_hourly is not None else self.base_hourly

    @property
    def specs(self) -> str:
        mem = int(self.memory_gb) if float(self.memory_gb).is_integer() else self.memory_gb
        return f"{self.cores}C / {mem}G / {self.disk_gb}G"


class OSImage(Base):
    __tablename__ = "os_images"
    __table_args__ = (UniqueConstraint("provider_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    flavor: Mapped[str | None] = mapped_column(String(32))
    arch: Mapped[str] = mapped_column(String(32), default="x86")  # comma-separated supported archs
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def supports(self, arch: str) -> bool:
        return arch in self.arch.split(",")


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    image_id: Mapped[int | None] = mapped_column(ForeignKey("os_images.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), index=True)
    billing_mode: Mapped[str] = mapped_column(String(1), default="h")  # h=hourly, m=monthly

    provider_server_id: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_ip_id: Mapped[str | None] = mapped_column(String(64))
    datacenter: Mapped[str | None] = mapped_column(String(32))
    ipv4: Mapped[str | None] = mapped_column(String(45), index=True)
    ipv6: Mapped[str | None] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(32), default="root")
    root_password_enc: Mapped[str | None] = mapped_column(Text)
    power_status: Mapped[str] = mapped_column(String(24), default="unknown")

    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    lock_reason: Mapped[str | None] = mapped_column(String(255))

    purchase_ip_changes: Mapped[int] = mapped_column(Integer, default=0)
    last_ip_change_at: Mapped[datetime | None] = mapped_column(DateTime)
    check_summary: Mapped[str | None] = mapped_column(Text)
    check_passed: Mapped[bool | None] = mapped_column(Boolean)

    # Prices snapshotted at purchase time (IRR)
    hourly_price: Mapped[int] = mapped_column(Money, default=0)
    monthly_price: Mapped[int] = mapped_column(Money, default=0)
    ip_hourly_price: Mapped[int] = mapped_column(Money, default=0)
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    progress_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    progress_message_id: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User | None] = relationship(lazy="selectin")
    provider: Mapped[Provider] = relationship(lazy="selectin")
    plan: Mapped[Plan] = relationship(lazy="selectin")
    location: Mapped[Location] = relationship(lazy="selectin")
    image: Mapped[OSImage | None] = relationship(lazy="selectin")

    @property
    def is_online(self) -> bool:
        return self.power_status == "running"


class IPAddress(Base):
    """Reserve (spare) IPv4 addresses owned by a customer. Primary IPs live on the server row."""

    __tablename__ = "ips"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"))
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    datacenter: Mapped[str | None] = mapped_column(String(32))
    provider_ip_id: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(45), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    hourly_price: Mapped[int] = mapped_column(Money, default=0)
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(lazy="selectin")
    provider: Mapped[Provider] = relationship(lazy="selectin")
    location: Mapped[Location] = relationship(lazy="selectin")


class AbuseReport(Base):
    __tablename__ = "abuse_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    ip: Mapped[str | None] = mapped_column(String(45))
    source: Mapped[str] = mapped_column(String(16))  # provider | email | admin
    external_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=AbuseStatus.OPEN, index=True)
    admin_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    server: Mapped[Server | None] = relationship(lazy="selectin")


class SettingKV(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Job(Base):
    """Durable background job, executed by the worker process."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    ip_id: Mapped[int | None] = mapped_column(ForeignKey("ips.id", ondelete="SET NULL"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
