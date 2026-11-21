"""Intégration — le watchdog relance une unité factice sur la cible jetable.

Le conteneur ``fossor-ssh-wd`` (image ``fossor-sshd:test``) ne tourne pas sous
systemd : un faux ``systemctl``, installé par le test dans ``/usr/local/bin``,
écrit l'état de l'unité dans ``/tmp/watchdog-unit.state`` et la commande reçue
dans ``/tmp/watchdog-systemctl.log``.  L'unité démarre **arrêtée** ; le test
vérifie qu'elle est relancée par une vraie commande SSH et que le journal
``watchdog_events`` porte l'action réelle.

Aucune machine réelle n'est contactée.

Lancement :

    docker network create fossor-t-wd
    docker run -d --rm --name fossor-ssh-wd --network fossor-t-wd \\
      --network-alias fossor-ssh-wd fossor-sshd:test
    docker exec -u root fossor-ssh-wd sh -c \\
      'printf "tester ALL=(ALL) NOPASSWD:ALL\\n" > /etc/sudoers.d/tester && chmod 0440 /etc/sudoers.d/tester'
    docker run --rm --network fossor-t-wd \\
      -e FOSSOR_TEST_SSH_HOST=fossor-ssh-wd -e FOSSOR_TEST_SSH_PORT=2222 \\
      -e FOSSOR_TEST_SSH_USER=tester -e FOSSOR_TEST_SSH_PASSWORD=testerpass \\
      -v $PWD:/src:ro -w /src fossor-app:test \\
      python -m pytest tests/test_watchdog_integration.py -m integration -q
"""

from __future__ import annotations

import os
import uuid

import asyncssh
import pytest

pytestmark = pytest.mark.integration

SHIM = """#!/bin/sh
echo "$@" >> /tmp/watchdog-systemctl.log
case "$1" in
  restart) echo active > /tmp/watchdog-unit.state ;;
  stop) echo inactive > /tmp/watchdog-unit.state ;;
esac
exit 0
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


async def _install_shim(target: dict[str, str]) -> None:
    """Faux systemctl + unité factice à l'arrêt."""
    payload = SHIM.replace("'", "'\\''")
    await _ssh(target, "sudo -n rm -f /tmp/watchdog-unit.state /tmp/watchdog-systemctl.log")
    stdout, stderr, code = await _ssh(
        target,
        f"printf '%s' '{payload}' | sudo -n tee /usr/local/bin/systemctl > /dev/null"
        " && sudo -n chmod 0755 /usr/local/bin/systemctl"
        " && sudo -n sh -c 'echo inactive > /tmp/watchdog-unit.state;"
        " chmod 0666 /tmp/watchdog-unit.state; : > /tmp/watchdog-systemctl.log;"
        " chmod 0666 /tmp/watchdog-systemctl.log'",
    )
    assert code == 0, (stdout, stderr)


@pytest.mark.asyncio
async def test_unite_factice_arretee_relancee_et_journalisee():
    target = _target()
    await _install_shim(target)
    state, _, _ = await _ssh(target, "cat /tmp/watchdog-unit.state")
    assert state.strip() == "inactive"

    from app import watchdog as W
    from app.database import SessionLocal
    from app.models import Device, Machine, MinerProfile, WatchdogEvent
    from app.secrets import encrypt_secret

    db = SessionLocal()
    try:
        machine = Machine(
            uuid=str(uuid.uuid4()), name="unite-factice", host=target["host"],
            ssh_port=int(target["port"]), ssh_user=target["user"],
            auth_type="password", secret_ref=encrypt_secret(target["password"]),
            usage="both", enabled=True, simulation_disabled=True,
        )
        db.add(machine)
        db.commit()
        db.refresh(machine)
        profile = MinerProfile(
            uuid=str(uuid.uuid4()), name="unite-factice", coin="PRL",
            wallet="prl1testwalletfossor0000000000000000000000000000000000",
            pool="stratum+tcp://pool.exemple.tld:3360", software="srbminer",
            binary_url="http://192.0.2.1/srbminer.tar.gz", sha256="0" * 64,
            binary_path="/opt/fossor/miners/srbminer", version="2.0.0",
            cmdline="/opt/fossor/miners/srbminer --wallet {wallet} --pool {pool}",
            enabled=True,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        db.add(Device(
            uuid=str(uuid.uuid4()), machine_id=machine.id, kind="cpu",
            model="Faux processeur", present=True, enabled_for_mining=True,
            default_token_id=profile.id,
        ))
        machine.default_miner_profile_id = profile.id
        db.commit()

        # Clé d'hôte épinglée avant l'évaluation, comme après le test de
        # connexion du provisionnement : sinon l'épinglage (écriture depuis la
        # connexion SSH) se heurte à la transaction de lecture du test.
        db.refresh(machine)
        db.expunge(machine)
        from app.ssh_client import ssh_run_command

        pinned = await ssh_run_command(machine, "echo ok")
        assert pinned.get("ok"), pinned

        config = W.normalize({})
        for rule in W.RULE_NAMES:
            config["rules"][rule]["enabled"] = (rule == "miner_absent")
        config["grace_minutes"] = 0
        config["escalation_minutes"] = 0
        W.save_config(db, config)

        events = await W.evaluate_machine(
            db, machine, stats={}, raw_stdout="root 1 0.0 /usr/bin/autre-chose")
        assert [event["action"] for event in events] == ["restart_miner"], events
        row = db.query(WatchdogEvent).order_by(WatchdogEvent.id.desc()).first()
        assert row.rule == "miner_absent"
        assert row.result == "ok" and row.simulated is False
        assert "fossor-miner-unite-factice.service" in row.detail

        state, _, _ = await _ssh(target, "cat /tmp/watchdog-unit.state")
        log, _, _ = await _ssh(target, "cat /tmp/watchdog-systemctl.log")
        print("\n### UNITÉ APRÈS RELANCE:", state.strip())
        print("### COMMANDE REÇUE:", log.strip())
        assert state.strip() == "active"
        assert "restart fossor-miner-unite-factice.service" in log
    finally:
        db.close()
