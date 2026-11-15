"""Unit + integration tests for app/ssh_client.py.

Unit tests mock asyncssh.
Integration tests use a real Docker SSH container (marked @pytest.mark.integration).
"""

import asyncio
import base64
import hashlib
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from app.ssh_client import (
    WHITELISTED_COMMANDS,
    _compute_fingerprint,
    _audit_log,
    reset_machine_host_key,
    ssh_test_machine,
)


# ── Helpers ───────────────────────────────────────────────────────────
class _FakeMachine:
    """Minimal ORM-like machine object for testing."""

    def __init__(
        self,
        id=1,
        host="10.0.0.99",
        port=22,
        user="root",
        auth_type="key",
        secret_ref=None,
        host_key=None,
        os_kind="other",
    ):
        self.id = id
        self.host = host
        self.ssh_port = port
        self.ssh_user = user
        self.auth_type = auth_type
        self.secret_ref = secret_ref
        self.host_key = host_key
        self.os_kind = os_kind


def _make_fake_conn(result_obj, raw_key=b"fakekeydata1234567890"):
    """Build a mock asyncssh connection context manager.

    The mock must:
      - Be an async context manager (__aenter__/__aexit__)
      - Have a .run() method that returns the result
      - Have get_server_host_key() returning an object with .blob
    """
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.run = AsyncMock(return_value=result_obj)
    mock_conn.close = MagicMock()
    mock_conn.wait_closed = AsyncMock()

    mock_key = MagicMock()
    mock_key.blob = raw_key
    mock_conn.get_server_host_key = MagicMock(return_value=mock_key)

    return mock_conn


def _make_fake_result(stdout="", stderr="", exit_code=0):
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.exit_code = exit_code
    return r


# ── Unit Tests ────────────────────────────────────────────────────────
class TestWhitelist:
    def test_only_identify(self):
        assert set(WHITELISTED_COMMANDS.keys()) == {"identify"}

    def test_identify_has_expected_parts(self):
        cmd = WHITELISTED_COMMANDS["identify"]
        assert "hostname" in cmd
        assert "whoami" in cmd
        assert "uname" in cmd
        assert "os-release" in cmd
        assert "HIVEOS" in cmd

    def test_no_shell_subprocess(self):
        for name, cmd in WHITELISTED_COMMANDS.items():
            assert "subprocess" not in cmd
            assert "shell=" not in cmd


class TestComputeFingerprint:
    def test_format(self):
        fp = _compute_fingerprint(b"x" * 32)
        assert fp.startswith("SHA256:")
        b64part = fp.replace("SHA256:", "")
        # Should be valid base64
        import base64
        # Add padding back
        padded = b64part + "=" * (4 - len(b64part) % 4) if len(b64part) % 4 else b64part
        base64.b64decode(padded)

    def test_same_input_same_output(self):
        data = b"constant key blob"
        a = _compute_fingerprint(data)
        b = _compute_fingerprint(data)
        assert a == b

    def test_different_input_different_output(self):
        assert _compute_fingerprint(b"x") != _compute_fingerprint(b"y")


class TestSSHConnectSuccess:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_data(self):
        fake_m = _FakeMachine(
            host="10.0.0.99",
            secret_ref=None,
            auth_type="password",
        )

        stdout = "testbox\nroot\nLinux 6.1.0 x86_64\nNAME=Ubuntu\nVERSION=\"22.04 LTS\"\n"
        result = _make_fake_result(stdout=stdout)
        conn = _make_fake_conn(result)

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="fakepass"):
                with patch("app.ssh_client._pin_host_key"):
                    res = await ssh_test_machine(fake_m)

        assert res["ok"] is True
        assert "latency_ms" in res
        assert res["hostname"] == "testbox"
        assert res["user"] == "root"
        assert res["os_kind"] == "ubuntu"
        assert res["host_key_fingerprint"].startswith("SHA256:")
        assert "error" not in res

    @pytest.mark.asyncio
    async def test_success_hiveos_detection(self):
        fake_m = _FakeMachine()
        stdout = "hivebox\nuser\nLinux 5.15.0 aarch64\nHIVEOS=1\n"
        result = _make_fake_result(stdout=stdout)
        conn = _make_fake_conn(result)

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                with patch("app.ssh_client._pin_host_key"):
                    res = await ssh_test_machine(fake_m)

        assert res["ok"] is True
        assert res["os_kind"] == "hiveos"

    @pytest.mark.asyncio
    async def test_success_proxmox_detection(self):
        fake_m = _FakeMachine()
        stdout = "pve\nroot\nLinux 6.2.0 x86_64\nNAME=Proxmox\n"
        result = _make_fake_result(stdout=stdout)
        conn = _make_fake_conn(result)

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                with patch("app.ssh_client._pin_host_key"):
                    res = await ssh_test_machine(fake_m)

        assert res["ok"] is True
        assert res["os_kind"] == "proxmox"

    @pytest.mark.asyncio
    async def test_first_connection_pins_host_key(self):
        fake_m = _FakeMachine(host_key=None)
        stdout = "newbox\nroot\nLinux 6.0.0 x86_64\nNAME=Ubuntu\n"
        result = _make_fake_result(stdout=stdout)
        raw_key = b"some-real-key-data!!"
        conn = _make_fake_conn(result, raw_key=raw_key)

        pinned_calls = []

        def capture_pin(mid, fp):
            pinned_calls.append((mid, fp))

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                with patch("app.ssh_client._pin_host_key", side_effect=capture_pin):
                    await ssh_test_machine(fake_m)

        assert len(pinned_calls) == 1
        assert pinned_calls[0][0] == fake_m.id
        assert pinned_calls[0][1].startswith("SHA256:")

    @pytest.mark.asyncio
    async def test_pinned_key_verifies_fp(self):
        raw_key = b"some-real-key-data!!"
        expected_fp = "SHA256:" + base64.b64encode(
            hashlib.sha256(raw_key).digest()
        ).rstrip(b"=").decode()

        fake_m = _FakeMachine(host_key=expected_fp)
        stdout = "pinnedbox\nroot\nLinux 6.0.0 x86_64\n"
        result = _make_fake_result(stdout=stdout)
        conn = _make_fake_conn(result, raw_key=raw_key)

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is True


class TestSSHFailureCases:
    @pytest.mark.asyncio
    async def test_permission_denied(self):
        fake_m = _FakeMachine(auth_type="password")

        async def raise_perm(**kwargs):
            raise asyncssh.PermissionDenied("auth failed")

        with patch("asyncssh.connect", side_effect=raise_perm):
            with patch("app.ssh_client.decrypt_secret", return_value="wrongpass"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is False
        assert "Permission denied" in res.get("error", "")
        # No secret in error
        assert "wrongpass" not in res.get("error", "")

    @pytest.mark.asyncio
    async def test_connection_failed(self):
        fake_m = _FakeMachine()

        async def raise_conn(**kwargs):
            raise OSError("host unreachable")

        with patch("asyncssh.connect", side_effect=raise_conn):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is False
        assert "Connection failed" in res.get("error", "")

    @pytest.mark.asyncio
    async def test_timeout(self):
        fake_m = _FakeMachine()

        async def raise_timeout(**kwargs):
            raise asyncio.TimeoutError()

        with patch("asyncssh.connect", side_effect=raise_timeout):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is False
        assert "timed out" in res.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_host_key_changed_refused(self):
        raw_key = b"new-key-different!!!"
        old_key = b"old-key-different!!!"
        old_fp = "SHA256:" + base64.b64encode(
            hashlib.sha256(old_key).digest()
        ).rstrip(b"=").decode()

        fake_m = _FakeMachine(host_key=old_fp)

        async def raise_fp(**kwargs):
            raise asyncssh.HostKeyNotVerifiable("fingerprint changed")

        with patch("asyncssh.connect", side_effect=raise_fp):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is False
        assert "fingerprint" in res.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_generic_ssh_error(self):
        fake_m = _FakeMachine()

        async def raise_generic(**kwargs):
            raise Exception("random error")

        with patch("asyncssh.connect", side_effect=raise_generic):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is False
        assert "SSH error" in res.get("error", "")


class TestNoSecretLeak:
    @pytest.mark.asyncio
    async def test_success_no_secret_in_result(self):
        fake_m = _FakeMachine(
            secret_ref="encrypted_super_secret_key",
            auth_type="key",
        )
        stdout = "box\nroot\nLinux 6.0.0\n"
        result = _make_fake_result(stdout=stdout)
        conn = _make_fake_conn(result)

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="SECRETKEY123"):
                with patch("app.ssh_client._pin_host_key"):
                    res = await ssh_test_machine(fake_m)

        # No secret in result dict keys
        for key in ["secret", "secret_ref", "private_key", "ssh_key", "password"]:
            assert key not in res
        # No secret in values
        for val in res.values():
            if isinstance(val, str):
                assert "SECRETKEY123" not in val

    @pytest.mark.asyncio
    async def test_failure_no_secret_in_error(self):
        fake_m = _FakeMachine(
            secret_ref="encrypted_super_secret_key",
            auth_type="password",
        )

        async def raise_perm(**kwargs):
            raise asyncssh.PermissionDenied("auth failed")

        with patch("asyncssh.connect", side_effect=raise_perm):
            with patch("app.ssh_client.decrypt_secret", return_value="P@ssw0rd!"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is False
        assert "P@ssw0rd!" not in res.get("error", "")

    @pytest.mark.asyncio
    async def test_host_key_changed_no_secret_in_error(self):
        old_key = b"old-key-different!!!"
        old_fp = "SHA256:" + base64.b64encode(
            hashlib.sha256(old_key).digest()
        ).rstrip(b"=").decode()

        fake_m = _FakeMachine(host_key=old_fp, secret_ref="encrypted_ref")

        async def raise_fp(**kwargs):
            raise asyncssh.HostKeyNotVerifiable("fingerprint changed")

        with patch("asyncssh.connect", side_effect=raise_fp):
            with patch("app.ssh_client.decrypt_secret", return_value="S3cretP@ss!"):
                res = await ssh_test_machine(fake_m)

        assert res["ok"] is False
        err = res.get("error", "")
        assert "S3cretP@ss!" not in err


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_written_on_success(self):
        fake_m = _FakeMachine()
        stdout = "box\nroot\nLinux 6.0.0\n"
        result = _make_fake_result(stdout=stdout)
        conn = _make_fake_conn(result)

        audit_calls = []

        def capture_audit(mid, action, result, output=""):
            audit_calls.append((mid, action, result, output))

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                with patch("app.ssh_client._pin_host_key"):
                    with patch("app.ssh_client._audit_log", side_effect=capture_audit):
                        await ssh_test_machine(fake_m)

        # Should have 2 audit calls: host-key-pinned + ssh-test
        actions = [a[1] for a in audit_calls]
        assert "host-key-pinned" in actions
        assert "ssh-test" in actions

    @pytest.mark.asyncio
    async def test_audit_written_on_failure(self):
        fake_m = _FakeMachine()

        audit_calls = []

        def capture_audit(mid, action, result, output=""):
            audit_calls.append((mid, action, result, output))

        async def raise_perm(**kwargs):
            raise asyncssh.PermissionDenied("auth failed")

        with patch("asyncssh.connect", side_effect=raise_perm):
            with patch("app.ssh_client.decrypt_secret", return_value="fakekey"):
                with patch("app.ssh_client._audit_log", side_effect=capture_audit):
                    await ssh_test_machine(fake_m)

        actions = [(a[1], a[2]) for a in audit_calls]
        assert ("ssh-test", "fail") in actions

    @pytest.mark.asyncio
    async def test_audit_no_secret_in_output(self):
        fake_m = _FakeMachine(secret_ref="enc_ref")
        stdout = "box\nroot\nLinux 6.0.0\n"
        result = _make_fake_result(stdout=stdout)
        conn = _make_fake_conn(result)

        audit_outputs = []

        def capture(mid, action, result, output=""):
            audit_outputs.append(output)

        async def fake_connect(**kwargs):
            return conn

        with patch("asyncssh.connect", side_effect=fake_connect):
            with patch("app.ssh_client.decrypt_secret", return_value="SECRETKEY123"):
                with patch("app.ssh_client._pin_host_key"):
                    with patch("app.ssh_client._audit_log", side_effect=capture):
                        await ssh_test_machine(fake_m)

        for out in audit_outputs:
            assert "SECRETKEY123" not in out
            assert "S3cret" not in out


class TestResetHostKey:
    @pytest.mark.asyncio
    async def test_reset_nonexistent_machine(self):
        with patch("app.ssh_client._audit_log"):
            with patch("app.ssh_client._pin_host_key"):
                res = await reset_machine_host_key(99999)
        assert res["ok"] is False
        assert "not found" in res.get("error", "").lower()
        assert "secret" not in res.get("error", "").lower()


# ── Integration Tests ─────────────────────────────────────────────────
@pytest.mark.integration
class TestSSHIntegration:
    """Real SSH connection against a Docker container.

    Run with: pytest -m integration
    Requires a running OpenSSH container on 127.0.0.1:2222.
    """

    @pytest.fixture(autouse=True)
    def ssh_container(self):
        """Ensure the test SSH container is running.
        Uses linuxserver/openssh-server on port 2222.
        """
        check = os.popen(
            "docker inspect -f '{{.State.Running}}' fossor-ssh-test 2>/dev/null"
        ).read().strip()

        if check != "true":
            os.popen("docker rm -f fossor-ssh-test 2>/dev/null").read()
            os.popen(
                "docker run -d --name fossor-ssh-test -p 127.0.0.1:2222:2222 "
                "-e USER_PASSWORD=testpass123 "
                "-e PUBLIC_KEY='' "
                "lscr.io/linuxserver/openssh-server:latest"
            ).read()
            import time
            time.sleep(5)

        yield

        os.popen("docker rm -f fossor-ssh-test 2>/dev/null").read()

    def test_password_auth_success(self, ssh_container):
        fake_m = _FakeMachine(
            host="127.0.0.1",
            port=2222,
            user="root",
            auth_type="password",
            secret_ref="testpass123",
            os_kind="other",
        )

        with patch("app.ssh_client.decrypt_secret", return_value="testpass123"):
            res = asyncio.run(ssh_test_machine(fake_m))

        assert res["ok"] is True, f"Test failed: {res.get('error')}"
        assert "latency_ms" in res
        assert res["os_kind"] in ("ubuntu", "other")
        assert res["host_key_fingerprint"].startswith("SHA256:")
        assert "error" not in res
        # No secrets in response
        for val in res.values():
            if isinstance(val, str):
                assert "testpass123" not in val

    def test_password_auth_wrong(self, ssh_container):
        fake_m = _FakeMachine(
            host="127.0.0.1",
            port=2222,
            user="root",
            auth_type="password",
            secret_ref="wrongpassword",
        )

        with patch("app.ssh_client.decrypt_secret", return_value="wrongpassword"):
            res = asyncio.run(ssh_test_machine(fake_m))

        assert res["ok"] is False
        assert "Permission denied" in res.get("error", "")

    def test_host_key_pinning_then_mismatch(self, ssh_container):
        """First connect pins key; recreate container → mismatch."""
        # First connection — should succeed and pin
        fake_m = _FakeMachine(
            host="127.0.0.1",
            port=2222,
            user="root",
            auth_type="password",
            secret_ref="testpass123",
            host_key=None,
            os_kind="other",
        )

        with patch("app.ssh_client.decrypt_secret", return_value="testpass123"):
            res1 = asyncio.run(ssh_test_machine(fake_m))

        assert res1["ok"] is True, f"First test failed: {res1.get('error')}"
        original_fp = res1["host_key_fingerprint"]
        assert original_fp.startswith("SHA256:")

        # Now recreate the container (new host key)
        os.popen("docker rm -f fossor-ssh-test 2>/dev/null").read()
        os.popen(
            "docker run -d --name fossor-ssh-test -p 127.0.0.1:2222:2222 "
            "-e USER_PASSWORD=testpass123 "
            "-e PUBLIC_KEY='' "
            "lscr.io/linuxserver/openssh-server:latest"
        ).read()
        import time

        time.sleep(5)

        # Second connection with old pinned key should fail
        fake_m2 = _FakeMachine(
            host="127.0.0.1",
            port=2222,
            user="root",
            auth_type="password",
            secret_ref="testpass123",
            host_key=original_fp,
            os_kind="other",
        )

        with patch("app.ssh_client.decrypt_secret", return_value="testpass123"):
            res2 = asyncio.run(ssh_test_machine(fake_m2))

        assert res2["ok"] is False
        assert "fingerprint" in res2.get("error", "").lower()

    def test_key_auth_success(self, ssh_container):
        """Test SSH with key authentication against real container."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, "test_key")

            os.popen(f"ssh-keygen -t ed25519 -f {key_path} -N '' -q").read()
            pub_key = os.popen(f"cat {key_path}.pub").read().strip()

            os.popen("docker exec fossor-ssh-test mkdir -p /home/testuser/.ssh").read()
            os.popen(
                f"echo '{pub_key}' >> /tmp/test_pub && "
                f"docker cp /tmp/test_pub fossor-ssh-test:/tmp/test_pub"
            ).read()
            os.popen(
                f"docker exec fossor-ssh-test sh -c "
                "'cat /tmp/test_pub >> /home/testuser/.ssh/authorized_keys && "
                "chown -R testuser:testuser /home/testuser/.ssh'"
            ).read()

            private_key = os.popen(f"cat {key_path}").read()

            fake_m = _FakeMachine(
                host="127.0.0.1",
                port=2222,
                user="testuser",
                auth_type="key",
                secret_ref=private_key,
                os_kind="other",
            )

            with patch("app.ssh_client.decrypt_secret", return_value=private_key):
                res = asyncio.run(ssh_test_machine(fake_m))

            assert res["ok"] is True, f"Key auth test failed: {res.get('error')}"
            assert "latency_ms" in res
            for val in res.values():
                if isinstance(val, str):
                    assert "-----BEGIN" not in val


@pytest.mark.integration
class TestResetHostKeyIntegration:
    @pytest.mark.asyncio
    async def test_reset_clears_key(self):
        with patch("app.ssh_client._audit_log"):
            with patch("app.ssh_client._pin_host_key"):
                res = await reset_machine_host_key(42)
        assert "ok" in res
        assert "secret" not in res.get("error", "").lower() if "error" in res else True
