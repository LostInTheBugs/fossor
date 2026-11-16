"""Integration tests: ``collect_stats`` against the disposable SSH container.

Never a real rig, never .195: the server is the throw-away
``lscr.io/linuxserver/openssh-server`` of the test network.  The fake HiveOS
files are dropped into it by the caller (``docker exec``); without them the
machine simply looks like a CPU-only Ubuntu box.
"""

import os
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

HIVE_GPUS = ["01:00.0", "02:00.0", "04:00.0", "05:00.0", "06:00.0"]


def disposable_machine() -> SimpleNamespace:
    from app.secrets import encrypt_secret

    return SimpleNamespace(
        id=None,
        name="ssh-jetable",
        host=os.environ["FOSSOR_TEST_SSH_HOST"],
        ssh_port=int(os.environ.get("FOSSOR_TEST_SSH_PORT", "2222")),
        ssh_user=os.environ.get("FOSSOR_TEST_SSH_USER", "tester"),
        auth_type="password",
        secret_ref=encrypt_secret(os.environ.get("FOSSOR_TEST_SSH_PASSWORD", "testerpass")),
        host_key=None,
        host_key_public=None,
    )


async def _snapshot():
    if not os.environ.get("FOSSOR_TEST_SSH_HOST"):
        pytest.skip("FOSSOR_TEST_SSH_HOST absent: no disposable SSH server")
    from app.ssh_client import ssh_collect_stats

    return await ssh_collect_stats(disposable_machine())


async def test_collect_stats_on_plain_container_has_no_error():
    if os.environ.get("FOSSOR_TEST_HIVE_FAKE") == "1":
        pytest.skip("fake HiveOS files are installed: the container is no longer plain")
    raw = await _snapshot()
    assert raw["ok"] is True, raw.get("error")
    for section in ("HIVE_STAT", "HIVE_GPU", "NVSMI", "PS_MINERS", "UPTIME"):
        assert f"### {section}" in raw["stdout"]

    from app.stats_parser import parse_collect_stats

    stats = parse_collect_stats(raw["stdout"])
    # Alpine container: no HiveOS dump, no nvidia-smi, no miner → CPU only.
    assert stats["devices"] == []
    assert stats["api_port"] is None
    assert stats["uptime_s"] is not None and stats["uptime_s"] > 0


async def test_collect_stats_reads_fake_hiveos_and_lists_five_gpus():
    if os.environ.get("FOSSOR_TEST_HIVE_FAKE") != "1":
        pytest.skip("fake /run/hive files not installed in the disposable server")

    raw = await _snapshot()
    assert raw["ok"] is True, raw.get("error")

    from app.stats_parser import parse_collect_stats

    stats = parse_collect_stats(raw["stdout"])
    gpus = [d for d in stats["devices"] if d["kind"] == "gpu"]
    assert len(gpus) == 5
    assert [g["pci_bus"] for g in gpus] == HIVE_GPUS
    assert stats["miner"] == "srbminer"
    assert stats["coin"] == "PEARL"
    # No miner process runs in this container: api_port is legitimately absent,
    # its discovery is unit-tested against the fixture's ps output.
    assert stats["api_port"] is None
    assert stats["total_hashrate_hs"] == pytest.approx(3.28947e14)
    # Secrets present in the raw dump never survive parsing.
    assert "S3CR3T-HIVE-PASS" not in str(stats)
