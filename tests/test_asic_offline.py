"""Un ASIC muet ne doit jamais figer le serveur (2026.11.008).

Deux formes de panne, mesurées sur l'API réelle tant que la collecte sonde
l'hôte :

* **port fermé** — connexion refusée, tout de suite ;
* **port qui accepte la connexion TCP puis se tait** (ni TLS, ni HTTP) — la
  sonde attend son délai de 6 s, qui doit rester hors du chemin des requêtes.

L'API (``/api/version``, ``/dashboard``) doit répondre en moins de 2 s pendant
ce temps, et l'ASIC finir marqué hors ligne.  Aucune machine réelle n'est
contactée : tout est sur ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest


@pytest.fixture()
def silent_port():
    """Port TCP qui accepte les connexions et ne répond jamais."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    stop = threading.Event()
    held: list[socket.socket] = []

    def loop() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except OSError:
                continue
            held.append(conn)  # gardé ouvert, jamais de réponse

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in held:
            conn.close()
        server.close()


def _seed_asic(name: str, host: str, driver: str = "axeos") -> str:
    """Déclare un ASIC ; rend son uuid (clé de ``machine_status``)."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        machine = Machine(
            name=name,
            host=host,
            machine_kind="asic",
            asic_driver=driver,
            usage="none",  # aucun SSH : seule la sonde ASIC doit tourner
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        return machine.uuid
    finally:
        db.close()


async def _timed_get(client, path: str):
    started = time.perf_counter()
    response = await client.get(path)
    return response, time.perf_counter() - started


async def test_dead_asic_never_blocks_the_api(client, silent_port):
    from app import collector
    from app.database import SessionLocal
    from app.models import MachineStatus

    refused = _seed_asic("e2e-asic-refused", "127.0.0.1:9")
    silent = _seed_asic("e2e-asic-silent", f"127.0.0.1:{silent_port}", "iceriver")

    task = asyncio.create_task(collector.collect_all())
    await asyncio.sleep(0.2)  # la sonde est en vol (6 s pour le port muet)

    version, dt_version = await _timed_get(client, "/api/version")
    dashboard, dt_dashboard = await _timed_get(client, "/dashboard")
    assert version.status_code == 200, version.text
    assert dashboard.status_code == 200, dashboard.status_code
    assert dt_version < 2, f"/api/version a mis {dt_version:.2f}s"
    assert dt_dashboard < 2, f"/dashboard a mis {dt_dashboard:.2f}s"
    assert not task.done(), "la sonde devait encore être en vol"

    results = await asyncio.wait_for(task, timeout=30)
    assert {r["machine_id"] for r in results if r.get("online")} == set()

    db = SessionLocal()
    try:
        rows = {
            row.machine_uuid: row
            for row in db.query(MachineStatus).filter(
                MachineStatus.machine_uuid.in_([refused, silent])
            )
        }
    finally:
        db.close()
    for uuid in (refused, silent):
        row = rows.get(uuid)
        assert row is not None, f"aucun machine_status pour {uuid}"
        assert row.online is False
        assert row.error, "l'ASIC hors ligne doit porter sa raison"


@pytest.mark.parametrize(
    "failure",
    [
        "RemoteProtocolError",
        "ReadError",
        "WriteTimeout",
        "PoolTimeout",
    ],
)
async def test_session_turns_every_httpx_error_into_asic_error(failure):
    """``_Session.request`` ne laisse sortir aucune erreur httpx brute."""
    httpx = pytest.importorskip("httpx")
    from app import asic_client

    exc_type = getattr(httpx, failure)

    def handler(request):  # noqa: ANN001 — signature imposée par MockTransport
        raise exc_type(f"{failure} simulée")

    session = asic_client._Session()
    session._plain_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(asic_client.AsicError) as raised:
            await session.request("GET", "http://127.0.0.1:1/api/system/info")
    finally:
        await session._plain_client.aclose()
    assert failure in str(raised.value), str(raised.value)
