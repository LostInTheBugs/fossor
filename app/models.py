from sqlalchemy import String, Integer, Boolean, DateTime, Text, ForeignKey, JSON, Float, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime, timezone
import enum
import uuid as uuid_lib

from app.database import Base


class AuthTypeEnum(str, enum.Enum):
    key = "key"
    password = "password"
    # §4.1 bis mode 2: Fossor connects with its own ed25519 key.
    fossor_key = "fossor_key"


class SudoTypeEnum(str, enum.Enum):
    none = "none"
    nopasswd = "sudo without password"
    withpasswd = "sudo with password"


class OsKindEnum(str, enum.Enum):
    hiveos = "hiveos"
    ubuntu = "ubuntu"
    proxmox = "proxmox"
    other = "other"


class UsageEnum(str, enum.Enum):
    cpu = "cpu"
    gpu = "gpu"
    both = "both"
    none = "none"


class ActorEnum(str, enum.Enum):
    fred = "fred"
    hermes = "hermes"
    scheduler = "scheduler"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, default=lambda: str(uuid_lib.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(50), default="root")
    auth_type: Mapped[AuthTypeEnum] = mapped_column(String(20), default=AuthTypeEnum.key)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    sudo: Mapped[SudoTypeEnum] = mapped_column(String(30), default=SudoTypeEnum.none)
    os_kind: Mapped[OsKindEnum] = mapped_column(String(20), default=OsKindEnum.other)
    host_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    host_key_public: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[UsageEnum] = mapped_column(String(10), default=UsageEnum.both)
    vm_host: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # §4.15: CPU and platform power are typed in by the operator (no sensor).
    cpu_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    platform_power_w: Mapped[float] = mapped_column(Float, default=50.0)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())

    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="machine", cascade="all, delete-orphan")


class Device(Base):
    """CPU or GPU detected on a machine (§4.2)."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # cpu | gpu
    index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vram_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pci_bus: Mapped[str | None] = mapped_column(String(20), nullable=True)
    driver: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ram_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flags: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # NVIDIA UUID, kept only as a matching key when pci_bus is unavailable.
    nv_uuid: Mapped[str | None] = mapped_column(String(50), nullable=True)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled_for_mining: Mapped[bool] = mapped_column(Boolean, default=True)
    # Integrated GPU (Intel UHD/Iris, AMD APU Radeon Graphics, ASPEED/Matrox BMC):
    # detected but never enabled for mining by default.
    integrated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class AiProvider(Base):
    """AI analysis provider (§4.12) — one ``primary`` and an optional ``fallback``."""

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    which: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)  # primary | fallback
    provider_type: Mapped[str] = mapped_column(String(30), default="hermes-agent")
    name: Mapped[str] = mapped_column(String(100), default="")
    base_url: Mapped[str] = mapped_column(String(255), default="")
    api_key_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(100), default="")
    timeout_s: Mapped[int] = mapped_column(Integer, default=120)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024)
    reasoning_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    extra_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class AppSetting(Base):
    """Key/value application settings (exported by §4.13)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class MachineStatus(Base):
    """Last known machine-level state, rewritten by every collection round."""

    __tablename__ = "machine_status"

    machine_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    miner: Mapped[str] = mapped_column(String(50), default="")
    algo: Mapped[str] = mapped_column(String(50), default="")
    coin: Mapped[str] = mapped_column(String(20), default="")
    uptime_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_hashrate_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)


class Metric(Base):
    """One row per device (or per machine when no device matched) per round."""

    __tablename__ = "metrics"
    __table_args__ = (Index("ix_metrics_machine_ts", "machine_uuid", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_uuid: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    device_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    hashrate_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    fan_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class Coin(Base):
    """Token catalogue (§4.3): ticker, links, verified CoinGecko id, exchanges, manual price."""

    __tablename__ = "coins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    algo: Mapped[str] = mapped_column(String(160), default="")
    kind: Mapped[str] = mapped_column(String(10), default="both")  # cpu | gpu | both
    site_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ann_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    explorer_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Only a *verified* id may be used for pricing: a wrong id silently prices another coin.
    coingecko_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    coingecko_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    exchanges: Mapped[list] = mapped_column(JSON, default=list)
    manual_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    manual_price_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class Wallet(Base):
    """Wallet address (§4.4). Never a seed, a password or a private key."""

    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("coin_ticker", "address", name="uq_wallet_coin_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin_ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(200), default="")  # machine / VM where the wallet lives
    backup_path: Mapped[str] = mapped_column(String(500), default="")  # path only
    pool: Mapped[str] = mapped_column(String(30), default="")  # luckypool | rplant | solo | ""
    # Luckypool per-coin subdomain (``pearl``, ``qtc``…); empty = derived from ticker.
    pool_host: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class MinedCampaign(Base):
    """A mining campaign — running (``active``) or imported from the legacy history."""

    __tablename__ = "mined_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Stable identity for idempotent imports (``current``, ``wam_spec``, ``history:<hash>``).
    legacy_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="legacy")
    coin_ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    coin_name: Mapped[str] = mapped_column(String(160), default="")
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | ended | blocked
    start_iso: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_iso: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mined_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    mined_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    mined_unit: Mapped[str] = mapped_column(String(20), default="")
    verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    machine: Mapped[str | None] = mapped_column(Text, nullable=True)
    pool: Mapped[str | None] = mapped_column(Text, nullable=True)
    algo: Mapped[str | None] = mapped_column(Text, nullable=True)
    wallet_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashrate: Mapped[str | None] = mapped_column(Text, nullable=True)
    links: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_note: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class PoolBalance(Base):
    """Last known pool balance of one wallet (§4.16) — one row per wallet."""

    __tablename__ = "pool_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(Integer, ForeignKey("wallets.id"), unique=True, nullable=False)
    pool: Mapped[str] = mapped_column(String(30), nullable=False)
    coin_ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    paid: Mapped[float | None] = mapped_column(Float, nullable=True)
    unpaid: Mapped[float | None] = mapped_column(Float, nullable=True)
    immature: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_payment_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_payment_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_payment_tx: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class PoolBalanceSample(Base):
    """One dated pool-balance reading, used for the §4.15 revenue (24 h delta)."""

    __tablename__ = "pool_balance_samples"
    __table_args__ = (Index("ix_pool_samples_wallet_ts", "wallet_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(Integer, ForeignKey("wallets.id"), nullable=False)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    paid: Mapped[float | None] = mapped_column(Float, nullable=True)
    unpaid: Mapped[float | None] = mapped_column(Float, nullable=True)
    immature: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    actor: Mapped[ActorEnum] = mapped_column(String(20), default=ActorEnum.fred)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    machine_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("machines.id"), nullable=True)
    object_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result: Mapped[str] = mapped_column(String(200), default="ok")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)

    machine: Mapped[Machine | None] = relationship(back_populates="audit_logs")
