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
    # Machine created but provisioning (temporary login) hasn't succeeded yet.
    provision_pending = "provision_pending"


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
    auth_type: Mapped[AuthTypeEnum] = mapped_column(String(20), default=AuthTypeEnum.provision_pending)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    sudo: Mapped[SudoTypeEnum] = mapped_column(String(30), default=SudoTypeEnum.none)
    os_kind: Mapped[OsKindEnum] = mapped_column(String(20), default=OsKindEnum.other)
    # §4.20 : « host » = machine jointe en SSH, « asic » = ASIC joint par IP,
    # sans SSH et en lecture seule.
    machine_kind: Mapped[str] = mapped_column(String(10), default="host", server_default="host")
    asic_driver: Mapped[str | None] = mapped_column(String(20), nullable=True)
    asic_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    asic_expected_hashrate_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    asic_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 2026.11.008 : connexion au panneau IceRiver (firmware ICM168 en HTTPS).
    # Le mot de passe est chiffré comme les autres secrets ; le certificat
    # auto-signé est épinglé par son empreinte SHA-256, comme une clé d'hôte SSH.
    asic_user: Mapped[str | None] = mapped_column(String(80), nullable=True)
    asic_password_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    asic_cert_sha256: Mapped[str | None] = mapped_column(String(80), nullable=True)
    asic_scheme: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # §4.1 ter : type choisi à l'ajout (os_kind) vs OS réellement détecté.
    os_kind_detected: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # §3e : dernière sonde HiveOS **masquée** (aucun RIG_PASSWD, aucun wallet).
    hiveos_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    host_key_public: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Last external miners seen (JSON list, masked) — dashboard + machine card.
    external_miners: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[UsageEnum] = mapped_column(String(10), default=UsageEnum.both)
    vm_host: Mapped[bool] = mapped_column(Boolean, default=False)
    # §4.17 : machine qui héberge Fossor — aucune coupure d'alimentation permise.
    fossor_host: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Préprod : la machine accepte le pilotage réel (« PILOTER ») quand le
    # global sera passé en réel.  0 = simulation héritée du global.
    simulation_disabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    # §10.1 : reboot et power cycle du watchdog exigent un mot « VALIDER »
    # par machine.  Les deux restent faux tant que l'opérateur n'a pas confirmé.
    watchdog_reboot_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    watchdog_power_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # §4.15: CPU and platform power are typed in by the operator (no sensor).
    cpu_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    platform_power_w: Mapped[float] = mapped_column(Float, default=50.0)
    # §4.5: per-machine override of the default miner profile (null = global).
    default_miner_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())

    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="machine", cascade="all, delete-orphan")


class MinerProfile(Base):
    """Miner profile (§4.5): what to run to mine one coin.

    The command template is validated and rendered by :mod:`app.miners`; the
    binary is fetched by the target machine and checked against ``sha256``
    before anything is extracted.
    """

    __tablename__ = "miner_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    coin: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    wallet: Mapped[str] = mapped_column(String(128), nullable=False)
    pool: Mapped[str] = mapped_column(String(200), nullable=False)
    software: Mapped[str] = mapped_column(String(20), nullable=False)
    binary_url: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    binary_path: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    cmdline: Mapped[str] = mapped_column(Text, nullable=False)
    api_port: Mapped[int] = mapped_column(Integer, default=0)
    # Type d'API du mineur (cpuminer: -b 127.0.0.1:port, ``/summary``).
    api_kind: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # ``url`` = release publique ; ``fossor`` = archive déposée dans Fossor.
    binary_source: Mapped[str] = mapped_column(String(10), default="url", server_default="url")
    binary_stored: Mapped[str] = mapped_column(String(300), default="", server_default="")
    binary_size: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Type du dépôt, détecté aux octets magiques : elf, tar.gz, tar.xz, zip.
    binary_kind: Mapped[str] = mapped_column(String(10), default="", server_default="")
    # ``file`` = wallet local à sauvegarder ; ``pool_address`` = adresse seule.
    wallet_mode: Mapped[str] = mapped_column(String(16), default="file", server_default="file")
    execution_mode: Mapped[str] = mapped_column(String(10), default="host")
    threads: Mapped[int] = mapped_column(Integer, default=0)
    # Nom de worker/RIG du gabarit ; vide = nom de la machine.
    worker: Mapped[str] = mapped_column(String(32), default="", server_default="")
    # Gouverneur CPU « performance » au démarrage (ExecStartPre en root).
    cpu_governor: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Pseudo-terminal fourni par ``script`` : SRBMiner plante sans TTY (SIGSEGV).
    tty_wrapper: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


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
    # GPU occupied by an external miner not managed by Fossor.
    occupied_external: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 2026.11.023-bis : qui occupe le device.  ``external`` est le seul cas qui
    # bloque Fossor ; ``fossor_default`` / ``fossor_campaign`` / ``hiveos`` sont
    # libérables (arrêt de l'unité, ou ``miner stop`` sur HiveOS).
    occupied_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    occupied_campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # §4.19 : rôle du device vis-à-vis du token par défaut et du spec mining.
    # ``default_only`` : jamais pris par une campagne ; ``default_and_spec`` : peut
    # l'être, puis revient au token par défaut.  Les devices existants passent à
    # ``default_and_spec`` par migration.
    role: Mapped[str] = mapped_column(
        String(20), default="default_and_spec", server_default="default_and_spec"
    )
    # §4.19/§10 : le device fait partie du groupe mobilisé par le mode boost.
    boost_pool: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # §4.19 : token par défaut propre au device (sinon celui de la machine, sinon le global).
    default_token_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # §4.2 bis : alimentation qui porte le device, et seconde PSU quand un GPU
    # est réparti (part slot/riser sur psu_id, le reste sur psu2_id).
    psu_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    psu2_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    split_w: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    # §4.6 (003-c2): SRBMiner API unreachable from the container *and* over SSH.
    api_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2026.11.003-c7: CPU state + per-CPU-miner hashrate JSON (charge,
    # température, fréquence, puissance, source de chaque hashrate).
    cpu_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # §4.20 : état normalisé d'un ASIC (hashrate, domaine inactif, pool…).
    asic_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2026.11 : état par profil (unité, actif, hashrate, shares) — plusieurs
    # profils peuvent tourner sur une même machine (tokens par device).
    profiles_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class RawCollection(Base):
    """Last raw ``collect_stats`` output of a machine, masked (§4.7 bis).

    One row per machine (``machine_uuid`` primary key): the diagnostic screen
    shows it to an admin so a parser can be fixed without an SSH session.
    """

    __tablename__ = "raw_collections"

    machine_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    stdout: Mapped[str] = mapped_column(Text, default="")


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


class MetricAgg(Base):
    """Buckets horaires/journaliers des métriques (§10.7).

    Les données brutes de ``metrics`` sont agrégées par (machine ou device),
    par heure puis par jour ; la purge et l'agrégation sont bornées et
    journalisées (voir ``app.history``).
    """

    __tablename__ = "metric_aggs"
    __table_args__ = (
        UniqueConstraint("scope", "scope_uuid", "bucket", "ts", name="uq_metric_agg"),
        Index("ix_metric_aggs_scope_ts", "scope", "scope_uuid", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)  # machine | device
    scope_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    bucket: Mapped[str] = mapped_column(String(4), nullable=False)  # hour | day
    ts: Mapped[str] = mapped_column(String(30), nullable=False)
    hashrate_avg_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    hashrate_max_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_avg_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_max_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_plug_avg_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_plug_max_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_avg_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_max_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    samples: Mapped[int] = mapped_column(Integer, default=0)


class PlugPowerSample(Base):
    """Puissance à la prise d'une machine (§10.7), un point par collecte.

    ``power_w`` vient du calcul alimentation (DC / rendement) ; ``source``
    vaut ``psu`` quand au moins une PSU est déclarée, ``dc`` sinon (la valeur
    est alors la puissance DC mesurée, pas une mesure à la prise).
    """

    __tablename__ = "plug_power_samples"
    __table_args__ = (Index("ix_plug_samples_machine_ts", "machine_uuid", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    dc_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="dc")


class PriceSample(Base):
    """Prix daté d'un token (§10.7 / §4.16) — sert à valoriser une réception
    à sa date. Aucun prix n'est inventé : sans point antérieur, la valeur
    reste vide dans l'export comptable.
    """

    __tablename__ = "price_samples"
    __table_args__ = (Index("ix_price_samples_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="")


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
    # Pool host substrings used to infer a coin from a miner's pool URL
    # (``pearl-eu2.luckypool.io`` → PRL).  JSON list of hosts.
    pool_hosts: Mapped[list] = mapped_column(JSON, default=list)
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
    # Parité mining.html : prolongations et notes datées reprises de mining-status.json.
    events: Mapped[list] = mapped_column(JSON, default=list)
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


class PoolPayment(Base):
    """One payment kept from a pool's history (§4.16) — never a full address.

    ``dedupe_key`` is ``pool + tx`` when the transaction exists, otherwise
    ``pool + received_at + amount``; its unique constraint makes repeated
    syncs idempotent even when a pool omits the transaction hash.
    """

    __tablename__ = "pool_payments"
    __table_args__ = (
        Index("ix_pool_payments_coin_date", "coin_ticker", "received_at"),
        Index("ix_pool_payments_wallet_date", "wallet_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("wallets.id"), nullable=True)
    coin_ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    pool: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    received_at: Mapped[str] = mapped_column(String(30), nullable=False)
    tx: Mapped[str] = mapped_column(String(160), default="")
    address_hint: Mapped[str] = mapped_column(String(24), default="")
    source: Mapped[str] = mapped_column(String(30), default="luckypool")
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class SoloBlock(Base):
    """A block found by a solo node (§4.16), one row per height."""

    __tablename__ = "solo_blocks"
    __table_args__ = (UniqueConstraint("coin_ticker", "height", name="uq_solo_coin_height"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin_ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    reward: Mapped[float] = mapped_column(Float, nullable=False)
    found_at: Mapped[str] = mapped_column(String(30), nullable=False)
    node: Mapped[str] = mapped_column(String(120), default="")
    tx: Mapped[str] = mapped_column(String(160), default="")
    source: Mapped[str] = mapped_column(String(20), default="solo")
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class NotificationLog(Base):
    """One queued/sent notification: the queue *and* the journal (§4.14)."""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    event: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    level: Mapped[str] = mapped_column(String(10), default="info")
    subject: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # pending → sent | failed | skipped (heures silencieuses)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class NotificationState(Base):
    """Small persistent state for debounce / dedupe (offline counters, last alerts)."""

    __tablename__ = "notification_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    actor: Mapped[str] = mapped_column(String(20), default="fred")
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    machine_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("machines.id"), nullable=True)
    object_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result: Mapped[str] = mapped_column(String(200), default="ok")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)

    machine: Mapped[Machine | None] = relationship(back_populates="audit_logs")


class Psu(Base):
    """Alimentation d'une machine (§4.2 bis) — une ou plusieurs par machine."""

    __tablename__ = "psus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    rated_w: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    certification: Mapped[str] = mapped_column(String(12), default="none", server_default="none")
    amps_12v: Mapped[float | None] = mapped_column(Float, nullable=True)
    derate_pct: Mapped[float] = mapped_column(Float, default=100.0, server_default="100")
    measured_efficiency: Mapped[float | None] = mapped_column(Float, nullable=True)
    brand: Mapped[str] = mapped_column(String(60), default="", server_default="")
    model: Mapped[str] = mapped_column(String(80), default="", server_default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class MachineComponent(Base):
    """Autre composant inventorié (§4.2 bis) : RAM, disque, carte, ventilateur…

    ``watts`` est la puissance d'une unité, ``quantity`` le nombre ; la charge
    d'une PSU somme ``watts × quantity`` des composants qui lui sont affectés.
    """

    __tablename__ = "machine_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    watts: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    measured: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    source: Mapped[str] = mapped_column(String(10), default="manual", server_default="manual")
    psu_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("psus.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class Campaign(Base):
    """Campagne de minage (§4.7) : un profil, des devices, une fenêtre horaire.

    Le statut suit la machine à états de :mod:`app.campaigns` — aucune écriture
    ne le change sans passer par elle.  Les colonnes ``restore_*`` et
    ``attempts`` sont la mémoire du planificateur : après un redémarrage du
    conteneur, il relit l'état et reprend l'étape en cours au lieu de la rejouer.
    """

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    coin: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(Integer, ForeignKey("miner_profiles.id"), nullable=False)
    start_at: Mapped[str] = mapped_column(String(40), nullable=False)
    # NULL = sans limite (valeur par défaut pour une campagne CPU, §4.7).
    end_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed", server_default="proposed", index=True)
    origin: Mapped[str] = mapped_column(String(10), default="manual", server_default="manual")
    # §4.6 : mode d'exécution de la campagne (host, vm ou hybrid).
    execution_mode: Mapped[str] = mapped_column(String(10), default="host", server_default="host")
    # Arrêt demandé à la main avant l'heure de fin.
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # §4.2 bis : démarrage au-delà de 80 % de charge PSU accepté explicitement.
    confirm_overload: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    start_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_unit: Mapped[str] = mapped_column(String(20), default="", server_default="")
    balance_source: Mapped[str] = mapped_column(String(60), default="", server_default="")
    gain: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    tariff_eur_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Retour au token par défaut : délai, échéance de la tentative en cours.
    restore_timeout_s: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    restore_deadline: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # None = pas encore vérifié ; 0/1 = vérifié (1 = hashrate revenu).
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # §4.9/§5.4 : analyse Hermes qui a produit cette proposition (NULL sinon).
    analysis_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("analyses.id"), nullable=True, index=True
    )
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())


class CampaignDevice(Base):
    """Device engagé par une campagne : une machine, et le device précis."""

    __tablename__ = "campaign_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"), nullable=False)
    device_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), default="gpu", server_default="gpu")
    label: Mapped[str] = mapped_column(String(120), default="", server_default="")


class CampaignEvent(Base):
    """Journal d'événements d'une campagne : horodaté, acteur, simulé ou non.

    Sert aussi d'**idempotence** au planificateur : une étape déjà écrite en
    ``result="ok"`` n'est jamais rejouée, y compris après un redémarrage.
    """

    __tablename__ = "campaign_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    ts: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    actor: Mapped[str] = mapped_column(String(20), default="system", server_default="system")
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    result: Mapped[str] = mapped_column(String(10), default="ok", server_default="ok")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class SpecVm(Base):
    """VM jetable du spec mining (§4.6) — créée par Fossor, jamais adoptée.

    Une VM n'est touchée que si son nom porte le préfixe ``fossor-`` **et** si
    libvirt porte la métadonnée ``fossor_vm=1`` (voir :mod:`app.spec_vm`). Le
    wallet vit dans la VM : seule son **adresse** et le **chemin** de sa
    sauvegarde sont enregistrés ici — jamais une seed, jamais un mot de passe.
    """

    __tablename__ = "spec_vms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("campaigns.id"), nullable=True, index=True)
    host_machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(10), default="vm", server_default="vm")
    # Pilote qui a créé la VM : libvirt ou Proxmox (le VMID n'existe qu'en PVE).
    hypervisor: Mapped[str] = mapped_column(String(12), default="libvirt", server_default="libvirt")
    vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_image: Mapped[str] = mapped_column(String(200), default="", server_default="")
    overlay_path: Mapped[str] = mapped_column(String(300), default="", server_default="")
    seed_path: Mapped[str] = mapped_column(String(300), default="", server_default="")
    vcpus: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    memory_mb: Mapped[int] = mapped_column(Integer, default=2048, server_default="2048")
    disk_gb: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    network: Mapped[str] = mapped_column(String(40), default="default", server_default="default")
    node_port: Mapped[int] = mapped_column(Integer, default=19333, server_default="19333")
    ip: Mapped[str] = mapped_column(String(40), default="", server_default="")
    host_key: Mapped[str] = mapped_column(String(300), default="", server_default="")
    state: Mapped[str] = mapped_column(String(16), default="creating", server_default="creating")
    wallet_address: Mapped[str] = mapped_column(String(255), default="", server_default="")
    backup_path: Mapped[str] = mapped_column(String(500), default="", server_default="")
    # Verdict du §10.11 (taille et date seulement, jamais le contenu).
    backup_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_checked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Hashrate du mineur **dans** la VM (API ou journal), rafraîchi par le planificateur.
    hashrate_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    hashrate_source: Mapped[str] = mapped_column(String(60), default="", server_default="")
    hashrate_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str] = mapped_column(String(30), default=lambda: datetime.now(timezone.utc).isoformat())
    destroyed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class PowerController(Base):
    """§4.17 : contrôleur d'alimentation optionnel d'une machine ou d'un ASIC.

    ``token_ref`` porte le jeton Home Assistant **chiffré** (Fernet de la clé
    maître) ; il n'est jamais renvoyé par l'API ni exporté en clair.
    """

    __tablename__ = "power_controllers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    machine_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("machines.id"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str] = mapped_column(String(300), default="", server_default="")
    token_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity: Mapped[str] = mapped_column(String(120), default="", server_default="")
    power_entity: Mapped[str] = mapped_column(String(120), default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    max_per_day: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    min_delay_s: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    created_at: Mapped[str] = mapped_column(
        String(30), default=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: Mapped[str] = mapped_column(
        String(30), default=lambda: datetime.now(timezone.utc).isoformat()
    )


class PowerAction(Base):
    """Journal d'une action d'alimentation (on / off / cycle) et base du quota."""

    __tablename__ = "power_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    result: Mapped[str] = mapped_column(String(12), default="ok", server_default="ok")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    actor: Mapped[str] = mapped_column(String(50), default="system", server_default="system")
    at: Mapped[str] = mapped_column(
        String(40), default=lambda: datetime.now(timezone.utc).isoformat(), index=True
    )


class PowerSample(Base):
    """Puissance mesurée à la prise (Shelly / Home Assistant)."""

    __tablename__ = "power_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ts: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    power_w: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(60), default="", server_default="")
    controller_kind: Mapped[str] = mapped_column(String(20), default="", server_default="")


class EnergyRuleEvent(Base):
    """Journal de la règle « heures chères » (§4.15)."""

    __tablename__ = "energy_rule_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    machine_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class Rule(Base):
    """Règle d'analyse des tokens (§4.8).

    ``id`` est un identifiant stable (slug) : c'est lui que l'API Hermes lit et
    que ``PATCH /api/rules/{id}`` modifie.  ``params`` est du JSON **validé par
    le schéma pydantic** de :mod:`app.rules` avant toute écriture — jamais du
    texte libre.
    """

    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    scope: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[str] = mapped_column(
        String(30), default=lambda: datetime.now(timezone.utc).isoformat()
    )


class RuleChange(Base):
    """Historique d'une règle (§4.8) : qui, quand, avant/après."""

    __tablename__ = "rule_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ts: Mapped[str] = mapped_column(
        String(30), default=lambda: datetime.now(timezone.utc).isoformat(), index=True
    )
    actor: Mapped[str] = mapped_column(String(20), default="fred", server_default="fred")
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)


class TokenAlias(Base):
    """Alias manuel entre deux identifiants de token (§4.9).

    ``mode=merge`` relie deux identifiants (fusion), ``mode=split`` bloque le
    lien direct (séparation) que l'heuristique aurait fait.  Toute écriture est
    auditée : c'est une décision de Fred, pas une devinette.
    """

    __tablename__ = "token_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    member: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="merge", server_default="merge")
    actor: Mapped[str] = mapped_column(String(40), default="fred", server_default="fred")
    created_at: Mapped[str] = mapped_column(
        String(30), default=lambda: datetime.now(timezone.utc).isoformat()
    )


class ProfileInstallation(Base):
    """Binaire d'un profil déjà installé sur une machine (adoption, §4.5)."""

    __tablename__ = "profile_installations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("miner_profiles.id"), nullable=False, index=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(300), default="", server_default="")
    source: Mapped[str] = mapped_column(String(20), default="adopted", server_default="adopted")
    created_at: Mapped[str] = mapped_column(
        String(30), default=lambda: datetime.now(timezone.utc).isoformat()
    )


class Analysis(Base):
    """Analyse de veille écrite par Hermes ou importée (§4.9).

    ``fingerprint`` (SHA-256 du contenu normalisé) porte l'**idempotence** de
    l'import : rejouer le même fichier ne crée aucun doublon.
    """

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    run: Mapped[str] = mapped_column(String(80), default="", server_default="")
    action: Mapped[str] = mapped_column(String(20), default="rien", server_default="rien")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    source: Mapped[str] = mapped_column(String(12), default="hermes", server_default="hermes")
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[str] = mapped_column(
        String(30), default=lambda: datetime.now(timezone.utc).isoformat()
    )


class VaultMeta(Base):
    """Paramètres du coffre des wallets — ni phrase ni secret en clair.

    Le sel Argon2id, le vérificateur AES-GCM et la clé privée X25519 scellée
    par la phrase maîtresse vivent ici.  La phrase elle-même n'est **jamais**
    stockée, et la clé dérivée ne quitte pas la mémoire du processus.
    """

    __tablename__ = "vault_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kdf_algorithm: Mapped[str] = mapped_column(String(20), default="argon2id")
    kdf_time_cost: Mapped[int] = mapped_column(Integer, default=3)
    kdf_memory_kib: Mapped[int] = mapped_column(Integer, default=65536)
    kdf_parallelism: Mapped[int] = mapped_column(Integer, default=4)
    kdf_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    verifier_nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    verifier_ct: Mapped[str] = mapped_column(String(128), nullable=False)
    wrapped_nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    wrapped_ct: Mapped[str] = mapped_column(String(128), nullable=False)
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    lock_minutes: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    blocked_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default="")
    updated_at: Mapped[str] = mapped_column(String(30), default="")


class VaultWallet(Base):
    """Wallet du coffre : métadonnées lisibles, secrets en sealed boxes.

    ``sealed_*`` contient une sealed box X25519 (base64).  Aucune colonne ne
    porte de seed, de clé privée ni de mot de passe en clair : même une copie
    complète de la base ne permet pas de les lire sans la phrase maîtresse.
    """

    __tablename__ = "vault_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    coin: Mapped[str] = mapped_column(String(20), default="")
    network: Mapped[str] = mapped_column(String(40), default="", server_default="")
    address: Mapped[str] = mapped_column(String(160), default="", server_default="")
    machine: Mapped[str] = mapped_column(String(80), default="", server_default="")
    wallet_path: Mapped[str] = mapped_column(String(255), default="", server_default="")
    note: Mapped[str] = mapped_column(String(500), default="", server_default="")
    origin: Mapped[str] = mapped_column(String(10), default="fred", server_default="fred")
    address_status: Mapped[str] = mapped_column(String(12), default="unverified", server_default="unverified")
    address_note: Mapped[str] = mapped_column(String(200), default="", server_default="")
    sealed_seed: Mapped[str | None] = mapped_column(Text, nullable=True)
    sealed_private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sealed_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(30), default="")
    updated_at: Mapped[str] = mapped_column(String(30), default="")


class WatchdogEvent(Base):
    """Journal d'un déclenchement du watchdog (§10.1) — jamais réécrit."""

    __tablename__ = "watchdog_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    machine_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    machine_name: Mapped[str] = mapped_column(String(100), default="", server_default="")
    device_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    device_label: Mapped[str] = mapped_column(String(160), default="", server_default="")
    rule: Mapped[str] = mapped_column(String(30), nullable=False)
    measure: Mapped[str] = mapped_column(String(300), default="", server_default="")
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[str] = mapped_column(String(40), default="", server_default="")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")


class WatchdogState(Base):
    """Mémoire d'une règle active : début, palier, dernière action.

    ``device_id = 0`` désigne une règle de machine (shares, injoignable) ;
    la contrainte d'unicité est portée par le code, pas par SQLite (NULL).
    """

    __tablename__ = "watchdog_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    device_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rule: Mapped[str] = mapped_column(String(30), nullable=False)
    since: Mapped[str] = mapped_column(String(40), nullable=False)
    stage: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_action_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reference_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counter_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

class Boost(Base):
    """Mode boost (§10.3) : mobilisation temporaire du pool sur un coin.

    Le statut suit la machine à états de :mod:`app.boost` ; ``ends_at`` borne
    la durée, ``stop_requested`` porte l'annulation en un clic.  Les colonnes
    ``restore_*`` et ``attempts`` sont la mémoire du planificateur : après un
    redémarrage, l'étape en cours reprend au lieu d'être rejouée.
    """

    __tablename__ = "boosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coin: Mapped[str] = mapped_column(String(20), nullable=False)
    profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed", server_default="proposed", index=True)
    origin: Mapped[str] = mapped_column(String(10), default="manual", server_default="manual")
    scope: Mapped[str] = mapped_column(String(12), default="pool", server_default="pool")
    proportion: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    machine_ids: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    duration_h: Mapped[float] = mapped_column(Float, default=6.0, server_default="6")
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    confirm_overload: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ends_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    restore_deadline: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default="", server_default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="", server_default="")


class BoostDevice(Base):
    """Device mobilisé par un boost, et l'état de son retour au token par défaut."""

    __tablename__ = "boost_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    boost_id: Mapped[int] = mapped_column(Integer, ForeignKey("boosts.id"), nullable=False, index=True)
    machine_id: Mapped[int] = mapped_column(Integer, nullable=False)
    device_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), default="gpu", server_default="gpu")
    label: Mapped[str] = mapped_column(String(120), default="", server_default="")
    hiveos: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    restore_deadline: Mapped[str | None] = mapped_column(String(40), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    restored_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

class BoostEvent(Base):
    """Journal du boost — il **est** l'idempotence des étapes (comme campaign_events)."""

    __tablename__ = "boost_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    boost_id: Mapped[int] = mapped_column(Integer, ForeignKey("boosts.id"), nullable=False, index=True)
    ts: Mapped[str] = mapped_column(String(40), default="", server_default="")
    kind: Mapped[str] = mapped_column(String(40), default="", server_default="")
    message: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    result: Mapped[str] = mapped_column(String(10), default="ok", server_default="ok")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    detail: Mapped[str] = mapped_column(String(2000), default="", server_default="")
