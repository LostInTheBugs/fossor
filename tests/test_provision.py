"""§4.1 bis mode 3 — provisioning: validation, secret hygiene, rollback.

Everything runs against a fake asyncssh connection: no rig, no real machine.
"""

from __future__ import annotations

import json
import logging

import pytest


# ── Validation ────────────────────────────────────────────────────────
class TestAccountValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "fossor; rm -rf /",
            "$(id)",
            "`id`",
            "fos sor",
            "Fossor",
            "-fossor",
            "1fossor",
            "fossor\nroot",
            "a" * 33,
            "",
        ],
    )
    def test_rejected(self, name):
        from app.provision import ProvisionError, validate_account

        with pytest.raises(ProvisionError):
            validate_account(name)

    @pytest.mark.parametrize("name", ["fossor", "_svc", "miner-01", "a" * 32, "a_b-c9"])
    def test_accepted(self, name):
        from app.provision import validate_account

        assert validate_account(name) == name


class TestPublicKeyValidation:
    def test_own_key_accepted(self, setup_test_env):
        from app import fossor_keys
        from app.provision import validate_public_key

        key = fossor_keys.public_key_text()
        assert validate_public_key(key) == key
        assert validate_public_key(key, expected=key) == key

    def test_foreign_key_refused(self, setup_test_env):
        import asyncssh

        from app import fossor_keys
        from app.provision import ProvisionError, validate_public_key

        other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
        with pytest.raises(ProvisionError):
            validate_public_key(other, expected=fossor_keys.public_key_text())

    @pytest.mark.parametrize(
        "line",
        [
            "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQ== user@host",
            "not-a-key",
            "ssh-ed25519 !!!!not-base64!!!! fossor@x",
            "",
        ],
    )
    def test_garbage_refused(self, line):
        from app.provision import ProvisionError, validate_public_key

        with pytest.raises(ProvisionError):
            validate_public_key(line)


# ── Fake SSH plumbing ─────────────────────────────────────────────────
class _Reader:
    def __init__(self, text: str):
        self._text = text

    async def read(self) -> str:
        return self._text


class _Stdin:
    def __init__(self):
        self.written: list[str] = []
        self.eof = False

    def write(self, data):
        self.written.append(data)

    def write_eof(self):
        self.eof = True


class _Proc:
    def __init__(self, stdout: str, stderr: str, status: int):
        self.stdout = _Reader(stdout)
        self.stderr = _Reader(stderr)
        self._status = status
        self.stdin = _Stdin()

    async def wait(self) -> int:
        return self._status


class FakeConn:
    """Returns scripted results and records every command and stdin write."""

    def __init__(self, responses: list[tuple[str, str, int]]):
        self.responses = list(responses)
        self.commands: list[str] = []
        self.stdin_writes: list[str] = []
        self.closed = False

    async def create_process(self, script: str):
        self.commands.append(script)
        out, err, status = self.responses.pop(0)
        proc = _Proc(out, err, status)
        original_write = proc.stdin.write

        def _record(data):
            self.stdin_writes.append(data)
            original_write(data)

        proc.stdin.write = _record  # type: ignore[method-assign]
        return proc

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def _machine(db, password: str):
    from app.models import Machine
    from app.secrets import encrypt_secret

    machine = Machine(
        name="jetable",
        host="fossor-ssh-alt",
        ssh_port=2222,
        ssh_user="tester",
        auth_type="password",
        secret_ref=encrypt_secret(password),
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def _patch_connections(monkeypatch, conns):
    from app import ssh_client

    queue = list(conns)

    async def fake_open(**kwargs):
        conn = queue.pop(0)
        return conn, "SHA256:fake", None

    monkeypatch.setattr(ssh_client, "open_pinned_connection", fake_open)


# ── Behaviour ─────────────────────────────────────────────────────────
class TestProvision:
    async def test_success_switches_mode_and_erases_secret(
        self, setup_test_env, monkeypatch, caplog
    ):
        from app.database import SessionLocal
        from app.models import AuditLog
        from app.provision import provision_machine

        caplog.set_level(logging.DEBUG)
        db = SessionLocal()
        machine = _machine(db, "SuperSecret-42")
        temp = FakeConn(
            [
                ("PREFLIGHT_OK\n", "", 0),
                ("ACCOUNT_CREATED\n", "", 0),
                ("KEY_INSTALLED\n", "", 0),
                ("GPU_WRAPPER_INSTALLED\n", "", 0),
                ("SUDOERS_INSTALLED\n", "", 0),
            ]
        )
        verify = FakeConn([("SUDO_OK\n", "", 0)])
        _patch_connections(monkeypatch, [temp, verify])

        result = await provision_machine(machine, "tester", "SuperSecret-42", "fossor")
        assert result["ok"] is True, result
        assert result["account"] == "fossor"
        assert "SuperSecret-42" not in json.dumps(result)
        # sudo -S got the password on stdin, never in a command line.
        assert "SuperSecret-42\n" in temp.stdin_writes
        assert all("SuperSecret-42" not in cmd for cmd in temp.commands)
        assert any("authorized_keys" in cmd for cmd in temp.commands)
        assert any("visudo -cf" in cmd for cmd in temp.commands)
        assert any("install -m 0440" in cmd for cmd in temp.commands)
        assert any("sudo -n -l /usr/local/sbin/fossor-gpu-pl" in cmd for cmd in verify.commands)

        db.expire_all()
        machine = db.query(type(machine)).filter_by(id=machine.id).first()
        assert machine.auth_type == "fossor_key"
        assert machine.ssh_user == "fossor"
        assert machine.secret_ref is None
        assert machine.sudo == "sudo without password"

        audits = db.query(AuditLog).all()
        assert all("SuperSecret-42" not in (a.output or "") for a in audits)
        assert any(a.action == "provision" and a.result == "ok" for a in audits)
        assert "SuperSecret-42" not in caplog.text
        db.close()

    async def test_failure_at_sudoers_rolls_back(
        self, setup_test_env, monkeypatch, caplog
    ):
        from app.database import SessionLocal
        from app.models import AuditLog
        from app.provision import provision_machine

        caplog.set_level(logging.DEBUG)
        db = SessionLocal()
        machine = _machine(db, "SuperSecret-42")
        temp = FakeConn(
            [
                ("PREFLIGHT_OK\n", "", 0),
                ("ACCOUNT_CREATED\n", "", 0),
                ("KEY_INSTALLED\n", "", 0),
                ("GPU_WRAPPER_INSTALLED\n", "", 0),
                ("", "visudo: syntax error", 1),
                ("KEY_REMOVED\n", "", 0),
                ("ACCOUNT_DELETED\n", "", 0),
            ]
        )
        _patch_connections(monkeypatch, [temp])

        result = await provision_machine(machine, "tester", "SuperSecret-42", "fossor")
        assert result["ok"] is False
        assert "fichier sudoers" in result["error"]
        assert "SuperSecret-42" not in result["error"]
        joined = "\n".join(temp.commands)
        assert "userdel" in joined
        assert "authorized_keys" in joined
        # The machine keeps its temporary password mode: nothing was switched.
        db.expire_all()
        machine = db.query(type(machine)).filter_by(id=machine.id).first()
        assert machine.auth_type == "password"
        entry = db.query(AuditLog).filter(AuditLog.action == "provision").order_by(AuditLog.id.desc()).first()
        assert entry.result == "fail"
        assert "préparation échouée à l'étape « fichier sudoers »" in entry.output
        assert "SuperSecret-42" not in (entry.output or "")
        db.close()

    async def test_preflight_missing_useradd_is_explicit(self, setup_test_env, monkeypatch):
        from app.database import SessionLocal
        from app.provision import provision_machine

        db = SessionLocal()
        machine = _machine(db, "SuperSecret-42")
        temp = FakeConn([("MISSING:useradd\n", "", 21)])
        _patch_connections(monkeypatch, [temp])

        result = await provision_machine(machine, "tester", "SuperSecret-42", "fossor")
        assert result["ok"] is False
        assert "useradd" in result["error"]
        assert "préparation automatique impossible" in result["error"]
        db.close()

    async def test_verification_failure_rolls_back_through_temp_access(
        self, setup_test_env, monkeypatch
    ):
        from app.database import SessionLocal
        from app.provision import provision_machine

        db = SessionLocal()
        machine = _machine(db, "SuperSecret-42")
        temp = FakeConn(
            [
                ("PREFLIGHT_OK\n", "", 0),
                ("ACCOUNT_CREATED\n", "", 0),
                ("KEY_INSTALLED\n", "", 0),
                ("GPU_WRAPPER_INSTALLED\n", "", 0),
                ("SUDOERS_INSTALLED\n", "", 0),
            ]
        )
        verify = FakeConn([("SUDO_FAIL\n", "", 1)])
        rollback = FakeConn(
            [
                ("SUDOERS_REMOVED\n", "", 0),
                ("KEY_REMOVED\n", "", 0),
                ("ACCOUNT_DELETED\n", "", 0),
            ]
        )
        _patch_connections(monkeypatch, [temp, verify, rollback])

        result = await provision_machine(machine, "tester", "SuperSecret-42", "fossor")
        assert result["ok"] is False
        assert "vérification par clé" in result["error"]
        assert any("userdel" in cmd for cmd in rollback.commands)
        db.expire_all()
        machine = db.query(type(machine)).filter_by(id=machine.id).first()
        assert machine.auth_type == "password"
        db.close()


class TestProvisionRoutes:
    async def test_bad_account_is_400_before_any_connection(self, client):
        created = await client.post(
            "/api/machines",
            json={"name": "m1", "host": "192.0.2.10", "auth_type": "password", "secret": "x"},
        )
        assert created.status_code == 200, created.text
        mid = created.json()["id"]
        response = await client.post(
            f"/api/machines/{mid}/provision",
            json={"temp_user": "user", "temp_password": "x", "account": "fossor; id"},
        )
        assert response.status_code == 400
        assert "Nom de compte invalide" in response.json()["detail"]

    async def test_missing_temp_password_is_400(self, client):
        created = await client.post(
            "/api/machines",
            json={"name": "m2", "host": "192.0.2.11", "auth_type": "password", "secret": "x"},
        )
        mid = created.json()["id"]
        response = await client.post(
            f"/api/machines/{mid}/provision",
            json={"temp_user": "user", "temp_password": "", "account": "fossor"},
        )
        assert response.status_code == 400

    async def test_deprovision_refuses_password_machine(self, client):
        created = await client.post(
            "/api/machines",
            json={"name": "m3", "host": "192.0.2.12", "auth_type": "password", "secret": "x"},
        )
        mid = created.json()["id"]
        response = await client.post(f"/api/machines/{mid}/deprovision", json={})
        assert response.status_code == 400
        assert "clé de Fossor" in response.json()["detail"]

    async def test_public_key_endpoint(self, client):
        response = await client.get("/api/ssh/public-key")
        assert response.status_code == 200
        assert response.text.startswith("ssh-ed25519 ")
        assert "fossor@" in response.text
