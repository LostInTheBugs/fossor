"""Integrated GPUs (§4.2): never proposed for mining, flagged ``integrated``."""

import uuid as uuid_lib
from pathlib import Path

import pytest

from app.hw_parser import is_integrated, parse_detect_hw, parse_gpus_pci

FIXTURES = Path(__file__).parent / "fixtures"


def detect(name: str) -> dict:
    return parse_detect_hw((FIXTURES / name).read_text())


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def machine(db):
    from app.models import Machine

    row = Machine(name="igpu-test", host="192.0.2.50", ssh_port=22, ssh_user="root")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_fixture_amd_flags_intel_uhd_630():
    """(b) The Intel UHD 630 of the AMD fixture is an iGPU, the RX 6700 is not."""
    gpus = detect("detect_hw_amd_lspci.txt")["gpus"]
    uhd = next(gpu for gpu in gpus if "UHD Graphics" in (gpu["model"] or ""))
    assert uhd["vendor"] == "Intel"
    assert uhd["integrated"] is True
    radeon = next(gpu for gpu in gpus if "6700" in (gpu["model"] or ""))
    assert radeon["integrated"] is False


def test_nvidia_fixture_has_no_integrated_gpu():
    gpus = detect("detect_hw_hiveos_rig.txt")["gpus"]
    assert len(gpus) == 5
    assert all(gpu["integrated"] is False for gpu in gpus)


def test_aspeed_and_matrox_are_integrated():
    section = (
        "0c:00.0 VGA compatible controller [0300]: "
        "ASPEED Technology, Inc. ASPEED Graphics Family [1a03:2000] (rev 41)\n"
        "0d:00.0 VGA compatible controller [0300]: "
        "Matrox Electronics Systems Ltd. MGA G200eW WPCM450 [102b:0532] (rev 0a)\n"
    )
    gpus = parse_gpus_pci(section)
    assert {gpu["vendor"] for gpu in gpus} == {"ASPEED", "Matrox"}
    assert all(gpu["integrated"] is True for gpu in gpus)


@pytest.mark.parametrize(
    "vendor,model,expected",
    [
        ("Intel", "UHD Graphics 630", True),
        ("Intel", "Iris Xe Graphics", True),
        ("Intel", "HD Graphics 530", True),
        ("Intel", "Arc A770 Graphics", False),
        ("AMD", "Radeon Graphics", True),
        ("AMD", "Radeon Vega Series / Radeon Vega Mobile Series", True),
        ("AMD", "Radeon RX 6700/6700 XT/6750 XT / 6800M", False),
        ("AMD", "Radeon RX Vega 64", False),
        ("AMD", "Navi 22 [Radeon RX 6700 XT]", False),
        ("NVIDIA", "GeForce RTX 3070", False),
        ("ASPEED", "ASPEED Graphics Family", True),
        ("Matrox", "MGA G200eW WPCM450", True),
    ],
)
def test_is_integrated_rules(vendor, model, expected):
    assert is_integrated(vendor, model) is expected


def test_apply_detection_creates_integrated_disabled(db, machine):
    from app.devices import apply_detection
    from app.models import Device

    apply_detection(db, machine, detect("detect_hw_amd_lspci.txt"))
    devices = db.query(Device).filter(Device.machine_id == machine.id, Device.kind == "gpu").all()
    uhd = next(device for device in devices if "UHD Graphics" in (device.model or ""))
    radeon = next(device for device in devices if "6700" in (device.model or ""))
    assert uhd.integrated is True
    assert uhd.enabled_for_mining is False
    assert radeon.integrated is False
    assert radeon.enabled_for_mining is True


def test_integrated_choice_survives_redetection(db, machine):
    """An iGPU the user explicitly enabled stays enabled after a new detection."""
    from app.devices import apply_detection
    from app.models import Device

    detected = detect("detect_hw_amd_lspci.txt")
    apply_detection(db, machine, detected)
    uhd = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.kind == "gpu")
        .all()
    )
    uhd = next(device for device in uhd if "UHD Graphics" in (device.model or ""))
    uhd.enabled_for_mining = True
    db.commit()
    apply_detection(db, machine, detected)
    db.refresh(uhd)
    assert uhd.integrated is True
    assert uhd.enabled_for_mining is True


def test_device_api_exposes_integrated(db, machine):
    """``device_to_dict`` carries the flag the UI renders as « intégré »."""
    from app.devices import apply_detection, device_to_dict
    from app.models import Device

    apply_detection(db, machine, detect("detect_hw_amd_lspci.txt"))
    uhd = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.kind == "gpu")
        .all()
    )
    uhd = next(device for device in uhd if "UHD Graphics" in (device.model or ""))
    payload = device_to_dict(uhd)
    assert payload["integrated"] is True
    assert payload["uuid"] == uhd.uuid
