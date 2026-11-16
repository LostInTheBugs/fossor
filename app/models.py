from sqlalchemy import String, Integer, Boolean, DateTime, Text, ForeignKey, JSON, Float, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime, timezone
import enum
import uuid as uuid_lib

from app.database import Base


class AuthTypeEnum(str, enum.Enum):
    key = "key"
    password = "password"


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
