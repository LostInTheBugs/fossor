"""Mise à niveau d'une base 2026.11.022 vers le code courant.

Le démarrage de la 2026.11.023 sur la base de production échouait sur
``no such column: devices.occupied_by`` : ``_migrate_boost_pool_defaults``
interroge le modèle complet avant que ``_migrate_device_occupancy_columns``
n'ajoute les colonnes.  Les tests ne le voyaient pas : ``create_all`` part
toujours d'une base déjà à jour.

Ce fichier bâtit donc une base **2026.11.022** réelle — schéma par migrations,
puis les deux colonnes apparues en 023 retirées — la remplit de données
fictives, et la fait démarrer avec le code courant.  Aucune donnée de
production, aucune adresse réelle (RFC 5737).
"""
import sqlite3

import pytest

import app.database as database

NEW_COLUMNS = ("occupied_by", "occupied_campaign_id")

TS = "2026-11-01T00:00:00+00:00"


def _point_at(path, monkeypatch):
    """Rebranche le module sur une autre base.

    ``app.database.engine`` et ``SessionLocal`` sont les objets globaux que
    ``init_db`` utilise vraiment : ``_make_engine`` les recrée ici, sinon la
    mise à niveau travaillerait sur la base du démarrage des tests.
    """
    if database.engine is not None:
        database.engine.dispose()  # sinon la base reste verrouillée (WAL)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    database._db_url = None
    database._lazy._engine = None
    database._lazy._session_factory = None
    database.engine = database._make_engine()
    database.SessionLocal = database._make_session()


def _build_022(tmp_path, monkeypatch, journal_mode="wal"):
    """Base au schéma 022 : créée par les migrations, puis amputée de la 023."""
    path = tmp_path / "fossor-022.db"
    if journal_mode == "wal":
        # Schéma bâti par les migrations réelles, comme une base de production.
        _point_at(path, monkeypatch)
        database.init_db()
        database.engine.dispose()
    else:
        # Fichier neuf laissé dans son mode par défaut (DELETE).
        from sqlalchemy import create_engine

        from app.models import Base

        plain = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(bind=plain)
        plain.dispose()
    conn = sqlite3.connect(path)
    try:
        for column in NEW_COLUMNS:
            conn.execute(f"ALTER TABLE devices DROP COLUMN {column}")
        conn.commit()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == journal_mode
    finally:
        conn.close()
    return path


@pytest.fixture(autouse=True)
def _restore_engine():
    """Rend au module son moteur d'origine : sans cela, les tests suivants
    travailleraient sur la base de ce fichier (fuite de la 023 en 022)."""
    engine_before = database.engine
    session_before = database.SessionLocal
    url_before = database._db_url
    yield
    database.engine.dispose()
    database.engine = engine_before
    database.SessionLocal = session_before
    database._db_url = url_before
    database._lazy._engine = engine_before
    database._lazy._session_factory = session_before


def _fill(path):
    """Données fictives : deux machines, GPU HiveOS hors pool, profil, coffre,
    campagne importée, règles semées par ``init_db``."""
    conn = sqlite3.connect(path)
    try:
        machine_cols = (
            "name, host, ssh_port, ssh_user, auth_type, sudo, os_kind, machine_kind,"
            " usage, vm_host, fossor_host, enabled, simulation_disabled,"
            " watchdog_reboot_ok, watchdog_power_ok, platform_power_w, created_at, updated_at"
        )
        values = (
            "('rig-hiveos', '198.51.100.11', 22, 'root', 'key', 'none', 'hiveos', 'rig',"
            " 'mining', 0, 0, 1, 0, 1, 1, 0, '%s', '%s')" % (TS, TS),
            "('athlon-lab', '198.51.100.12', 22, 'fossor', 'key', 'none', 'linux', 'desktop',"
            " 'mining', 0, 0, 1, 0, 1, 0, 0, '%s', '%s')" % (TS, TS),
        )
        conn.execute(f"INSERT INTO machines ({machine_cols}) VALUES {values[0]}, {values[1]}")

        device_cols = (
            "uuid, machine_id, kind, present, enabled_for_mining, integrated,"
            " occupied_external, role, boost_pool, created_at, updated_at"
        )
        devices = (
            "('gpu-hive', 1, 'gpu', 1, 1, 0, 0, 'mining', 0, '%s', '%s')" % (TS, TS),
            "('gpu-athlon', 2, 'gpu', 1, 1, 1, 0, 'mining', 0, '%s', '%s')" % (TS, TS),
            "('cpu-athlon', 2, 'cpu', 1, 1, 1, 0, 'mining', 0, '%s', '%s')" % (TS, TS),
        )
        conn.execute(f"INSERT INTO devices ({device_cols}) VALUES {', '.join(devices)}")

        conn.execute(
            "INSERT INTO miner_profiles (uuid, name, coin, wallet, pool, software,"
            " binary_url, sha256, binary_path, version, cmdline, api_port, api_kind,"
            " binary_source, binary_stored, binary_size, binary_kind, wallet_mode,"
            " execution_mode, threads, worker, cpu_governor, tty_wrapper, enabled,"
            " created_at, updated_at)"
            " VALUES ('p-022', 'profil-022', 'XMR', 'fictif', 'pool.example:3333', 'xmrig',"
            " 'https://example.invalid/xmrig.tar.gz', '%s', '/data/bin/xmrig', '6.21.0',"
            " 'xmrig --donate-level 1', 3333, 'xmrig', 'url', '', 0, 'tar', 'profile',"
            " 'per_device', 4, 'worker', 0, 0, 1, '%s', '%s')" % ("0" * 64, TS, TS)
        )
        conn.execute(
            "INSERT INTO campaigns (uuid, coin, profile_id, start_at, status, origin,"
            " execution_mode, stop_requested, confirm_overload, balance_unit,"
            " balance_source, restore_timeout_s, attempts, created_at, updated_at)"
            " VALUES ('c-022', 'XMR', 1, '%s', 'imported', 'manual', 'per_device', 0, 0,"
            " 'coin', 'pool', 120, 0, '%s', '%s')" % (TS, TS, TS)
        )
        conn.execute(
            "INSERT INTO vault_meta (kdf_algorithm, kdf_time_cost, kdf_memory_kib,"
            " kdf_parallelism, kdf_salt, verifier_nonce, verifier_ct, wrapped_nonce,"
            " wrapped_ct, public_key, lock_minutes, failed_attempts, created_at, updated_at)"
            " VALUES ('scrypt', 3, 65536, 1, 'salt-fictif', 'nonce', 'ct', 'nonce', 'ct',"
            " 'cle-publique-fictive', 15, 0, '%s', '%s')" % (TS, TS)
        )
        conn.commit()
    finally:
        conn.close()


def _upgrade(path, monkeypatch):
    """Le code courant redémarre sur la base de la version précédente."""
    _point_at(path, monkeypatch)
    database.init_db()


def test_upgrade_from_022_on_filled_database(tmp_path, monkeypatch):
    """La 022 remplie démarre en 023 : colonnes ajoutées, données et règles intactes."""
    path = _build_022(tmp_path, monkeypatch)
    _fill(path)

    _upgrade(path, monkeypatch)

    conn = sqlite3.connect(path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(devices)")]
        assert "occupied_by" in columns
        assert "occupied_campaign_id" in columns
        assert conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM miner_profiles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM vault_meta").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0
        # La migration de données a bien tourné après l'ajout des colonnes :
        # le GPU du rig HiveOS, hors pool en 022, entre dans le pool.
        assert conn.execute(
            "SELECT boost_pool FROM devices WHERE uuid = 'gpu-hive'"
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_upgrade_from_022_in_delete_journal_mode(tmp_path, monkeypatch):
    """Base laissée en journal_mode=DELETE : la mise à niveau passe en WAL."""
    path = _build_022(tmp_path, monkeypatch, journal_mode="delete")
    _fill(path)

    _upgrade(path, monkeypatch)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 3
    finally:
        conn.close()


def test_upgrade_refuses_read_only_database(tmp_path, monkeypatch):
    """Fichier en lecture seule : message explicite, pas un traceback ORM."""
    path = _build_022(tmp_path, monkeypatch)
    _fill(path)
    path.chmod(0o444)

    try:
        with pytest.raises(Exception) as caught:
            _upgrade(path, monkeypatch)
        message = str(caught.value).lower()
        assert "readonly" in message or "read-only" in message or "non migrable" in message
    finally:
        path.chmod(0o600)


# ── Schéma 026 → courant : les tables de l'historique §10.7 ───────────
NEW_TABLES_063 = ("metric_aggs", "plug_power_samples", "price_samples")


def _build_026(tmp_path, monkeypatch):
    """Base au schéma 026 : migrations réelles, puis tables 6.3 retirées."""
    path = tmp_path / "fossor-026.db"
    _point_at(path, monkeypatch)
    database.init_db()
    database.engine.dispose()
    conn = sqlite3.connect(path)
    try:
        for table in NEW_TABLES_063:
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
        remaining = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not (set(NEW_TABLES_063) & remaining)
    finally:
        conn.close()
    return path


def _fill_026(path):
    """Base 026 remplie : une machine et 24 métriques brutes déjà collectées."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO machines (name, host, ssh_port, ssh_user, auth_type, sudo,"
            " os_kind, machine_kind, usage, vm_host, fossor_host, enabled,"
            " simulation_disabled, watchdog_reboot_ok, watchdog_power_ok,"
            " platform_power_w, created_at, updated_at)"
            " VALUES ('rig-026', '198.51.100.26', 22, 'fossor', 'key', 'none',"
            " 'linux', 'rig', 'mining', 0, 0, 1, 0, 1, 1, 0, ?, ?)",
            (TS, TS),
        )
        machine_uuid = "02600000-0000-0000-0000-000000000026"
        for hour in range(24):
            conn.execute(
                "INSERT INTO metrics (machine_uuid, ts, power_w) VALUES (?, ?, ?)",
                (machine_uuid, f"2026-11-01T{hour:02d}:00:00+00:00", 100.0 + hour),
            )
        conn.commit()
    finally:
        conn.close()
    return "02600000-0000-0000-0000-000000000026"


def test_upgrade_from_026_creates_history_tables_on_filled_database(tmp_path, monkeypatch):
    """La 026 remplie démarre : les trois tables 6.3 existent et sont utilisables."""
    path = _build_026(tmp_path, monkeypatch)
    machine_uuid = _fill_026(path)

    _upgrade(path, monkeypatch)

    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(NEW_TABLES_063) <= tables
        # Les données de la 026 sont intactes.
        assert conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 24
        assert conn.execute(
            "SELECT COUNT(*) FROM metrics WHERE machine_uuid = ?", (machine_uuid,)
        ).fetchone()[0] == 24
        # Les nouvelles tables acceptent leurs lignes (pas seulement présentes).
        conn.execute(
            "INSERT INTO metric_aggs (scope, scope_uuid, bucket, ts, samples)"
            " VALUES ('machine', ?, 'hour', '2026-11-01T00:00:00+00:00', 1)",
            (machine_uuid,),
        )
        conn.execute(
            "INSERT INTO plug_power_samples (machine_uuid, ts, power_w, dc_w, source)"
            " VALUES (?, '2026-11-01T00:00:00+00:00', 120.0, 110.0, 'psu')",
            (machine_uuid,),
        )
        conn.execute(
            "INSERT INTO price_samples (ticker, ts, eur, source) VALUES ('XMR', '2026-11-01T00:00:00+00:00', 150.0, 'test')"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM metric_aggs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM plug_power_samples").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM price_samples").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
