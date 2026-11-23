"""Script enveloppe `fossor-gpu-pl` : entiers, bornes relues, aucune option libre."""

from __future__ import annotations

import os
import stat
import subprocess

from app import provision

FAKE_NVIDIA = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$*" in
  *power.min_limit*) echo "100.00, 250.00" ; exit 0 ;;
esac
if [ "$1" = "-i" ]; then
  echo "Power limit for GPU $2 was set to $4.00 W"
  exit 0
fi
exit 0
"""


def _harnais(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nvidia-smi"
    fake.write_text(FAKE_NVIDIA)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "fossor-gpu-pl"
    script.write_text(provision.GPU_PL_WRAPPER)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "nvidia.log"
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "FAKE_LOG": str(log),
    }
    return script, log, env


def _run(script, env, *args):
    return subprocess.run(["sh", str(script), *args], capture_output=True,
                          text=True, env=env, timeout=20)


def test_limite_valide_appliquee(tmp_path):
    script, log, env = _harnais(tmp_path)
    result = _run(script, env, "0", "150")
    assert result.returncode == 0, result.stderr
    calls = [line for line in log.read_text().splitlines() if " -pl " in line]
    assert calls == ["-i 0 -pl 150"]
    assert "/etc/sudoers.d/fossor-gpu-pl" in provision._INSTALL_GPU_WRAPPER
    assert "/usr/local/sbin/fossor-gpu-pl" in provision._INSTALL_GPU_WRAPPER


def test_au_dessus_du_maximum_refuse(tmp_path):
    script, log, env = _harnais(tmp_path)
    result = _run(script, env, "0", "999")
    assert result.returncode == 5
    assert "hors bornes" in result.stderr
    assert not log.exists() or not [
        line for line in log.read_text().splitlines() if " -pl " in line]


def test_sous_le_minimum_refuse(tmp_path):
    script, _log, env = _harnais(tmp_path)
    result = _run(script, env, "0", "50")
    assert result.returncode == 5


def test_option_injectee_dans_index_refusee(tmp_path):
    script, log, env = _harnais(tmp_path)
    result = _run(script, env, "0 -pl 999", "150")
    assert result.returncode == 2
    assert "index invalide" in result.stderr
    assert not log.exists() or not [
        line for line in log.read_text().splitlines() if " -pl " in line]


def test_option_injectee_dans_watts_refusee(tmp_path):
    script, log, env = _harnais(tmp_path)
    result = _run(script, env, "0", "150 -pm 1")
    assert result.returncode == 2
    assert "watts invalide" in result.stderr
    assert not log.exists() or not [
        line for line in log.read_text().splitlines() if " -pl " in line]
