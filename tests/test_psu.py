"""Alimentations (§4.2 bis), inventaire des composants et iGPU (§4.2)."""

import uuid as uuid_lib
from pathlib import Path

import pytest

from app.hw_parser import parse_gpus_pci
from app.inventory import (
    parse_dmidecode_memory,
    parse_hive_fans,
    parse_inventory,
    parse_lsblk,
    parse_lspci,
    parse_meminfo,
)
from app.psu import (
    available_w,
    compute_power,
    efficiency_pct,
    estimate_gpu_w,
    normalize_certification,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── Courbes et puissance disponible ──────────────────────────────────
def test_efficiency_gold_interpolation_and_measured_override():
    assert efficiency_pct("gold", 20) == 90.0
    assert efficiency_pct("gold", 50) == 92.0
    assert efficiency_pct("gold", 100) == 89.0
    assert efficiency_pct("gold", 35) == pytest.approx(91.0)
    assert efficiency_pct("gold", 10) == 90.0          # sous 20 %
    assert efficiency_pct("gold", 120) == 89.0         # au-delà de 100 %
    assert efficiency_pct("gold", 35, measured_efficiency=93.5) == 93.5


def test_available_power_derate_and_12v_limit():
    assert available_w(750) == 750.0
    assert available_w(750, derate_pct=90) == 675.0
    assert available_w(750, amps_12v=50) == 600.0     # 50 A × 12 = 600 W
    assert available_w(750, amps_12v=80, derate_pct=90) == 675.0


def test_certification_aliases_and_unknown():
    assert normalize_certification("80+ Standard") == "white"
    assert normalize_certification("aucune") == "none"
    with pytest.raises(ValueError):
        normalize_certification("plutonium")


def test_gpu_estimate():
    assert estimate_gpu_w("NVIDIA GeForce RTX 3070") == 220.0
    assert estimate_gpu_w("Radeon RX 6700 XT") == 175.0
    assert estimate_gpu_w("carte inconnue") == 150.0


# ── Calculs de charge ────────────────────────────────────────────────
def _psu(pid, rated, name=None, **kwargs):
    return {"id": pid, "name": name or f"PSU {pid}", "rated_w": rated,
            "certification": "gold", "amps_12v": None, "derate_pct": 100,
            "measured_efficiency": None, "is_default": pid == 1, **kwargs}


def test_single_psu_load_and_wall_power():
    result = compute_power(
        [_psu(1, 650)],
        [{"kind": "motherboard", "label": "Carte mère", "watts": 30, "quantity": 1}],
        [{"kind": "gpu", "label": "GPU 0", "model": "GeForce RTX 3070", "power_w": None,
          "psu_id": None, "enabled": True, "integrated": False}],
    )
    psu = result["psus"][0]
    assert psu["available_dc_w"] == 650.0
    assert psu["load_dc_w"] == 250.0
    assert psu["load_pct"] == pytest.approx(38.5)
    assert psu["efficiency_pct"] == pytest.approx(91.2)
    assert psu["wall_w"] == pytest.approx(274.0)
    assert psu["status"] == "ok"
    assert result["total_dc_w"] == 250.0
    assert result["total_wall_w"] == pytest.approx(274.0)
    assert result["gpus_without_psu"] == ["GPU 0"]


def test_two_psus_split_gpu_and_orange_threshold():
    result = compute_power(
        [_psu(1, 650), _psu(2, 170)],
        [{"kind": "motherboard", "label": "Carte mère", "watts": 30, "quantity": 1}],
        [{"kind": "gpu", "label": "GPU 0", "model": "GeForce RTX 3070", "power_w": None,
          "psu_id": 1, "psu2_id": 2, "split_w": 75, "enabled": True, "integrated": False}],
    )
    first, second = result["psus"]
    assert first["load_dc_w"] == 105.0          # 30 W plate-forme + 75 W slot
    assert second["load_dc_w"] == 145.0         # 220 - 75
    assert second["load_pct"] == pytest.approx(85.3)
    assert second["status"] == "orange"
    assert first["status"] == "ok"
    assert result["worst_load_pct"] == pytest.approx(85.3)
    assert result["worst_psu"] == "PSU 2"
    assert result["alerts"][0]["level"] == "orange"
    assert result["total_dc_w"] == 250.0
    assert result["wall_factor"] > 1.1


def test_derate_and_12v_limit_change_available_power():
    result = compute_power(
        [_psu(1, 750, amps_12v=50, derate_pct=100)],
        [{"kind": "pump", "label": "Pompe", "watts": 500, "quantity": 1, "measured": True}],
        [],
    )
    psu = result["psus"][0]
    assert psu["available_dc_w"] == 600.0
    assert psu["load_pct"] == pytest.approx(83.3)
    assert psu["status"] == "orange"


def test_thresholds_80_90_are_strict():
    at_80 = compute_power([_psu(1, 200)], [{"kind": "ram", "label": "RAM", "watts": 160, "quantity": 1}], [])
    at_82 = compute_power([_psu(1, 200)], [{"kind": "ram", "label": "RAM", "watts": 165, "quantity": 1}], [])
    at_95 = compute_power([_psu(1, 200)], [{"kind": "ram", "label": "RAM", "watts": 190, "quantity": 1}], [])
    assert at_80["psus"][0]["status"] == "ok"          # 80 % n'est pas « > 80 »
    assert at_82["psus"][0]["status"] == "orange"
    assert at_95["psus"][0]["status"] == "red"
    assert at_95["alerts"][0]["level"] == "red"


def test_integrated_gpu_never_counted():
    result = compute_power(
        [_psu(1, 500)],
        [],
        [{"kind": "gpu", "label": "iGPU", "model": "Radeon Vega", "power_w": 50,
          "psu_id": None, "enabled": True, "integrated": True}],
    )
    assert result["total_dc_w"] == 0.0
    assert result["gpus_without_psu"] == []


# ── Inventaire (parseurs) ────────────────────────────────────────────
DMIDECODE = """# dmidecode 3.3

Memory Device
\tSize: 8 GB
\tLocator: DIMM_A1
\tType: DDR4

Memory Device
\tSize: 8 GB
\tLocator: DIMM_B1
\tType: DDR4

Memory Device
\tSize: No Module Installed
\tLocator: DIMM_A2
"""

LSBLK = """NAME   ROTA TRAN SIZE MODEL
sda       1 sata 465.8G ST500DM002
nvme0n1   0 nvme 931.5G Samsung SSD 980
sdb       1 usb   14.9G Ultra Fit
loop0     1       55.6M snap
sr0       1 sata  1024M DVD
"""

LSPCI = """00:00.0 Host bridge: Intel Corporation 8th Gen Core Processor Host Bridge/DRAM Registers
00:01.0 PCI bridge: Intel Corporation Xeon E3-1200 v5/E3-1500 v5 PCI Express Root Port
00:14.0 USB controller: Intel Corporation 200 Series PCH USB 3.0 xHCI Controller
00:17.0 SATA controller: Intel Corporation 200 Series PCH SATA controller [AHCI mode]
00:1f.6 Ethernet controller: Intel Corporation Ethernet Connection (2) I219-V
01:00.0 VGA compatible controller: NVIDIA Corporation GA104 [GeForce RTX 3070]
02:00.0 Non-Volatile memory controller: Samsung Electronics Co Ltd NVMe SSD Controller
03:00.0 RAID bus controller: Broadcom / LSI MegaRAID SAS-3 3108
04:00.0 Audio device: Creative Labs CA0132 Sound Core3D
"""

HIVE_STAT = '{"fan": [55, 0, 48], "cputemp": [50]}'


def test_parse_dmidecode_memory_counts_only_installed():
    ram = parse_dmidecode_memory(DMIDECODE)
    assert len(ram) == 2
    assert all(row["kind"] == "ram" and row["watts"] == 3.0 for row in ram)
    assert ram[0]["label"] == "RAM DDR4 8 GB"


def test_parse_meminfo_fallback():
    ram = parse_meminfo("MemTotal:       16384000 kB\nMemFree: 100 kB\n")
    assert len(ram) == 1
    assert "16 Go" in ram[0]["label"]
    assert ram[0]["quantity"] == 2       # 2 barrettes de 8 Go estimées
    assert ram[0]["watts"] == 3.0


def test_parse_lsblk_kinds_and_skips():
    disks = parse_lsblk(LSBLK)
    labels = " ".join(row["label"] for row in disks)
    assert "HDD" in labels and "NVMe" in labels and "USB" in labels
    assert [row["watts"] for row in disks] == [8.0, 5.0, 1.0]   # loop et sr0 exclus


def test_parse_lspci_other_cards_only():
    cards = parse_lspci(LSPCI)
    labels = " ".join(row["label"] for row in cards)
    assert "Réseau" in labels
    assert "USB" in labels
    assert "HBA/RAID" in labels
    assert "Audio" in labels
    assert "GeForce" not in labels
    assert all(row["kind"] == "pcie_card" for row in cards)
    assert {row["watts"] for row in cards} >= {5.0, 12.0, 2.0}


def test_parse_hive_fans_and_full_inventory():
    assert parse_hive_fans(HIVE_STAT) == 2
    stdout = "\n".join([
        "### DMIDECODE", DMIDECODE, "### MEMINFO", "MemTotal: 16384000 kB",
        "### LSBLK", LSBLK, "### LSPCI", LSPCI, "### HIVE_STAT", HIVE_STAT,
    ])
    detected = parse_inventory(stdout)
    counts = detected["counts"]
    assert counts["ram"] == 2
    assert counts["storage"] == 3
    assert counts["cards"] == 5
    assert counts["fans"] == 2
    assert detected["ram_source"] == "dmidecode"
    assert detected["fan_source"] == "hiveos"


# ── iGPU : les trois cas réels ───────────────────────────────────────
def test_integrated_classification_three_prod_cases():
    gpus = parse_gpus_pci((FIXTURES / "lspci_igpu_prod.txt").read_text())
    hd4000, raven, cezanne, rtx = gpus
    assert "HD Graphics 4000" in hd4000["model"]
    assert hd4000["vendor"] == "Intel" and hd4000["integrated"] is True
    assert raven["vendor"] == "AMD" and raven["integrated"] is True
    assert "Radeon Vega" in raven["model"]
    assert cezanne["vendor"] == "AMD" and cezanne["integrated"] is True
    assert "Radeon Vega" in cezanne["model"]
    assert rtx["vendor"] == "NVIDIA" and rtx["integrated"] is False


# ── API : PSU, composants, affectation, export ───────────────────────
MACHINE = {
    "name": "psu-api", "host": "192.0.2.77", "ssh_port": 22, "ssh_user": "root",
    "auth_type": "password", "secret": "secret", "usage": "both", "enabled": True,
}


async def _create_machine(client) -> int:
    response = await client.post("/api/machines", json=MACHINE)
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _add_device(machine_id: int, **overrides) -> int:
    from app.database import SessionLocal
    from app.models import Device

    values = {
        "kind": "gpu", "index": 0, "vendor": "NVIDIA",
        "model": "NVIDIA GeForce RTX 3070", "present": True,
        "enabled_for_mining": True, "integrated": False,
    }
    values.update(overrides)
    db = SessionLocal()
    try:
        device = Device(uuid=str(uuid_lib.uuid4()), machine_id=machine_id, **values)
        db.add(device)
        db.commit()
        db.refresh(device)
        return device.id
    finally:
        db.close()


async def test_psu_api_split_gpu_components_and_export(client):
    machine_id = await _create_machine(client)
    first = await client.post(f"/api/machines/{machine_id}/psus", json={
        "name": "PSU 1", "rated_w": 650, "certification": "gold",
    })
    assert first.status_code == 201, first.text
    second = await client.post(f"/api/machines/{machine_id}/psus", json={
        "name": "PSU 2", "rated_w": 170, "certification": "gold",
    })
    assert second.status_code == 201, second.text
    assert first.json()["is_default"] is True and second.json()["is_default"] is False

    device_id = _add_device(machine_id)
    power = (await client.get(f"/api/machines/{machine_id}/power")).json()
    assert len(power["psus"]) == 2
    assert power["gpus_without_psu"] == ["GPU 0 — NVIDIA GeForce RTX 3070"]

    assigned = await client.put(f"/api/devices/{device_id}/psu", json={
        "psu_id": first.json()["id"], "psu2_id": second.json()["id"], "split_w": 75,
    })
    assert assigned.status_code == 200, assigned.text
    power = assigned.json()["power"]
    assert power["psus"][0]["load_dc_w"] == 109.0     # 34 W de défauts + 75 W slot
    assert power["psus"][1]["load_dc_w"] == 145.0
    assert power["psus"][1]["status"] == "orange"
    assert power["psus"][1]["load_pct"] == pytest.approx(85.3)
    assert power["gpus_without_psu"] == []

    components = (await client.get(f"/api/machines/{machine_id}/components")).json()
    kinds = {row["kind"] for row in components}
    assert {"motherboard", "fan", "riser", "pump"} <= kinds
    added = await client.post(f"/api/machines/{machine_id}/components", json={
        "kind": "fan", "label": "Ventilateur boîtier", "watts": 5, "quantity": 2,
    })
    assert added.status_code == 201, added.text
    patched = await client.patch(f"/api/components/{added.json()['id']}", json={"quantity": 3})
    assert patched.json()["quantity"] == 3

    export = (await client.get("/api/config/export")).json()
    assert len(export["psus"]) == 2
    assert any(row["label"] == "Ventilateur boîtier" for row in export["components"])
    assignment = next(iter(export["device_psus"].values()))
    assert assignment["psu_uuid"] == first.json()["uuid"]
    assert assignment["psu2_uuid"] == second.json()["uuid"]

    from app.config_io import validate_document

    validate_document(export)   # sections optionnelles acceptées


async def test_integrated_gpu_is_masked_by_default(client):
    machine_id = await _create_machine(client)
    device_id = _add_device(machine_id, model="Intel HD Graphics 4000", integrated=True, enabled_for_mining=False)
    refused = await client.patch(f"/api/devices/{device_id}", json={"enabled_for_mining": True})
    assert refused.status_code == 400
    assert "intégré" in refused.json()["detail"]
    devices = (await client.get(f"/api/machines/{machine_id}/devices")).json()
    assert devices[0]["integrated"] is True and devices[0]["enabled_for_mining"] is False
    view = (await client.get(f"/api/machines/{machine_id}/miner")).json()
    assert view["gpus"] == []
    settings = (await client.put("/api/power/settings", json={"hide_integrated_gpus": False})).json()
    assert settings["hide_integrated_gpus"] is False
