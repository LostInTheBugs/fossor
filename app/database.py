import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

_db_url: str | None = None


def _get_db_url() -> str:
    global _db_url
    if _db_url is None:
        _db_url = os.environ.get("DATABASE_URL", "sqlite:////data/fossor.db")
    return _db_url


def _make_engine():
    return create_engine(
        _get_db_url(),
        connect_args={"check_same_thread": False},
    )


def _make_session():
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Create engine/session lazily via a proxy approach
class _LazyEngine:
    """Proxy that creates the real engine on first use."""
    def __init__(self):
        self._engine = None
        self._session_factory = None

    @property
    def engine(self):
        if self._engine is None:
            self._engine = _make_engine()
        return self._engine

    @property
    def session_factory(self):
        if self._session_factory is None:
            self._session_factory = _make_session()
        return self._session_factory


_lazy = _LazyEngine()
engine = _lazy.engine
SessionLocal = _lazy.session_factory


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _ensure_admin()
    _migrate_host_key_public_column()
    _migrate_machine_uuid_column()
    _migrate_device_integrated_column()
    _migrate_wallet_pool_host_column()
    _migrate_machine_power_columns()
    _migrate_machine_default_miner_column()
    _migrate_device_occupied_external_column()
    _migrate_machine_external_miners_column()
    _migrate_coin_pool_hosts_column()
    _migrate_machine_status_api_error_column()
    _migrate_machine_status_cpu_json_column()
    _migrate_device_psu_columns()
    _migrate_device_psu_split_meaning()
    _migrate_machine_os_detected_column()
    _migrate_device_role_columns()


def _migrate_machine_os_detected_column():
    """§4.1 ter : ``os_kind`` (choisi) vs ``os_kind_detected`` (OS réel).

    Les machines créées avant le champ n'ont pas de type choisi distinct :
    leur ``os_kind`` a toujours été écrit par la détection, il devient donc la
    valeur détectée — aucune bannière rétroactive, et le type choisi reste
    modifiable par « Appliquer le préréglage ».
    """
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(machines)")}
        if "os_kind_detected" not in columns:
            conn.exec_driver_sql("ALTER TABLE machines ADD COLUMN os_kind_detected VARCHAR(20)")
        conn.exec_driver_sql(
            "UPDATE machines SET os_kind_detected = os_kind WHERE os_kind_detected IS NULL"
        )


def _migrate_coin_pool_hosts_column():
    """Add coins.pool_hosts (§4.6, 003-c2) and seed the known coins.

    A miner's coin is inferred from its pool host; the catalogue owns the
    mapping so it can be edited without a new release.
    """
    import json
    import sqlite3

    _add_column_if_missing(
        "coins", "pool_hosts", "pool_hosts JSON NOT NULL DEFAULT '[]'"
    )
    db_path = _sqlite_path()
    if not db_path:
        return
    seeds = {
        "PRL": ["pearl.luckypool.io", "pearl"],
        "KSH": ["korsh.luckypool.io", "korsh"],
        "WAM": ["wamcoin.luckypool.io", "wamcoin", "wam"],
    }
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT ticker, pool_hosts FROM coins").fetchall()
        for ticker, stored in rows:
            hosts = seeds.get(str(ticker).upper())
            if not hosts:
                continue
            try:
                current = json.loads(stored or "[]")
            except (TypeError, ValueError):
                current = []
            if not current:
                conn.execute(
                    "UPDATE coins SET pool_hosts = ? WHERE ticker = ?",
                    (json.dumps(hosts), ticker),
                )
        conn.commit()
    finally:
        conn.close()


def _migrate_machine_status_api_error_column():
    """Add machine_status.api_error — miner API unreachable (§4.6, 003-c2)."""
    _add_column_if_missing("machine_status", "api_error", "api_error TEXT")


def _migrate_machine_status_cpu_json_column():
    """Add machine_status.cpu_json — état du CPU et hashrate CPU (003-c7)."""
    _add_column_if_missing("machine_status", "cpu_json", "cpu_json TEXT")


def _migrate_machine_default_miner_column():
    """Add machines.default_miner_profile_id (§4.5, étape 3a)."""
    _add_column_if_missing(
        "machines", "default_miner_profile_id", "default_miner_profile_id INTEGER"
    )


def _migrate_device_integrated_column():
    """Add devices.integrated (iGPU / BMC GPU, never mined by default)."""
    import sqlite3

    db_path = _sqlite_path()
    if not db_path:
        return
    conn = sqlite3.connect(db_path)
    try:
        col_names = [c[1] for c in conn.execute("PRAGMA table_info(devices)").fetchall()]
        if col_names and "integrated" not in col_names:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN integrated BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    """SQLite ``ALTER TABLE`` guarded by ``PRAGMA table_info``."""
    import sqlite3

    db_path = _sqlite_path()
    if not db_path:
        return
    conn = sqlite3.connect(db_path)
    try:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            conn.commit()
    finally:
        conn.close()


def _migrate_wallet_pool_host_column():
    """Add wallets.pool_host (Luckypool per-coin subdomain, §4.16)."""
    _add_column_if_missing("wallets", "pool_host", "pool_host VARCHAR(80) NOT NULL DEFAULT ''")


def _migrate_machine_power_columns():
    """Add machines.cpu_power_w / platform_power_w (§4.15)."""
    _add_column_if_missing("machines", "cpu_power_w", "cpu_power_w FLOAT")
    _add_column_if_missing(
        "machines", "platform_power_w", "platform_power_w FLOAT NOT NULL DEFAULT 50"
    )


def _migrate_device_role_columns():
    """§4.19 : rôle de chaque device, pool de boost, token par défaut du device.

    Les devices existants passent à ``default_and_spec`` : le comportement des
    campagnes ne change pas pour eux, seul le réglage devient explicite.
    """
    _add_column_if_missing(
        "devices", "role", "role VARCHAR(20) NOT NULL DEFAULT 'default_and_spec'"
    )
    _add_column_if_missing(
        "devices", "boost_pool", "boost_pool BOOLEAN NOT NULL DEFAULT 0"
    )
    _add_column_if_missing("devices", "default_token_id", "default_token_id INTEGER")


def _migrate_device_occupied_external_column():
    """Add devices.occupied_external — GPU occupied by external miner (§4.24)."""
    _add_column_if_missing(
        "devices",
        "occupied_external",
        "occupied_external BOOLEAN NOT NULL DEFAULT 0",
    )


def _migrate_machine_external_miners_column():
    """Add machines.external_miners — last external miners seen (§2.6)."""
    _add_column_if_missing(
        "machines",
        "external_miners",
        "external_miners TEXT",
    )


def _sqlite_path() -> str | None:
    db_url = _get_db_url()
    if not db_url.startswith("sqlite"):
        return None
    path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    return path if os.path.exists(path) else None


def _migrate_machine_uuid_column():
    """Add machines.uuid and backfill UUID4 on existing rows (cluster prep, §4.11)."""
    import sqlite3
    import uuid as uuid_lib

    db_path = _sqlite_path()
    if not db_path:
        return
    conn = sqlite3.connect(db_path)
    try:
        col_names = [c[1] for c in conn.execute("PRAGMA table_info(machines)").fetchall()]
        if "uuid" not in col_names:
            conn.execute("ALTER TABLE machines ADD COLUMN uuid VARCHAR(36)")
        rows = conn.execute(
            "SELECT id FROM machines WHERE uuid IS NULL OR uuid = ''"
        ).fetchall()
        for (machine_id,) in rows:
            conn.execute(
                "UPDATE machines SET uuid = ? WHERE id = ?",
                (str(uuid_lib.uuid4()), machine_id),
            )
        conn.commit()
    finally:
        conn.close()


def _migrate_host_key_public_column():
    """Add host_key_public column if it doesn't exist (SQLite migration)."""
    import os
    db_url = _get_db_url()
    if not db_url.startswith("sqlite"):
        return
    from sqlalchemy import text
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    if not os.path.exists(db_path):
        return
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        cols = conn.execute(
            "PRAGMA table_info(machines)"
        ).fetchall()
        col_names = [c[1] for c in cols]
        if "host_key_public" not in col_names:
            conn.execute(
                "ALTER TABLE machines ADD COLUMN host_key_public TEXT"
            )
            conn.commit()
    finally:
        conn.close()


def reset_engine():
    """Reset the engine to pick up a new DATABASE_URL (for tests)."""
    global _db_url
    _db_url = None
    _lazy._engine = None
    _lazy._session_factory = None


def _ensure_admin():
    """Create the admin user if none exists and FOSSOR_ADMIN_PASSWORD is set."""
    from app.models import User
    from app.auth import hash_password

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if admin is None:
            pw = os.environ.get("FOSSOR_ADMIN_PASSWORD")
            if pw:
                admin = User(
                    username="admin",
                    password_hash=hash_password(pw),
                    is_admin=True,
                )
                db.add(admin)
                db.commit()
    finally:
        db.close()


def _migrate_device_psu_columns():
    """Add devices.psu_id/psu2_id/split_w (§4.2 bis)."""
    import os
    import sqlite3

    db_url = _get_db_url()
    if not db_url.startswith("sqlite"):
        return
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(devices)").fetchall()]
        for name, ddl in (("psu_id", "INTEGER"), ("psu2_id", "INTEGER"), ("split_w", "FLOAT")):
            if name not in cols:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {ddl}")
        conn.commit()
    finally:
        conn.close()


def _migrate_device_psu_split_meaning():
    """2026.11.004-c5 : une seconde PSU seule devient la PSU de la carte.

    Le formulaire permettait de choisir une PSU dans le second menu sans rien
    dans le premier : la base gardait ``psu_id`` NULL et ``psu2_id`` renseigné,
    puis toute la charge partait sur la PSU par défaut avec l'étiquette
    « slot/riser ».  La répartition slot/riser exige les deux : on remonte la
    seconde PSU en première et on efface la part.
    """
    import os
    import sqlite3

    db_url = _get_db_url()
    if not db_url.startswith("sqlite"):
        return
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)").fetchall()}
        if not {"psu_id", "psu2_id", "split_w"} <= cols:
            return
        conn.execute(
            "UPDATE devices SET psu_id = psu2_id, psu2_id = NULL, split_w = NULL "
            "WHERE psu_id IS NULL AND psu2_id IS NOT NULL"
        )
        conn.commit()
    finally:
        conn.close()
