"""Pure parser for the ``detect_hw`` whitelisted SSH command.

No SSH, no database, no I/O: ``parse_detect_hw(stdout)`` turns the sectioned
text produced by :data:`app.ssh_client.WHITELISTED_COMMANDS`["detect_hw"] into
a plain dictionary. Every missing tool leaves its section empty, so the parser
must never raise on partial output — an absent tool is not an error.
"""

from __future__ import annotations

import json
import re
from typing import Any

SECTION_MARK = "### "
SECTIONS = ("CPU", "MEM", "GPU_NVIDIA", "GPU_COMPUTE", "PS_MINERS", "GPU_PCI", "VIRT", "OS")

# Flags the spec cares about (§4.2), in display order.
USEFUL_FLAGS = ("aes", "avx2", "avx512f")

_PCI_LINE = re.compile(
    r"^(?P<bus>[0-9a-fA-F]{2,4}:[0-9a-fA-F]{2}\.[0-9a-fA-F])\s+(?P<rest>.+)$"
)
_REV = re.compile(r"\s*\(rev [^)]*\)\s*$")
_PCI_ID = re.compile(r"\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]\s*$")
_BRACKET = re.compile(r"\[([^\[\]]+)\]")

VENDOR_PREFIXES = (
    "Advanced Micro Devices, Inc. [AMD/ATI]",
    "Advanced Micro Devices, Inc. [AMD]",
    "Advanced Micro Devices, Inc.",
    "NVIDIA Corporation",
    "Intel Corporation",
    "ASPEED Technology, Inc.",
    "Matrox Electronics Systems Ltd.",
    "ASUSTeK Computer Inc.",
)


# ── Helpers ───────────────────────────────────────────────────────────
def split_sections(stdout: str) -> dict[str, str]:
    """Split the sectioned output into ``{SECTION: text}`` (missing == "")."""
    sections: dict[str, list[str]] = {name: [] for name in SECTIONS}
    current: str | None = None
    for line in (stdout or "").splitlines():
        if line.startswith(SECTION_MARK):
            name = line[len(SECTION_MARK):].strip()
            current = name if name in sections else None
            continue
        if current:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def norm_bus(bus: str | None) -> str:
    """Normalise a PCI bus id to ``DDDD:BB:DD.F`` -> ``BB:DD.F``.

    ``nvidia-smi`` reports ``00000000:01:00.0`` while ``lspci`` reports
    ``01:00.0``; both reduce to the same key so a card seen by both tools is
    matched once.
    """
    if not bus:
        return ""
    value = bus.strip().lower()
    match = re.match(r"^(?:[0-9a-f]{4,8}:)?([0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])$", value)
    return match.group(1) if match else value


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _useful_flags(raw: str) -> str:
    """Keep only the flags the spec lists (aes, avx2, avx512f)."""
    present = {part.lower() for part in (raw or "").replace(",", " ").split()}
    kept = [flag for flag in USEFUL_FLAGS if flag in present]
    return ",".join(kept)


def os_kind_from_text(text: str) -> str:
    """hiveos / ubuntu / proxmox / other from os-release + HiveOS marker."""
    os_kind = "other"
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped == "HIVEOS=1":
            os_kind = "hiveos"
        elif stripped.startswith("NAME=") and os_kind != "hiveos":
            low = stripped.lower()
            if "ubuntu" in low:
                os_kind = "ubuntu"
            elif "proxmox" in low:
                os_kind = "proxmox"
    return os_kind


# ── CPU ───────────────────────────────────────────────────────────────
def _parse_lscpu_json(blob: str) -> dict[str, str] | None:
    start = blob.find("{")
    end = blob.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(blob[start:end + 1])
    except (ValueError, TypeError):
        return None
    rows = data.get("lscpu") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    fields: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict) and "field" in row and "data" in row:
            fields[str(row["field"]).rstrip(":")] = str(row["data"])
    return fields or None


def _parse_cpuinfo(blob: str) -> tuple[dict[str, str], int]:
    """Return (first processor block, number of processors) from /proc/cpuinfo."""
    first: dict[str, str] = {}
    processors = 0
    core_keys: set[tuple[str, str]] = set()
    block: dict[str, str] = {}

    def close_block() -> None:
        nonlocal first
        if not block:
            return
        if not first:
            first = dict(block)
        physical = block.get("physical id", "0")
        core = block.get("core id")
        if core is not None:
            core_keys.add((physical, core))

    for line in (blob or "").splitlines():
        if not line.strip():
            close_block()
            block = {}
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        block[key] = value
        if key == "processor":
            processors += 1
    close_block()
    if core_keys:
        first["_unique_cores"] = str(len(core_keys))
    return first, processors


def parse_cpu(section: str) -> dict[str, Any] | None:
    """Model / cores / threads / flags from the CPU section (lscpu or cpuinfo)."""
    if not section.strip():
        return None

    model = ""
    cores = None
    threads = None
    flags = ""
    nproc = None
    trailing: list[str] = []

    fields = _parse_lscpu_json(section)
    if fields is None:
        # /proc/cpuinfo fallback (old lscpu without -J, or lscpu missing).
        # Any line that is a bare integer (nproc) or a flags line is not cpuinfo.
        cpuinfo_lines = []
        for line in section.splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                nproc = int(stripped)
            elif stripped.lower().startswith("flags") and ":" in stripped:
                if not flags:
                    flags = stripped.split(":", 1)[1].strip()
            else:
                cpuinfo_lines.append(line)
        info, processors = _parse_cpuinfo("\n".join(cpuinfo_lines))
        model = info.get("model name", "") or info.get("Processor", "")
        threads = processors or None
        per_socket = _to_int(info.get("cpu cores"))
        unique_cores = _to_int(info.get("_unique_cores"))
        if per_socket is not None:
            sockets = len({
                ln.split(":", 1)[1].strip()
                for ln in cpuinfo_lines
                if ln.strip().lower().startswith("physical id")
            }) or 1
            cores = per_socket * sockets
        elif unique_cores:
            cores = unique_cores
        if not model:
            return None
    else:
        for line in section.splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                nproc = int(stripped)
            elif stripped.lower().startswith("flags") and ":" in stripped and not fields.get("Flags"):
                trailing.append(stripped)
        model = fields.get("Model name", "") or fields.get("Vendor ID", "")
        threads = _to_int(fields.get("CPU(s)"))
        per_socket = _to_int(fields.get("Core(s) per socket"))
        sockets = _to_int(fields.get("Socket(s)")) or 1
        cores = per_socket * sockets if per_socket else None
        if not fields.get("Flags"):
            for line in trailing:
                flags = line.split(":", 1)[1].strip()
                break
        else:
            flags = fields.get("Flags", "")

    if nproc and not threads:
        threads = nproc
    if threads and not cores:
        cores = threads
    return {
        "model": model.strip(),
        "cores": cores,
        "threads": threads,
        "flags": _useful_flags(flags),
    }


def parse_mem(section: str) -> int | None:
    """Total RAM in MiB from ``free -b``."""
    for line in (section or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(":").lower() == "mem":
            total = _to_int(parts[1])
            if total:
                return int(total // (1024 * 1024))
    return None


# ── GPU ───────────────────────────────────────────────────────────────
def _vendor_of(text: str) -> str:
    """Vendor from an lspci description ("Corporation" contains "ati" — order matters)."""
    low = text.lower()
    if "nvidia" in low:
        return "NVIDIA"
    if "advanced micro devices" in low or "[amd" in low:
        return "AMD"
    if "intel" in low:
        return "Intel"
    if re.search(r"\bati\b", low):
        return "AMD"
    if "aspeed" in low:
        return "ASPEED"
    if "matrox" in low:
        return "Matrox"
    return ""


_INTEGRATED_INTEL = re.compile(r"\b(uhd|iris)\b|\bhd graphics\b", re.IGNORECASE)
_INTEGRATED_AMD = re.compile(r"\bradeon\s+(graphics|vega)\b", re.IGNORECASE)
_DISCRETE_AMD = re.compile(r"\brx\b|\bnavi\b|\bvii\b", re.IGNORECASE)


def is_integrated(vendor: str, model: str) -> bool:
    """Integrated GPU? Intel UHD/Iris, AMD APU Radeon Graphics, ASPEED/Matrox BMC.

    Intel Arc, AMD RX/Navi/VII and every NVIDIA card are discrete.
    """
    vendor_low = (vendor or "").lower()
    text = model or ""
    if vendor_low == "intel":
        return bool(_INTEGRATED_INTEL.search(text))
    if vendor_low == "amd":
        return bool(_INTEGRATED_AMD.search(text)) and not _DISCRETE_AMD.search(text)
    if vendor_low in ("aspeed", "matrox"):
        return True
    return False


def _clean_pci_model(text: str) -> str:
    """'NVIDIA Corporation GA104 [GeForce RTX 3070] [10de:2484]' -> 'GeForce RTX 3070'."""
    value = _REV.sub("", text.strip())
    value = _PCI_ID.sub("", value).strip()
    for prefix in VENDOR_PREFIXES:
        if value.lower().startswith(prefix.lower()):
            value = value[len(prefix):].strip(" ,;")
            break
    brackets = _BRACKET.findall(value)
    if brackets:
        return brackets[-1].strip()
    return value


def parse_gpus_nvidia(section: str) -> list[dict[str, Any]]:
    """CSV rows from nvidia-smi: index,name,memory.total,pci.bus_id,driver_version,uuid."""
    gpus: list[dict[str, Any]] = []
    for line in (section or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("index"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        index = _to_int(parts[0])
        gpus.append({
            "index": index,
            "vendor": "NVIDIA",
            "model": parts[1],
            "vram_mb": _to_int(parts[2]) if len(parts) > 2 else None,
            "pci_bus": norm_bus(parts[3]) if len(parts) > 3 else "",
            "driver": parts[4] if len(parts) > 4 else "",
            "nv_uuid": parts[5] if len(parts) > 5 else "",
            "integrated": False,
        })
    return gpus


def parse_gpus_pci(section: str) -> list[dict[str, Any]]:
    """VGA/3D/display lines from ``lspci -nn``."""
    gpus: list[dict[str, Any]] = []
    for line in (section or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _PCI_LINE.match(line)
        if not match:
            continue
        rest = match.group("rest")
        if ":" in rest:
            device_class, body = rest.split(":", 1)
            if not re.search(r"vga|3d|display", device_class, re.IGNORECASE):
                continue
        else:
            body = rest
        body = body.strip()
        vendor = _vendor_of(body)
        model = _clean_pci_model(body)
        gpus.append({
            "index": None,
            "vendor": vendor,
            "model": model,
            "vram_mb": None,
            "pci_bus": norm_bus(match.group("bus")),
            "driver": "",
            "nv_uuid": "",
            "integrated": is_integrated(vendor, model),
        })
    return gpus


# ── GPU Compute Apps (external miner detection) ───────────────────────
# Format: PID,Process Name,Used Memory (MiB) — or bare CSV without header.
def parse_gpu_compute(section: str, ps_section: str = "") -> list[dict[str, Any]]:
    """External miners from ``nvidia-smi --query-compute-apps`` + ``PS_MINERS``.

    Thin wrapper around :mod:`app.miners`, which owns the miner list, the
    password masking and the wallet truncation.
    """
    from app.external_miners import detect_external_miners

    return detect_external_miners(section, ps_section)


def parse_virt(section: str) -> dict[str, Any]:
    """Drapeaux de virtualisation.

    ``vm_capable`` exige **``/dev/kvm``** : une sonde qui ne voit que ``qemu``
    dans ``virsh capabilities`` (machine sans virtualisation imbriquée) ne peut
    pas héberger de VM minière — ``--cpu max`` y échoue. Proxmox garde sa
    branche ``pveversion``, mais toujours avec ``/dev/kvm``.
    """
    flags: dict[str, Any] = {"kvm": False, "virsh": False, "pveversion": False, "kvm_domain": 0}
    for line in (section or "").splitlines():
        stripped = line.strip()
        if stripped == "KVM=1":
            flags["kvm"] = True
        elif stripped == "VIRSH=1":
            flags["virsh"] = True
        elif stripped == "PVERSION=1":
            flags["pveversion"] = True
        elif stripped.startswith("KVM_DOMAIN="):
            try:
                flags["kvm_domain"] = int(stripped.split("=", 1)[1] or 0)
            except ValueError:
                flags["kvm_domain"] = 0
    flags["libvirt_kvm"] = bool(flags["kvm"] and flags["kvm_domain"] > 0)
    flags["vm_capable"] = bool(flags["kvm"] and (flags["kvm_domain"] > 0 or flags["pveversion"]))
    return flags


def parse_gpu_power_limits(text: str) -> dict[int, dict[str, float]]:
    """Bornes de power limit par GPU : ``index,min,max,default`` (W, CSV)."""
    out: dict[int, dict[str, float]] = {}
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            index = int(parts[0])
            values = [float(part) for part in parts[1:4]]
        except ValueError:
            continue
        if any(value <= 0 for value in values):
            continue  # « N/A » sur les cartes sans capteur : borne inconnue
        out[index] = {"min": values[0], "max": values[1], "default": values[2]}
    return out


# ── Entry point ───────────────────────────────────────────────────────
def parse_detect_hw(stdout: str) -> dict[str, Any]:
    """Parse a full ``detect_hw`` output into machines/devices data."""
    sections = split_sections(stdout)
    cpu = parse_cpu(sections["CPU"])
    ram_mb = parse_mem(sections["MEM"])
    if cpu is not None:
        cpu["ram_mb"] = ram_mb

    gpus = parse_gpus_nvidia(sections["GPU_NVIDIA"])
    seen = {gpu["pci_bus"] for gpu in gpus if gpu["pci_bus"]}
    for gpu in parse_gpus_pci(sections["GPU_PCI"]):
        if gpu["pci_bus"] and gpu["pci_bus"] in seen:
            continue  # already described by nvidia-smi
        seen.add(gpu["pci_bus"])
        gpus.append(gpu)
    for position, gpu in enumerate(gpus):
        if gpu["index"] is None:
            gpu["index"] = position

    return {
        "os_kind": os_kind_from_text(sections["OS"]),
        "cpu": cpu,
        "gpus": gpus,
        "gpu_compute": parse_gpu_compute(sections["GPU_COMPUTE"], sections["PS_MINERS"]),
        "virt": parse_virt(sections["VIRT"]),
    }
