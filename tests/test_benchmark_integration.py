"""Intégration — benchmark sur le faux hôte SSH, avec un faux `nvidia-smi`.

Le faux outil retient la limite reçue dans ``/tmp/fake-nvidia-smi.state``,
répond ``power.limit`` avec cette valeur et ``power.draw`` à 96 % de celle-ci :
les trois commandes en liste blanche du benchmark (§10.4) sont exercées par de
vraies connexions SSH, sans aucune machine réelle.

Lancement :

    docker network create fossor-t-bench
    docker run -d --rm --name fossor-ssh-bench --network fossor-t-bench \
      --network-alias fossor-ssh-bench fossor-sshd:test
    docker exec -u root fossor-ssh-bench sh -c \
      'printf "tester ALL=(ALL) NOPASSWD:ALL\\n" > /etc/sudoers.d/tester && chmod 0440 /etc/sudoers.d/tester'
    docker run --rm --network fossor-t-bench \
      -e FOSSOR_TEST_SSH_HOST=fossor-ssh-bench -e FOSSOR_TEST_SSH_PORT=2222 \
      -e FOSSOR_TEST_SSH_USER=tester -e FOSSOR_TEST_SSH_PASSWORD=testerpass \
      -v $PWD:/src:ro -w /src fossor-app:test \
      python -m pytest tests/test_benchmark_integration.py -m integration -q
"""

from __future__ import annotations

import os
import uuid

import asyncssh
import pytest

pytestmark = pytest.mark.integration

FAKE_NVIDIA = """#!/bin/sh
STATE=/tmp/fake-nvidia-smi.state
LOG=/var/log/fossor-bench/nvidia.log
echo "$@" >> $LOG
PL=$(cat $STATE 2>/dev/null || echo 180)
case "$*" in
  *power.min_limit*) echo "0, 100.00, 250.00, 180.00" ; exit 0 ;;
  *power.draw*)
    P=$(awk "BEGIN{printf \\"%.2f\\", $PL*0.96}")
    echo "0, $P, 55, $PL.00"
    exit 0 ;;
  *power.limit*) echo "$PL.00" ; exit 0 ;;
esac
if [ "$1" = "-i" ]; then
  echo $4 > $STATE
  echo "Power limit for GPU $2 was set to $4.00 W"
  exit 0
fi
exit 0
"""


FAKE_WRAPPER = """#!/bin/sh
echo "$@" >> /var/log/fossor-bench/wrapper.log
exec /usr/local/bin/nvidia-smi -i "$1" -pl "$2"
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


async def _install_fake(target: dict[str, str]) -> None:
    payload = FAKE_NVIDIA.replace("'", "'\\''")
    wrapper = FAKE_WRAPPER.replace("'", "'\\''")
    await _ssh(target, "sudo -n rm -f /tmp/fake-nvidia-smi.state /var/log/fossor-bench/nvidia.log"
                       " /var/log/fossor-bench/wrapper.log")
    await _ssh(target, "sudo -n install -d -m 0777 /var/log/fossor-bench"
                       " && sudo -n sh -c 'install -m 0666 /dev/null /var/log/fossor-bench/nvidia.log"
                       " /var/log/fossor-bench/wrapper.log'")
    stdout, stderr, code = await _ssh(
        target,
        f"printf '%s' '{payload}' | sudo -n tee /usr/local/bin/nvidia-smi > /dev/null"
        " && sudo -n chmod 0755 /usr/local/bin/nvidia-smi"
        f" && printf '%s' '{wrapper}' | sudo -n tee /usr/local/sbin/fossor-gpu-pl > /dev/null"
        " && sudo -n chmod 0755 /usr/local/sbin/fossor-gpu-pl",
    )
    assert code == 0, (stdout, stderr)


@pytest.mark.asyncio
async def test_bornes_limite_et_sonde_reelles_par_ssh():
    target = _target()
    await _install_fake(target)

    from app import benchmark as B
    from app import benchmark_runner as RUN
    from app import ssh_client as S
    from app.database import SessionLocal
    from app.models import Machine
    from app.secrets import encrypt_secret

    db = SessionLocal()
    try:
        machine = Machine(
            uuid=str(uuid.uuid4()), name="bench-factice", host=target["host"],
            ssh_port=int(target["port"]), ssh_user=target["user"],
            auth_type="password", secret_ref=encrypt_secret(target["password"]),
            usage="gpu", enabled=True, simulation_disabled=True,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)

        limits = await S.ssh_gpu_power_limits(machine)
        assert limits["ok"] is True
        assert limits["limits"][0] == {"min": 100.0, "max": 250.0, "default": 180.0}

        set_result = await S.ssh_set_gpu_power_limit(machine, 0, 150)
        assert set_result["ok"] is True
        assert "set to 150" in set_result["stdout"]
        log, _, _ = await _ssh(target, "cat /var/log/fossor-bench/nvidia.log")
        wrapper_log, _, _ = await _ssh(target, "cat /var/log/fossor-bench/wrapper.log")
        assert "-i 0 -pl 150" in log, (log, wrapper_log, set_result["stdout"])
        assert wrapper_log.strip() == "0 150", (log, wrapper_log, set_result["stdout"])

        probe = await S.ssh_benchmark_probe(machine)
        assert probe["ok"] is True, probe
        assert 0 in probe["gpus"], (probe.get("stdout"), probe)
        assert probe["gpus"][0]["limit_w"] == 150.0
        assert probe["gpus"][0]["power_w"] == pytest.approx(144.0)

        class _Device:
            index = 0

        bounds = await RUN.bounds_for(db, machine, _Device(), simulated=False)
        assert bounds == {"min": 100.0, "max": 250.0, "default": 180.0}
        assert [row["watts"] for row in B.levels(bounds["min"], bounds["max"], bounds["default"])] == [
            180, 162, 144, 126, 108]
    finally:
        db.close()
