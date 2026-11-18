"""Fabrique commune des données e2e : machines, dernier état, devices.

Toutes les insertions SQL brutes des tests e2e passent par ici : ``machine_status``
exige ``algo``, ``miner`` et ``coin`` (colonnes NOT NULL), et ``devices`` exige
``created_at``/``updated_at``. Une insertion partielle échoue avant la première
assertion d'écran — c'est ce qui a rendu test_z_ux1 rouge en UX-1.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime, timezone

from tests.e2e.conftest import E2E_DATA_DIR

DB_PATH = f"{E2E_DATA_DIR}/fossor.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_status(machine_uuid: str, conn=None, **values) -> None:
    """Écrit le dernier état d'une machine, comme le collecteur le ferait.

    *conn* : connexion déjà ouverte par l'appelant (même transaction) ; sinon
    une connexion est ouverte puis refermée. Deux connexions concurrentes en
    écriture sur la base e2e se répondent « database is locked ».
    """
    own = conn is None
    conn = conn or sqlite3.connect(DB_PATH, timeout=15)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(machine_status)")}
        row = {"algo": "", "miner": "", "coin": ""}
        row.update(values)
        row = {key: value for key, value in row.items() if key in columns}
        row["machine_uuid"] = machine_uuid
        row.setdefault("ts", _now())
        names = ", ".join(f'"{name}"' for name in row)
        marks = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT OR REPLACE INTO machine_status ({names}) VALUES ({marks})",
            list(row.values()),
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def insert_device(machine_id: int, *, kind: str = "gpu", index: int = 0,
                  vendor: str = "NVIDIA", model: str = "NVIDIA GeForce RTX 3070",
                  integrated: bool = False, enabled: bool = True,
                  present: bool = True) -> int:
    """Ajoute un device sans SSH, comme une détection l'aurait fait."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        now = _now()
        cursor = conn.execute(
            "INSERT INTO devices (uuid, machine_id, kind, \"index\", vendor, model, "
            "present, enabled_for_mining, integrated, occupied_external, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), machine_id, kind, index, vendor, model,
             1 if present else 0, 1 if enabled else 0, 1 if integrated else 0, 0, now, now),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


async def create_machine(page, name: str, *, host: str = "192.0.2.77",
                         usage: str = "both") -> dict:
    """Crée une machine par l'API depuis le navigateur et renvoie sa fiche."""
    unique = f"{name}-{int(time.time() * 1000)}"
    return await page.evaluate(
        """async ({name, host, usage}) => {
            const res = await fetch('/api/machines', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name, host: host, ssh_port: 22,
                    ssh_user: 'tester', auth_type: 'password',
                    usage: usage, enabled: true})
            });
            if (!res.ok) throw new Error('machine create failed ' + res.status);
            return await res.json();
        }""",
        {"name": unique, "host": host, "usage": usage},
    )
