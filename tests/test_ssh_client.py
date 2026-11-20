"""Tests for app/ssh_client.py.

Unit tests use *real* asyncssh key objects (generated on the fly) and mock
only the network calls, so key handling is exercised for real.

Integration tests talk to a throwaway SSH container and are skipped unless
FOSSOR_TEST_SSH_HOST / _PORT / _USER / _PASSWORD are set.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from app.ssh_client import (
    WHITELISTED_COMMANDS,
    _fingerprint_of,
    _public_key_of,
    reset_machine_host_key,
    ssh_detect_hw,
    ssh_test_machine,
)

SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")
SERVER_PUB = _public_key_of(SERVER_KEY)
SERVER_FP = _fingerprint_of(SERVER_KEY)
OTHER_KEY = asyncssh.generate_private_key("ssh-ed25519")


class _FakeMachine:
    def __init__(
        self,
        id=1,
        host="10.0.0.99",
        port=22,
        user="root",
        auth_type="key",
        secret_ref=None,
        host_key=None,
        host_key_public=None,
        os_kind="other",
    ):
        self.id = id
        self.host = host
        self.ssh_port = port
        self.ssh_user = user
        self.auth_type = auth_type
        self.secret_ref = secret_ref
        self.host_key = host_key
        self.host_key_public = host_key_public
        self.os_kind = os_kind


class _FakeConn:
    def __init__(self, result):
        self._result = result

    async def run(self, command, **kwargs):
        assert command == WHITELISTED_COMMANDS["identify"]
        return self._result

    def close(self):
        pass

    async def wait_closed(self):
        pass


def _result(stdout="", exit_code=0):
    r = MagicMock()
    r.stdout = stdout
    r.stderr = ""
    r.exit_code = exit_code
    return r


def _patch_connect(conn, calls=None):
    async def fake_connect(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return conn

    return patch("asyncssh.connect", side_effect=fake_connect)


def _patch_key():
    async def fake_key(host, port):
        return SERVER_KEY

    return patch("app.ssh_client.read_server_host_key", side_effect=fake_key)


# ── Whitelist ─────────────────────────────────────────────────────────
class TestWhitelist:
    def test_only_whitelisted_commands(self):
        assert set(WHITELISTED_COMMANDS.keys()) == {
            "identify", "detect_hw", "collect_stats", "inventory", "miner_api", "hiveos_probe",
            "solo_getbalance"
        }

    def test_collect_stats_has_five_sections_and_no_sudo(self):
        cmd = WHITELISTED_COMMANDS["collect_stats"]
        for section in ("HIVE_STAT", "HIVE_GPU", "NVSMI", "PS_MINERS", "UPTIME"):
            assert f"### {section}" in cmd
        assert "sudo" not in cmd
        assert cmd.rstrip().endswith("exit 0")

    def test_identify_has_expected_parts(self):
        cmd = WHITELISTED_COMMANDS["identify"]
        for part in ("hostname", "whoami", "uname", "os-release", "HIVEOS"):
            assert part in cmd

    def test_no_shell_subprocess(self):
        for cmd in WHITELISTED_COMMANDS.values():
            assert "subprocess" not in cmd
            assert "shell=" not in cmd


# ── Key helpers ───────────────────────────────────────────────────────
class TestKeyHelpers:
    def test_fingerprint_format(self):
        assert SERVER_FP.startswith("SHA256:")

    def test_different_keys_different_fingerprints(self):
        assert _fingerprint_of(OTHER_KEY) != SERVER_FP

    def test_public_key_roundtrip(self):
        imported = asyncssh.import_public_key(SERVER_PUB)
        assert _fingerprint_of(imported) == SERVER_FP


# ── Host-key pinning ──────────────────────────────────────────────────
class TestHostKeyPinning:
    @pytest.mark.asyncio
    async def test_first_contact_pins_before_auth(self):
        machine = _FakeMachine(host_key=None, host_key_public=None)
        pinned, connect_calls = [], []

        with _patch_key(), _patch_connect(_FakeConn(_result("box\nroot\nLinux 6.1 x86_64\n")), connect_calls):
            with patch("app.ssh_client._pin_host_key", side_effect=lambda *a: pinned.append(a)):
                with patch("app.ssh_client._audit_log"):
                    with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                        res = await ssh_test_machine(machine)

        assert res["ok"] is True, res
        assert res["host_key_fingerprint"] == SERVER_FP
        assert len(pinned) == 1 and pinned[0][0] == machine.id
        assert pinned[0][1] == SERVER_FP
        assert pinned[0][2] == SERVER_PUB
        # the very same key is handed to asyncssh for the first connection
        trusted = connect_calls[0]["known_hosts"][0][0]
        assert _fingerprint_of(trusted) == SERVER_FP
        assert connect_calls[0]["known_hosts"][1] == []
        assert connect_calls[0]["known_hosts"][2] == []

    @pytest.mark.asyncio
    async def test_changed_fingerprint_refused_before_connect(self):
        machine = _FakeMachine(host_key=_fingerprint_of(OTHER_KEY), host_key_public=_public_key_of(OTHER_KEY))
        connect_calls = []

        with _patch_key(), _patch_connect(_FakeConn(_result()), connect_calls):
            with patch("app.ssh_client._audit_log") as audit:
                with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                    res = await ssh_test_machine(machine)

        assert res["ok"] is False
        assert "Empreinte changée" in res["error"]
        assert _fingerprint_of(OTHER_KEY) in res["error"]
        assert SERVER_FP in res["error"]
        assert connect_calls == [], "aucune connexion ne doit être tentée"
        assert audit.called

    @pytest.mark.asyncio
    async def test_pinned_key_is_accepted(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=SERVER_PUB)
        connect_calls = []

        with _patch_key(), _patch_connect(_FakeConn(_result("box\nroot\nLinux 6.1 x86_64\n")), connect_calls):
            with patch("app.ssh_client._pin_host_key") as pin:
                with patch("app.ssh_client._audit_log"):
                    with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                        res = await ssh_test_machine(machine)

        assert res["ok"] is True, res
        pin.assert_not_called()
        trusted = connect_calls[0]["known_hosts"][0][0]
        assert _fingerprint_of(trusted) == SERVER_FP

    @pytest.mark.asyncio
    async def test_pinned_fingerprint_without_public_key_is_refused(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=None)
        connect_calls = []

        with _patch_key(), _patch_connect(_FakeConn(_result()), connect_calls):
            with patch("app.ssh_client._audit_log"):
                with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                    res = await ssh_test_machine(machine)

        assert res["ok"] is False
        assert "reset-host-key" in res["error"]
        assert connect_calls == []

    @pytest.mark.asyncio
    async def test_asyncssh_rejection_is_reported(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=SERVER_PUB)

        async def refuse(**kwargs):
            raise asyncssh.HostKeyNotVerifiable("key changed")

        with _patch_key(), patch("asyncssh.connect", side_effect=refuse):
            with patch("app.ssh_client._audit_log"):
                with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                    res = await ssh_test_machine(machine)

        assert res["ok"] is False
        assert "Empreinte changée" in res["error"]


# ── Connection outcomes ───────────────────────────────────────────────
class TestOutcomes:
    @pytest.mark.asyncio
    async def test_success_parses_output(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=SERVER_PUB)
        stdout = 'hivebox\nminer\nLinux 5.15.0 aarch64\nNAME="Ubuntu"\nHIVEOS=1\n'

        with _patch_key(), _patch_connect(_FakeConn(_result(stdout))):
            with patch("app.ssh_client._audit_log"):
                with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                    res = await ssh_test_machine(machine)

        assert res["ok"] is True
        assert res["hostname"] == "hivebox"
        assert res["user"] == "miner"
        assert res["uname"].startswith("Linux 5.15.0")
        assert res["os_kind"] == "hiveos"

    @pytest.mark.asyncio
    async def test_proxmox_detection(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=SERVER_PUB)
        stdout = 'pve\nroot\nLinux 6.2.0 x86_64\nNAME="Proxmox VE"\n'

        with _patch_key(), _patch_connect(_FakeConn(_result(stdout))):
            with patch("app.ssh_client._audit_log"):
                with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                    res = await ssh_test_machine(machine)

        assert res["os_kind"] == "proxmox"

    @pytest.mark.asyncio
    async def test_permission_denied(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=SERVER_PUB)

        async def deny(**kwargs):
            raise asyncssh.PermissionDenied("auth failed")

        with _patch_key(), patch("asyncssh.connect", side_effect=deny):
            with patch("app.ssh_client._audit_log"):
                with patch("app.ssh_client.decrypt_secret", return_value="wrongpass"):
                    res = await ssh_test_machine(machine)

        assert res["ok"] is False
        assert "refusés par la machine" in res["error"]
        assert "Permission denied" not in res["error"]
        assert "wrongpass" not in res["error"]

    @pytest.mark.asyncio
    async def test_connect_timeout(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=SERVER_PUB)

        async def slow(**kwargs):
            raise asyncio.TimeoutError()

        with _patch_key(), patch("asyncssh.connect", side_effect=slow):
            with patch("app.ssh_client._audit_log"):
                with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                    res = await ssh_test_machine(machine)

        assert res["ok"] is False
        assert "timed out" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_host_key_read_failure(self):
        machine = _FakeMachine(host_key=None)

        async def unreachable(host, port):
            raise OSError("host unreachable")

        with patch("app.ssh_client.read_server_host_key", side_effect=unreachable):
            with patch("app.ssh_client._audit_log"):
                with patch("app.ssh_client.decrypt_secret", return_value="pw"):
                    res = await ssh_test_machine(machine)

        assert res["ok"] is False
        assert "Connection failed" in res["error"]

    @pytest.mark.asyncio
    async def test_no_secret_leaks(self):
        machine = _FakeMachine(host_key=SERVER_FP, host_key_public=SERVER_PUB, auth_type="password", secret_ref="enc")

        with _patch_key(), _patch_connect(_FakeConn(_result("box\nroot\nLinux 6.0\n"))):
            with patch("app.ssh_client._audit_log") as audit:
                with patch("app.ssh_client.decrypt_secret", return_value="S3cretP@ss!"):
                    res = await ssh_test_machine(machine)

        assert "S3cretP@ss!" not in str(res)
        assert "S3cretP@ss!" not in str(audit.call_args_list)


# ── Reset ─────────────────────────────────────────────────────────────
class TestResetHostKey:
    @pytest.mark.asyncio
    async def test_reset_unknown_machine(self):
        with patch("app.ssh_client._audit_log"):
            res = await reset_machine_host_key(99999)
        assert res["ok"] is False
        assert "not found" in res["error"].lower()


# ── Integration (real server, opt-in) ─────────────────────────────────
SSH_HOST = os.environ.get("FOSSOR_TEST_SSH_HOST")
SSH_PORT = int(os.environ.get("FOSSOR_TEST_SSH_PORT", "2222"))
SSH_USER = os.environ.get("FOSSOR_TEST_SSH_USER", "tester")
SSH_PASSWORD = os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass")


@pytest.mark.integration
@pytest.mark.skipif(not SSH_HOST, reason="FOSSOR_TEST_SSH_HOST not set")
class TestAgainstRealServer:
    """Real asyncssh handshake against a throwaway container."""

    def _machine(self, **kw):
        defaults = dict(
            host=SSH_HOST,
            port=SSH_PORT,
            user=SSH_USER,
            auth_type="password",
            secret_ref="enc",
            host_key=None,
            host_key_public=None,
        )
        defaults.update(kw)
        return _FakeMachine(**defaults)

    def test_pin_then_auth(self):
        machine = self._machine()
        with patch("app.ssh_client.decrypt_secret", return_value=SSH_PASSWORD):
            res = asyncio.run(ssh_test_machine(machine))

        assert res["ok"] is True, res
        assert res["host_key_fingerprint"].startswith("SHA256:")
        assert res["hostname"]
        # pinning really happened server-side: the key read without auth is the
        # same one asyncssh then accepted for the authenticated connection
        stored_key = asyncio.run(asyncssh.get_server_host_key(SSH_HOST, SSH_PORT))
        assert res["host_key_fingerprint"] == _fingerprint_of(stored_key)

    def test_wrong_password(self):
        key = asyncio.run(asyncssh.get_server_host_key(SSH_HOST, SSH_PORT))
        machine = self._machine(
            host_key=_fingerprint_of(key), host_key_public=_public_key_of(key)
        )
        with patch("app.ssh_client.decrypt_secret", return_value="nope-not-it"):
            res = asyncio.run(ssh_test_machine(machine))
        assert res["ok"] is False
        assert "refusés par la machine" in res["error"]
        assert "Permission denied" not in res["error"]

    def test_detect_hw_reports_cpu_and_no_gpu(self):
        key = asyncio.run(asyncssh.get_server_host_key(SSH_HOST, SSH_PORT))
        machine = self._machine(
            host_key=_fingerprint_of(key), host_key_public=_public_key_of(key)
        )
        with patch("app.ssh_client.decrypt_secret", return_value=SSH_PASSWORD), \
                patch("app.ssh_client._audit_log"):
            res = asyncio.run(ssh_detect_hw(machine))

        assert res["ok"] is True, res
        assert res["cpu"] is not None
        assert res["cpu"]["model"]
        assert res["cpu"]["threads"] >= 1
        assert res["cpu"]["cores"] >= 1
        assert res["gpus"] == []
        assert res["virt"] == {"kvm": False, "virsh": False, "pveversion": False}

    def test_detect_hw_refuses_changed_fingerprint(self):
        other_key = asyncssh.generate_private_key("ssh-ed25519")
        machine = self._machine(
            host_key="SHA256:not-the-server-fingerprint",
            host_key_public=other_key.export_public_key().decode().strip(),
        )
        with patch("app.ssh_client.decrypt_secret", return_value=SSH_PASSWORD), \
                patch("app.ssh_client._audit_log") as audit:
            res = asyncio.run(ssh_detect_hw(machine))

        assert res["ok"] is False
        assert res["error"].startswith("Empreinte changée")
        assert any(call.args[1] == "detect" for call in audit.call_args_list)
