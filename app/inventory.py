"""Inventaire des « autres composants » d'une machine (§4.2 bis).

Détection **en lecture seule** : ``dmidecode -t memory`` (barrettes), ``/proc/meminfo``
en repli, ``lsblk`` (disques), ``lspci`` (cartes hors GPU) et ``fan`` de HiveOS
(ventilateurs).  Parseurs purs, testés sur des sorties figées ; une commande
absente laisse sa section vide et n'est jamais une erreur.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Consommation par composant (§4.2 bis), toutes modifiables ensuite.
RAM_W = {"DDR5": 4.0, "DDR4": 3.0, "DDR3": 2.5, "DDR2": 2.5}
RAM_DEFAULT_W = 3.0
DISK_W = {"hdd": 8.0, "nvme": 5.0, "usb": 1.0, "ssd": 3.0}
CARD_W = {
    "ethernet": 5.0, "10-gigabit": 10.0, "network": 5.0, "wireless": 3.0,
    "raid": 12.0, "sas": 12.0, "sata": 5.0, "usb": 5.0, "audio": 2.0,
}
CARD_DEFAULT_W = 5.0
FAN_W = 2.0
MOTHERBOARD_W = 30.0
RISER_W = 3.0
PUMP_W = 10.0

_SKIP_CLASSES = (
    "vga compatible", "3d controller", "display controller",
    "host bridge", "isa bridge", "pci bridge", "smbus", "communication controller",
    "signal processing", "performance counters", "system peripheral",
    "non-volatile memory", "serial bus", "generic system peripheral",
)


def split_sections(stdout: str) -> dict[str, str]:
    """Split ``### NAME`` sections (names are free-form here)."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in (stdout or "").splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _size_gb(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|TB)", text or "", re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    if unit == "MB":
        return value / 1024.0
    if unit == "TB":
        return value * 1024.0
    return value


def parse_dmidecode_memory(text: str) -> list[dict[str, Any]]:
    """One component per installed DIMM (``Size``/``Type`` of ``dmidecode -t memory``)."""
    components: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text or ""):
        if "Memory Device" not in block and "Size:" not in block:
            continue
        size_match = re.search(r"^\s*Size:\s*(.+)$", block, re.MULTILINE)
        if not size_match:
            continue
        size_text = size_match.group(1).strip()
        if "no module" in size_text.lower() or "not installed" in size_text.lower():
            continue
        if size_text.lower().startswith("unknown") or not _size_gb(size_text):
            continue
        type_match = re.search(r"^\s*Type:\s*(.+)$", block, re.MULTILINE)
        kind = (type_match.group(1).strip().upper() if type_match else "") or "RAM"
        watts = RAM_W.get(kind, RAM_DEFAULT_W)
        components.append({
            "kind": "ram",
            "label": f"RAM {kind} {size_text}",
            "watts": watts,
            "quantity": 1,
            "source": "detected",
            "detail": size_text,
        })
    return components


def parse_meminfo(text: str) -> list[dict[str, Any]]:
    """Fallback: one estimated row, 3 W per 8 Go of ``MemTotal``."""
    match = re.search(r"^MemTotal:\s*(\d+)\s*kB", text or "", re.MULTILINE)
    if not match:
        return []
    gb = int(match.group(1)) / (1024.0 * 1024.0)
    if gb <= 0:
        return []
    bars = max(1, int(round(gb / 8.0)))
    return [{
        "kind": "ram",
        "label": f"RAM (estimée) {gb:.0f} Go",
        "watts": RAM_DEFAULT_W,
        "quantity": bars,
        "source": "detected",
        "detail": f"MemTotal {gb:.1f} Go ({bars} barrette(s) estimée(s))",
    }]


def parse_lsblk(text: str) -> list[dict[str, Any]]:
    """Disques from ``lsblk -d -o NAME,ROTA,TRAN,SIZE,MODEL`` (header ignored)."""
    components: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line.strip() or line.strip().upper().startswith("NAME"):
            continue
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        name, rota, tran, size = parts[0], parts[1], parts[2], parts[3]
        model = parts[5].strip() if len(parts) > 5 else ""
        if name.startswith(("loop", "zram")) or name.startswith("sr"):
            continue
        tran_low = (tran or "").lower()
        if tran_low == "usb":
            family, watts = "USB", DISK_W["usb"]
        elif tran_low == "nvme":
            family, watts = "NVMe", DISK_W["nvme"]
        elif rota == "1":
            family, watts = "HDD", DISK_W["hdd"]
        else:
            family, watts = "SSD", DISK_W["ssd"]
        components.append({
            "kind": "storage",
            "label": f"{family} {size} {model}".strip(),
            "watts": watts,
            "quantity": 1,
            "source": "detected",
            "detail": f"{name} {tran or '?'} {size}",
        })
    return components


def parse_lspci(text: str) -> list[dict[str, Any]]:
    """Autres cartes PCIe (réseau, HBA/RAID, USB…), GPU et ponts exclus."""
    components: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        match = re.match(
            r"^[0-9a-fA-F]{2,4}:[0-9a-fA-F]{2}\.[0-9a-fA-F]\s+([^:]+):\s*(.+)$",
            line.strip(),
        )
        if not match:
            continue
        device_class = match.group(1).strip()
        description = match.group(2).strip()
        low = device_class.lower()
        if any(token in low for token in _SKIP_CLASSES):
            continue
        if "ethernet" in low:
            watts = CARD_W["10-gigabit"] if "10-gigabit" in description.lower() else CARD_W["ethernet"]
            kind = "Réseau"
        elif "network" in low:
            watts, kind = CARD_W["network"], "Réseau"
        elif "wireless" in low:
            watts, kind = CARD_W["wireless"], "Wi-Fi"
        elif "raid" in low or "sas" in low:
            watts, kind = CARD_W["raid"], "HBA/RAID"
        elif "sata" in low:
            watts, kind = CARD_W["sata"], "SATA"
        elif "usb" in low:
            watts, kind = CARD_W["usb"], "USB"
        elif "audio" in low:
            watts, kind = CARD_W["audio"], "Audio"
        else:
            watts, kind = CARD_DEFAULT_W, device_class
        components.append({
            "kind": "pcie_card",
            "label": f"{kind} — {description}"[:120],
            "watts": watts,
            "quantity": 1,
            "source": "detected",
            "detail": device_class,
        })
    return components


def parse_hive_fans(text: str) -> int | None:
    """Number of spinning fans in HiveOS ``last_stat.json`` (``fan`` list)."""
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    fans = data.get("fan") if isinstance(data, dict) else None
    if not isinstance(fans, list):
        return None
    return sum(1 for value in fans if isinstance(value, (int, float)) and value > 0)


def default_components(fan_count: int = 2) -> list[dict[str, Any]]:
    """Spec defaults, all editable: board 30 W, fans 2 W each, risers, pump."""
    rows = [
        {"kind": "motherboard", "label": "Carte mère / chipset", "watts": MOTHERBOARD_W,
         "quantity": 1, "source": "default"},
        {"kind": "fan", "label": "Ventilateurs", "watts": FAN_W,
         "quantity": max(0, int(fan_count)), "source": "default"},
        {"kind": "riser", "label": "Risers alimentés", "watts": RISER_W,
         "quantity": 0, "source": "default"},
        {"kind": "pump", "label": "Pompe / AIO", "watts": PUMP_W,
         "quantity": 0, "source": "default"},
    ]
    return rows


def parse_inventory(stdout: str) -> dict[str, Any]:
    """All detected components of an ``inventory`` command output."""
    sections = split_sections(stdout)
    ram = parse_dmidecode_memory(sections.get("DMIDECODE", ""))
    ram_source = "dmidecode" if ram else ""
    if not ram:
        ram = parse_meminfo(sections.get("MEMINFO", ""))
        ram_source = "meminfo" if ram else ""
    storage = parse_lsblk(sections.get("LSBLK", ""))
    cards = parse_lspci(sections.get("LSPCI", ""))
    fans = parse_hive_fans(sections.get("HIVE_STAT", ""))
    components = ram + storage + cards
    if fans:
        components.append({
            "kind": "fan", "label": "Ventilateurs (HiveOS)", "watts": FAN_W,
            "quantity": fans, "source": "detected", "detail": f"fan HiveOS ×{fans}",
        })
    return {
        "components": components,
        "ram_source": ram_source,
        "fan_source": "hiveos" if fans else "",
        "counts": {"ram": len(ram), "storage": len(storage), "cards": len(cards), "fans": fans or 0},
    }
