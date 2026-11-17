"""Intégration 2026.11.003-c7 — cible jetable : faux cpuminer + faux ``*-cli``.

Le conteneur ``fossor-ssh-c7`` (image ``fossor-cpu-c7:test``, dérivée de
``fossor-sshd:test``) fait tourner un faux cpuminer qui écrit son vrai format
``TTF @ 237.8 Mh/s`` dans ``/var/log/fake-cpuminer.log``, et un faux client de
nœud dont seuls les getters répondent.  Aucune connexion à un rig, à la prod ou
à une vraie machine.

Lancement (voir RAPPORT-003-c7.md) :

    docker network create fossor-t-c7
    docker run -d --rm --name fossor-ssh-c7 --network fossor-t-c7 \
      --network-alias fossor-ssh-c7 fossor-cpu-c7:test
    docker run --rm --network fossor-t-c7 \
      -e FOSSOR_TEST_SSH_HOST=fossor-ssh-c7 -e FOSSOR_TEST_SSH_PORT=2222 \
      -e FOSSOR_TEST_SSH_USER=tester -e FOSSOR_TEST_SSH_PASSWORD=testerpass \
      -v $PWD:/src:ro -w /src fossor-app:test \
      python -m pytest tests/test_collect_c7_integration.py -m integration -q
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
    from app.models import Device, Machine
    from app.secrets import encrypt_secret

    db = SessionLocal()
    try:
        machine = Machine(
            name=name, host=target["host"], ssh_port=int(target["port"]),
            ssh_user=target["user"], auth_type="password", usage="both",
            enabled=True, secret_ref=encrypt_secret(target["password"]),
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(
            machine_id=machine.id, uuid=f"uuid-c7-{name}", kind="cpu", index=None,
            vendor="Fake", model="Faux processeur 2 cœurs", present=True,
            enabled_for_mining=True,
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


async def test_cpu_hashrate_from_log_and_node_getters():
    """ps réel + sonde réelle : log du faux cpuminer et getters du faux nœud."""
    target = _target()
    machine_id = seed_machine(target, "cpu-c7")

    await _ssh(target, "pkill -x fake-cpuminer || true; pkill -x fakecoin-cli || true; sleep 0.3")
    # setsid détache le processus de la session SSH (sinon sshd le tue au
    # retour de la commande).  La redirection est héritée par le processus :
    # le chemin n'apparaît pas dans ps, c'est /proc/<pid>/fd/1 qui le donne.
    log = "/tmp/fake-cpuminer.log"
    await _ssh(
        target,
        f"rm -f {log}; setsid nohup /opt/fake-cpuminer >> {log} 2>&1 < /dev/null & sleep 1; echo ok",
    )
    await _ssh(
        target,
        "setsid nohup /usr/local/bin/fakecoin-cli -datadir=/root/fakecoin/data "
        "generatetoaddress 1 faddr 200000 > /dev/null 2>&1 < /dev/null & sleep 1; echo ok",
    )

    from app import collector

    result = await collector.collect_machine(machine_id)
    assert result["online"] is True, result

    from app.database import SessionLocal
    from app.models import Machine, MachineStatus

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).one()
        miners = json.loads(machine.external_miners or "[]")
        cpu_miners = [m for m in miners if m["kind"] in ("cpu", "solo")]
        assert cpu_miners, miners

        cpuminer = next(m for m in cpu_miners if "cpuminer" in (m["binary"] or ""))
        assert cpuminer["hashrate_hs"] == pytest.approx(237.8e6), cpuminer
        assert cpuminer["hashrate_source"].startswith("log /tmp/fake-cpuminer.log")

        solo = next(m for m in cpu_miners if m["kind"] == "solo")
        assert solo["node"]["height"] == 21458, solo
        assert solo["node"]["network_hs"] == pytest.approx(18.4e9)

        status = db.query(MachineStatus).one()
        cpu = json.loads(status.cpu_json)
        assert cpu["load_pct"] is not None and cpu["load_pct"] > 0
        assert cpu["cores"] and cpu["cores"] >= 1
        # Pas de capteur hwmon dans un conteneur : l'absence est explicite.
        assert cpu["temp_c"] is None or cpu["temp_c"] > 0
        assert cpu["power_source"] in ("rapl", "non mesurée")
        assert cpu["hashrate_hs"] == pytest.approx(237.8e6)
    finally:
        db.close()


async def test_probe_is_read_only_for_the_miner():
    """La sonde ne modifie rien : le PID du faux mineur est inchangé."""
    target = _target()
    machine_id = seed_machine(target, "cpu-c7-ro")
    before = (await _ssh(target, "pgrep -x fake-cpuminer | head -1")).strip()
    from app import collector

    await collector.collect_machine(machine_id)
    after = (await _ssh(target, "pgrep -x fake-cpuminer | head -1")).strip()
    assert before and before == after
