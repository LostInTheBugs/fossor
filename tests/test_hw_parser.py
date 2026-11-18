"""Unit tests for the pure hardware parser — frozen real outputs, no SSH.

Fixtures in ``tests/fixtures/detect_hw_*.txt`` are hand-written copies of what
``detect_hw`` prints on four kinds of machine in the fleet (HiveOS rig, Ubuntu
desktop with KVM, AMD box without nvidia-smi, small GPU-less VM). No test here
opens a connection.
"""

from pathlib import Path

import pytest

from app.hw_parser import SECTIONS, norm_bus, parse_detect_hw, split_sections
from app.ssh_client import WHITELISTED_COMMANDS

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestCommandShape:
    def test_command_has_every_section(self):
        command = WHITELISTED_COMMANDS["detect_hw"]
        for section in SECTIONS:
            assert f"'### {section}'" in command

    def test_command_is_read_only(self):
        command = WHITELISTED_COMMANDS["detect_hw"]
        for forbidden in ("sudo", "rm -", "> /", "mkdir", "systemctl", "nvidia-smi -r", "tee "):
            assert forbidden not in command

    def test_command_always_exits_zero(self):
        assert WHITELISTED_COMMANDS["detect_hw"].rstrip().endswith("exit 0")

    def test_fixtures_only_use_known_sections(self):
        for path in FIXTURES.glob("detect_hw_*.txt"):
            for line in path.read_text().splitlines():
                if line.startswith("### "):
                    assert line[4:] in SECTIONS, f"{path.name}: {line}"


class TestHiveosRig:
    """a. 4× RTX 3070 + 1× RTX 3080, i5-4570, no KVM."""

    @pytest.fixture(autouse=True)
    def _detect(self):
        self.d = parse_detect_hw(load("detect_hw_hiveos_rig.txt"))

    def test_os_kind(self):
        assert self.d["os_kind"] == "hiveos"

    def test_cpu_from_proc_cpuinfo(self):
        assert self.d["cpu"]["model"] == "Intel(R) Core(TM) i5-4570 CPU @ 3.20GHz"
        assert self.d["cpu"]["cores"] == 4
        assert self.d["cpu"]["threads"] == 4
        assert self.d["cpu"]["flags"] == "aes,avx2"

    def test_ram(self):
        assert self.d["cpu"]["ram_mb"] == 16000

    def test_five_gpus(self):
        assert len(self.d["gpus"]) == 5

    def test_gpu_details(self):
        first = self.d["gpus"][0]
        assert first["vendor"] == "NVIDIA"
        assert first["model"] == "NVIDIA GeForce RTX 3070"
        assert first["vram_mb"] == 8192
        assert first["pci_bus"] == "01:00.0"
        assert first["driver"] == "535.113.01"
        assert first["nv_uuid"].startswith("GPU-")
        last = self.d["gpus"][4]
        assert last["model"] == "NVIDIA GeForce RTX 3080"
        assert last["vram_mb"] == 10240
        assert last["pci_bus"] == "05:00.0"

    def test_lspci_does_not_duplicate_nvidia_cards(self):
        buses = [gpu["pci_bus"] for gpu in self.d["gpus"]]
        assert len(buses) == len(set(buses))
        assert sorted(buses) == ["01:00.0", "02:00.0", "03:00.0", "04:00.0", "05:00.0"]

    def test_no_virtualisation(self):
        assert self.d["virt"]["kvm"] is False and self.d["virt"]["vm_capable"] is False


class TestUbuntuKvm:
    """b. RTX 4080, KVM + virsh, AMD Ryzen."""

    @pytest.fixture(autouse=True)
    def _detect(self):
        self.d = parse_detect_hw(load("detect_hw_ubuntu_kvm.txt"))

    def test_os_kind(self):
        assert self.d["os_kind"] == "ubuntu"

    def test_cpu_from_lscpu_json(self):
        assert self.d["cpu"]["model"] == "AMD Ryzen 7 5800X 8-Core Processor"
        assert self.d["cpu"]["cores"] == 8
        assert self.d["cpu"]["threads"] == 16
        assert self.d["cpu"]["flags"] == "aes,avx2"
        assert self.d["cpu"]["ram_mb"] == 32000

    def test_one_gpu_with_uppercase_bus_id(self):
        assert len(self.d["gpus"]) == 1
        gpu = self.d["gpus"][0]
        assert gpu["model"] == "NVIDIA GeForce RTX 4080"
        assert gpu["vram_mb"] == 16376
        assert gpu["pci_bus"] == "0b:00.0"
        assert gpu["index"] == 0

    def test_virtualisation_detected(self):
        assert self.d["virt"]["kvm"] is True and self.d["virt"]["virsh"] is True
        assert self.d["virt"]["kvm_domain"] == 1 and self.d["virt"]["vm_capable"] is True


class TestAmdWithoutNvidiaSmi:
    """c. No nvidia-smi, an AMD card (and an Intel iGPU) seen by lspci."""

    @pytest.fixture(autouse=True)
    def _detect(self):
        self.d = parse_detect_hw(load("detect_hw_amd_lspci.txt"))

    def test_os_kind(self):
        assert self.d["os_kind"] == "other"

    def test_cpu(self):
        assert self.d["cpu"]["model"] == "AMD Ryzen 5 3600 6-Core Processor"
        assert self.d["cpu"]["cores"] == 6
        assert self.d["cpu"]["threads"] == 12

    def test_amd_card_from_lspci(self):
        amd = self.d["gpus"][0]
        assert amd["vendor"] == "AMD"
        assert amd["model"] == "Radeon RX 6700/6700 XT/6750 XT / 6800M"
        assert amd["pci_bus"] == "0a:00.0"
        assert amd["vram_mb"] is None
        assert amd["driver"] == ""
        assert amd["index"] == 0

    def test_intel_igpu_from_lspci(self):
        intel = self.d["gpus"][1]
        assert intel["vendor"] == "Intel"
        assert intel["model"] == "UHD Graphics 630"
        assert intel["pci_bus"] == "0b:00.0"
        assert intel["index"] == 1

    def test_no_virtualisation(self):
        assert self.d["virt"]["kvm"] is False


class TestNoGpuNoLspci:
    """d. No GPU at all, lspci absent."""

    @pytest.fixture(autouse=True)
    def _detect(self):
        self.d = parse_detect_hw(load("detect_hw_no_gpu.txt"))

    def test_os_kind(self):
        assert self.d["os_kind"] == "ubuntu"

    def test_cpu(self):
        assert self.d["cpu"]["model"] == "AMD EPYC 7542 32-Core Processor"
        assert self.d["cpu"]["cores"] == 4
        assert self.d["cpu"]["threads"] == 4
        assert self.d["cpu"]["ram_mb"] == 8000

    def test_no_gpu(self):
        assert self.d["gpus"] == []

    def test_no_virtualisation(self):
        assert self.d["virt"]["kvm"] is False and self.d["virt"]["vm_capable"] is False


class TestDegradedOutput:
    def test_empty_output_never_raises(self):
        d = parse_detect_hw("")
        assert d["cpu"] is None
        assert d["gpus"] == []
        assert d["os_kind"] == "other"
        assert d["virt"]["kvm"] is False and d["virt"]["vm_capable"] is False
        assert d["virt"]["kvm_domain"] == 0

    def test_sections_missing_from_output(self):
        d = parse_detect_hw("### MEM\nMem: 1048576000 0 1048576000\n")
        assert d["cpu"] is None
        assert d["gpus"] == []
        assert d["os_kind"] == "other"

    def test_split_sections_ignores_unknown_markers(self):
        sections = split_sections("### CPU\nx\n### NOPE\ny\n### MEM\nz\n")
        assert sections["CPU"] == "x"
        assert sections["MEM"] == "z"
        assert "NOPE" not in sections


class TestNormBus:
    @pytest.mark.parametrize("raw,expected", [
        ("00000000:01:00.0", "01:00.0"),
        ("0000:0B:00.0", "0b:00.0"),
        ("0b:00.0", "0b:00.0"),
        ("", ""),
        (None, ""),
    ])
    def test_norm_bus(self, raw, expected):
        assert norm_bus(raw) == expected
