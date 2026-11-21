"""023-bis — une unité lancée par Fossor n'est plus un « mineur externe ».

Cas réels relevés en prod le 22/11 (``PS_MINERS`` masqué, port 8008) :

* Athlon : SRBMiner Pearl GPU et cpuminer KSH sous ``/opt/fossor/miners`` ;
* Ryzen : Pearl relancé par ``script`` (``tty_wrapper``) + BigCoin hors Fossor ;
* Rig HiveOS : SRBMiner du flight sheet, wam-miner et nœud Korsh.

Avant : les six devices des trois machines affichaient « occupé par un mineur
externe (hors Fossor) », campagnes et boost bloqués par des 409.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile(db, name: str, *, software: str, binary: str, version: str,
             coin: str, api_port: int | None = None) -> int:
    from app.models import MinerProfile

    row = MinerProfile(
        uuid=str(uuid.uuid4()), name=name, coin=coin, wallet="testwallet",
        pool="stratum+tcp://pool.exemple.tld:3333", software=software,
        binary_url="", sha256="b" * 64, binary_path=binary, version=version,
        cmdline=f"/opt/fossor/miners/{software}/{version}/{binary}",
        api_port=api_port, api_kind="srbminer" if api_port else "",
        binary_source="fossor", execution_mode="host", threads=0,
        worker="test", created_at=_now(), updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _machine(db, name: str, *, os_kind: str = "ubuntu", kind: str = "host") -> int:
    from app.models import Machine

    row = Machine(
        name=name, host="192.0.2.10", ssh_port=22, ssh_user="fossor",
        auth_type="fossor_key", machine_kind=kind, os_kind=os_kind,
        enabled=True, default_miner_profile_id=None,
        created_at=_now(), updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _device(db, machine_id: int, kind: str, index: int | None, *,
            token: int | None = None, enabled: bool = True,
            integrated: bool = False, model: str = "") -> str:
    from app.models import Device

    value = str(uuid.uuid4())
    db.add(Device(
        uuid=value, machine_id=machine_id, kind=kind, index=index,
        vendor="amd", model=model or f"{kind} test", present=True,
        enabled_for_mining=enabled, integrated=integrated,
        role="default_and_spec", default_token_id=token,
        created_at=_now(), updated_at=_now(),
    ))
    db.commit()
    return value


def _apply(db, machine_id: int, entries: list[dict]) -> None:
    from app.devices import mark_external_devices, set_external_miners
    from app.models import Machine

    machine = db.get(Machine, machine_id)
    mark_external_devices(db, machine, entries)
    set_external_miners(machine, entries)
    db.commit()


def _by_uuid(db, value: str):
    from app.models import Device

    return db.query(Device).filter(Device.uuid == value).one()


def test_athlon_deux_unites_fossor_sont_releasables(db):
    """Pearl GPU + KSH CPU sous ``/opt/fossor/miners`` : plus d'« externe »."""
    pearl = _profile(db, "Athlon Pearl GPU (SRBMiner)", software="srbminer",
                     binary="srbminer_compute", version="3.8.3", coin="PRL",
                     api_port=21550)
    ksh = _profile(db, "Athlon KSH CPU", software="custom",
                   binary="cpuminer-korsh", version="1.0", coin="KSH")
    machine = _machine(db, "athlon-3000g")
    gpu0 = _device(db, machine, "gpu", 0, token=pearl)
    gpu1 = _device(db, machine, "gpu", 1, token=pearl)
    cpu = _device(db, machine, "cpu", 0, token=ksh)

    _apply(db, machine, [
        {"name": "SRBMiner", "kind": "gpu", "api_port": 21550,
         "cmd": "/opt/fossor/miners/srbminer/3.8.3/srbminer_compute --algorithm "
                "pearlhash --wallet testwallet --pool stratum+tcp://pool:3360 "
                "--api-enable --api-port 21550"},
        {"name": "cpuminer", "kind": "cpu", "api_port": None,
         "cmd": "/opt/fossor/miners/custom/1.0/cpuminer-korsh --algo yespower "
                "--url stratum+tcp://pool.korsh.example:3333 --threads 2"},
    ])

    for value in (gpu0, gpu1, cpu):
        device = _by_uuid(db, value)
        assert device.occupied_external is False
        assert device.occupied_by == "fossor_default"
    from app.devices import occupancy

    assert occupancy(_by_uuid(db, gpu0))["occupied_label"] == "Token par défaut (Fossor)"
    assert occupancy(_by_uuid(db, cpu))["releasable"] is True
    # Le JSON des mineurs ne contient plus les unités Fossor.
    from app.models import Machine

    assert not (db.get(Machine, machine).external_miners or "").strip(
        "[]"
    )


def test_ryzen_gpu_fossor_releasable_cpu_bigcoin_externe(db):
    """``script`` (tty_wrapper) reste une unité Fossor ; BigCoin reste externe."""
    pearl = _profile(db, "Ryzen Pearl GPU (SRBMiner)", software="srbminer",
                     binary="srbminer_compute", version="3.8.3", coin="PRL",
                     api_port=21373)
    machine = _machine(db, "Ryzen-5-3600")
    gpu = _device(db, machine, "gpu", 0, token=pearl)
    cpu = _device(db, machine, "cpu", 0, token=None)

    _apply(db, machine, [
        {"name": "SRBMiner", "kind": "gpu", "api_port": 21373,
         "cmd": "/usr/bin/script -qfec /opt/fossor/miners/srbminer/3.8.3/"
                "srbminer_compute --algorithm pearlhash --api-port 21373 /dev/null"},
        {"name": "Minage solo (nœud)", "kind": "solo", "api_port": None,
         "cmd": "./bitcoind -datadir=/home/test/bigcoin-mine/bigcoin/data -daemon"},
    ])

    assert _by_uuid(db, gpu).occupied_external is False
    assert _by_uuid(db, gpu).occupied_by == "fossor_default"
    assert _by_uuid(db, cpu).occupied_external is True
    assert _by_uuid(db, cpu).occupied_by == "external"


def test_rig_hiveos_flight_sheet_releasable(db):
    """HiveOS : le GPU du flight sheet est libéré par ``miner stop/start``."""
    machine = _machine(db, "RIG — rig7095FA", os_kind="hiveos")
    gpus = [_device(db, machine, "gpu", index) for index in range(3)]
    cpu = _device(db, machine, "cpu", 0)

    _apply(db, machine, [
        {"name": "SRBMiner-MULTI", "kind": "gpu", "api_port": 21550,
         "cmd": "./SRBMiner-MULTI --algorithm pearlhash --pool pool:3360 "
                "--wallet testwallet --api-port 21550"},
        {"name": "wam-miner", "kind": "cpu", "api_port": None,
         "cmd": "/root/wam/wam-miner -o stratum+tcp://pool.wam.example:3333 -t 2"},
        {"name": "Minage solo (nœud)", "kind": "solo", "api_port": None,
         "cmd": "/root/korsh/bin/korshd -datadir=/root/korsh/data -daemon"},
    ])

    for value in gpus:
        device = _by_uuid(db, value)
        assert device.occupied_external is False
        assert device.occupied_by == "hiveos"
    assert _by_uuid(db, cpu).occupied_external is True


def test_campagne_en_cours_nomme_l_occupation(db):
    """Un GPU tenu par une campagne Fossor affiche « Campagne #N (Fossor) »."""
    from app.models import Campaign, CampaignDevice

    pearl = _profile(db, "Athlon Pearl GPU (SRBMiner)", software="srbminer",
                     binary="srbminer_compute", version="3.8.3", coin="PRL",
                     api_port=21550)
    machine = _machine(db, "athlon-3000g")
    gpu = _device(db, machine, "gpu", 0, token=pearl)
    campaign = Campaign(
        uuid=str(uuid.uuid4()), coin="PRL", profile_id=pearl,
        start_at=_now(), status="running", origin="manual",
        execution_mode="host", created_at=_now(), updated_at=_now(),
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    db.add(CampaignDevice(campaign_id=campaign.id, device_uuid=gpu,
                          machine_id=machine, label="GPU 0"))
    db.commit()

    _apply(db, machine, [
        {"name": "SRBMiner", "kind": "gpu", "api_port": 21550,
         "cmd": "/opt/fossor/miners/srbminer/3.8.3/srbminer_compute "
                "--api-port 21550"},
    ])

    from app.devices import occupancy

    held = occupancy(_by_uuid(db, gpu))
    assert held["occupied_by"] == "fossor_campaign"
    assert held["occupied_label"] == f"Campagne #{campaign.id} (Fossor)"
    assert held["releasable"] is True


def test_mineur_externe_bloque_toujours(db):
    """BigCoin/WAM/Korsh restent « externe » : la cohabitation bloque encore."""
    machine = _machine(db, "athlon-3000g")
    gpu0 = _device(db, machine, "gpu", 0)
    gpu1 = _device(db, machine, "gpu", 1)

    _apply(db, machine, [
        {"name": "BigCoin", "kind": "gpu", "gpu_id": 1, "api_port": None,
         "cmd": "/home/test/bigcoin --wallet testwallet"},
    ])

    assert _by_uuid(db, gpu0).occupied_external is False
    assert _by_uuid(db, gpu1).occupied_external is True
    assert _by_uuid(db, gpu1).occupied_by == "external"
