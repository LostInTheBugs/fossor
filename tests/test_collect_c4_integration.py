"""Intégration 2026.11.003-c4 — repli SSH sur un faux mineur **réel**.

Cible jetable : conteneur ``fossor-sshd:test`` sur le réseau ``fossor-t-c4``.
Le faux SRBMiner (binaire renommé ``srbminer_compute``) n'écoute que sur
127.0.0.1:21550 : la requête directe depuis le conteneur Fossor échoue, seul le
relais SSH lit la charge.  Aucune connexion à un rig, à la prod ou à une vraie
machine.

Lancement (voir RAPPORT-003-c4.md) :

    docker network create fossor-t-c4
    docker run -d --rm --name fossor-ssh-c4 --network fossor-t-c4 \
      --network-alias fossor-ssh-c4 fossor-sshd:test
    docker run --rm --network fossor-t-c4 \
      -e FOSSOR_TEST_SSH_HOST=fossor-ssh-c4 -e FOSSOR_TEST_SSH_PORT=2222 \
      -e FOSSOR_TEST_SSH_USER=tester -e FOSSOR_TEST_SSH_PASSWORD=testerpass \
      -v $PWD:/src:ro -w /src fossor-app:test \
      python -m pytest tests/test_collect_c4_integration.py -m integration -q
"""

from __future__ import annotations

import json
import os

import asyncssh
import pytest

pytestmark = pytest.mark.integration


def _target() -> dict[str, str]:
    host = os.environ.get("FOSSOR_TEST_SSH_HOST", "")
    if not host:
        pytest.skip("cible SSH jetable absente (FOSSOR_TEST_SSH_HOST)")
    return {
        "host": host,
        "port": os.environ.get("FOSSOR_TEST_SSH_PORT", "2222"),
        "user": os.environ.get("FOSSOR_TEST_SSH_USER", "tester"),
        "password": os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass"),
    }


def seed_machine(target: dict[str, str], name: str) -> int:
    from app.database import SessionLocal
    from app.models import Coin, Machine
    from app.secrets import encrypt_secret

    db = SessionLocal()
    try:
        machine = Machine(
            name=name, host=target["host"], ssh_port=int(target["port"]),
            ssh_user=target["user"], auth_type="password", usage="both",
            enabled=True,
            secret_ref=encrypt_secret(target["password"]),
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        # Catalogue réaliste : pool_hosts contient l'URL complète avec le port.
        if db.query(Coin).filter(Coin.ticker == "PRL").first() is None:
            db.add(Coin(
                ticker="PRL", name="Pearl",
                pool_hosts=["stratum+tcp://pearl-eu2.luckypool.io:3360"],
            ))
        db.commit()
        return machine.id
    finally:
        db.close()


async def _ssh(target: dict[str, str], command: str) -> str:
    async with asyncssh.connect(
        target["host"], port=int(target["port"]), username=target["user"],
        password=target["password"], known_hosts=None,
    ) as conn:
        result = await conn.run(command, check=False)
        return str(result.stdout or "")


async def test_collection_reads_hashrate_through_ssh_relay():
    """ps réel + relais SSH réel + faux SRBMiner sur 127.0.0.1:21550."""
    target = _target()
    machine_id = seed_machine(target, "athlon-3000g-c4")
    from app import collector

    # Le test d'échec gèle le mineur : on le réveille pour rester idempotent.
    await _ssh(target, "pkill -CONT -f srbminer_compute")

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True, result
    assert result["api_port"] == 21550, result

    from app.database import SessionLocal
    from app.models import Machine, MachineStatus

    db = SessionLocal()
    try:
        status = db.query(MachineStatus).one()
        assert status.api_error is None
        assert status.total_hashrate_hs == pytest.approx(1.25e9)
        assert status.coin == "PRL"  # pool stocké en URL complète, normalisé
        machine = db.query(Machine).filter(Machine.id == machine_id).one()
        miners = json.loads(machine.external_miners)
        srb = [m for m in miners if m["name"] == "SRBMiner"][0]
        assert srb["api_port"] == 21550
        assert srb["coin"] == "PRL"
        assert srb["user"] == target["user"]  # utilisateur complet, jamais tronqué
        assert srb["cpu_pct"] is not None
        assert srb["count"] == 1
        assert srb["hashrate_hs"] == pytest.approx(1.25e9)
    finally:
        db.close()


async def test_hashrate_missing_with_open_port_is_reported():
    """Processus toujours visible, API gelée : api_error doit le dire."""
    target = _target()
    machine_id = seed_machine(target, "athlon-3000g-c4-down")
    from app import collector

    await _ssh(target, "pkill -STOP -f srbminer_compute")

    await collector.collect_machine(machine_id)

    from app.database import SessionLocal
    from app.models import MachineStatus

    db = SessionLocal()
    try:
        status = db.query(MachineStatus).one()
        assert status.api_port == 21550  # le port est bien vu dans ps
        assert status.total_hashrate_hs is None
        assert status.api_error, "l'absence de hashrate doit être signalée"
        assert "sans hashrate" in status.api_error or "injoignable" in status.api_error
    finally:
        db.close()
