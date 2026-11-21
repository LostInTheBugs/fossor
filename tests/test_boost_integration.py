"""Intégration — un boost complet contre un faux HiveOS sur un hôte SSH jetable.

Le conteneur ``fossor-sshd:test`` porte de faux ``/hive/bin/miner`` et
``/hive/bin/wd`` qui écrivent chaque appel dans ``/tmp/boost-hive.log``.  Le
boost est accéléré : le pilote HiveOS exécute réellement ses commandes par SSH,
le mineur de campagne est un bouchon qui enregistre l'ordre.

Aucune machine réelle n'est contactée.

Lancement :

    docker network create fossor-t-boost
    docker run -d --rm --name fossor-ssh-boost --network fossor-t-boost \\
      --network-alias fossor-ssh-boost fossor-sshd:test
    docker exec -u root fossor-ssh-boost sh -c \\
      'printf "tester ALL=(ALL) NOPASSWD:ALL\\n" > /etc/sudoers.d/tester && chmod 0440 /etc/sudoers.d/tester'
    docker run --rm --network fossor-t-boost \\
      -e FOSSOR_TEST_SSH_HOST=fossor-ssh-boost -e FOSSOR_TEST_SSH_PORT=2222 \\
      -e FOSSOR_TEST_SSH_USER=tester -e FOSSOR_TEST_SSH_PASSWORD=testerpass \\
      -v $PWD:/src:ro -w /src fossor-app:test \\
      python -m pytest tests/test_boost_integration.py -m integration -q
"""

from __future__ import annotations

import os
import uuid

import asyncssh
import pytest

pytestmark = pytest.mark.integration

HIVE_SCRIPTS = """set -e
sudo -n mkdir -p /hive/bin
printf '#!/bin/sh\\necho "miner $@" >> /tmp/boost-hive.log\\n' | sudo -n tee /hive/bin/miner >/dev/null
printf '#!/bin/sh\\necho "wd $@" >> /tmp/boost-hive.log\\n' | sudo -n tee /hive/bin/wd >/dev/null
sudo -n chmod 0755 /hive/bin/miner /hive/bin/wd
sudo -n sh -c ': > /tmp/boost-hive.log; chmod 0666 /tmp/boost-hive.log'
"""


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


async def _ssh(target: dict[str, str], command: str) -> tuple[str, str, int]:
    async with asyncssh.connect(
        target["host"], port=int(target["port"]), username=target["user"],
        password=target["password"], known_hosts=None,
    ) as conn:
        result = await conn.run(command, check=False)
        return str(result.stdout or ""), str(result.stderr or ""), int(result.exit_status)


class StubBackend:
    def __init__(self):
        self.calls = []

    async def start(self, machine, profile, argv, simulated):
        self.calls.append("campaign-start")
        return {"commands": ["campaign-start"]}

    async def stop(self, machine, profile, simulated):
        self.calls.append("campaign-stop")
        return {"commands": ["campaign-stop"]}

    async def hashrate(self, machine, profile):
        return 2000.0


@pytest.mark.asyncio
async def test_boost_hiveos_sequence_called_in_order_and_watchdog_restarted():
    target = _target()
    stdout, stderr, code = await _ssh(target, HIVE_SCRIPTS)
    assert code == 0, (stdout, stderr)

    from app import boost_runner as RUN
    from app import hiveos_remote as HR
    from app.database import SessionLocal
    from app.models import Device, Machine, MinerProfile
    from app.secrets import encrypt_secret

    db = SessionLocal()
    try:
        profile = MinerProfile(
            uuid=str(uuid.uuid4()), name="boost-hive", coin="NEWC",
            wallet="prl1testwalletfossor0000000000000000000000000000000000",
            pool="stratum+tcp://pool.exemple.tld:3333", software="srbminer",
            binary_url="http://192.0.2.1/srbminer.tar.gz", sha256="0" * 64,
            binary_path="/opt/fossor/miners/srbminer", version="1.0.0",
            cmdline="/bin/true", enabled=True,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        machine = Machine(
            uuid=str(uuid.uuid4()), name="rig-hiveos-faux", host=target["host"],
            ssh_port=int(target["port"]), ssh_user=target["user"],
            auth_type="password", secret_ref=encrypt_secret(target["password"]),
            usage="both", enabled=True, os_kind="hiveos",
            simulation_disabled=True, default_miner_profile_id=profile.id,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(
            uuid=str(uuid.uuid4()), machine_id=machine.id, kind="gpu", index=0,
            model="RTX faux", present=True, enabled_for_mining=True, boost_pool=True,
        ))
        db.commit()
        machine = db.get(Machine, machine.id)
        db.expunge(machine)

        # Sonde HiveOS factice : le rig a 1 GPU, watchdog actif.
        probe = {
            "detected": True,
            "miner": "srbminer",
            "miner_bin": "/hive/bin/miner",
            "gpu_total": 1,
            "mining_gpus": 1,
            "rig": {"watchdog": True, "maintenance": False},
            "processes": [],
        }

        async def fake_probe(db_, machine_, gpus_, simulated_):
            return probe

        RUN.hiveos_probe_data = fake_probe
        boost = RUN.create(db, {"coin": "NEWC", "profile_id": profile.id, "scope": "pool"})
        backend = StubBackend()
        started = await RUN.start_boost(db, boost, backend=backend, simulated=False)
        assert started["action"] == "started", started
        log, _, _ = await _ssh(target, "cat /tmp/boost-hive.log")
        lines = [line.strip() for line in log.splitlines() if line.strip()]
        print("\n### JOURNAL HIVE (ENTRÉE):", lines)
        assert lines[:2] == ["wd stop", "miner stop"], lines

        stop = await RUN.stop_boost(db, boost, backend=backend, simulated=False)
        db.refresh(boost)
        assert stop["verified"] is True
        log, _, _ = await _ssh(target, "cat /tmp/boost-hive.log")
        lines = [line.strip() for line in log.splitlines() if line.strip()]
        print("### JOURNAL HIVE (SORTIE):", lines)
        assert lines[-2:] == ["miner start", "wd start"], lines
        assert boost.status == "done"
        assert backend.calls == ["campaign-start", "campaign-stop"]
    finally:
        db.close()
