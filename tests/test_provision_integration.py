"""Real integration (§4.1 bis mode 3) against a throwaway SSH server.

The server is built by ``tests/docker/sshd-ubuntu.Dockerfile``: Ubuntu 24.04 +
openssh-server + sudo, user ``tester`` with password access and sudo. Ubuntu
(not Alpine) because ``useradd`` and ``visudo`` are what HiveOS provides.
"""

from __future__ import annotations

import json
import os

import pytest

HOST = os.environ.get("FOSSOR_TEST_PROVISION_HOST", "")
PORT = int(os.environ.get("FOSSOR_TEST_PROVISION_PORT", "2222"))
TEMP_USER = os.environ.get("FOSSOR_TEST_SSH_USER", "tester")
TEMP_PASSWORD = os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HOST, reason="FOSSOR_TEST_PROVISION_HOST absent"),
]


async def test_provision_then_key_connection_then_deprovision(client, capsys):
    created = await client.post(
        "/api/machines",
        json={
            "name": "jetable-provision",
            "host": HOST,
            "ssh_port": PORT,
            "ssh_user": TEMP_USER,
            "auth_type": "password",
            "secret": TEMP_PASSWORD,
        },
    )
    assert created.status_code == 200, created.text
    machine_id = created.json()["id"]

    response = await client.post(
        f"/api/machines/{machine_id}/provision",
        json={"temp_user": TEMP_USER, "temp_password": TEMP_PASSWORD, "account": "fossor"},
    )
    body = response.json()
    with capsys.disabled():
        print("\nPROVISION JSON:", json.dumps(body, ensure_ascii=False, indent=2))
    assert response.status_code == 200, body
    assert body["ok"] is True, body
    assert "testerpass" not in json.dumps(body)

    machine = (await client.get(f"/api/machines/{machine_id}")).json()
    assert machine["auth_type"] == "fossor_key"
    assert machine["ssh_user"] == "fossor"
    assert machine["has_secret"] is False

    tested = (await client.post(f"/api/machines/{machine_id}/test")).json()
    with capsys.disabled():
        print("\nTEST JSON:", json.dumps(tested, ensure_ascii=False, indent=2))
    assert tested["ok"] is True, tested
    assert tested["user"] == "fossor"

    removed = (
        await client.post(
            f"/api/machines/{machine_id}/deprovision", json={"delete_account": False}
        )
    ).json()
    with capsys.disabled():
        print("\nDEPROVISION JSON:", json.dumps(removed, ensure_ascii=False, indent=2))
    assert removed["ok"] is True, removed
