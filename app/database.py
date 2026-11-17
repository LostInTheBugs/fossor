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
    _migrate_device_occupied_external_column()
    _migrate_machine_external_miners_column()


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
