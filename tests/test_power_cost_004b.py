"""§4.2 / §4.15 — puissance CPU mesurée (RAPL avec plafond) et coût à la prise."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.cpu_stats import cpu_state_with, parse_cpu_report, parse_rapl, split_blocks

FIXTURE = Path(__file__).parent / "fixtures" / "rapl_pair_max.txt"
#: max_energy_range_uj réel, relevé avec energy_uj sur l'hôte Proxmox (i7-9750H).
MAX_ENERGY = 262_143_328_850


def test_rapl_fixture_is_the_real_proxmox_output():
    blocks = split_blocks(FIXTURE.read_text())
    # package-0 : 261398385689 - 261337865702 = 60 519 987 µJ sur 1 s.
    assert parse_rapl(blocks["POWER"], blocks["POWER2"]) == pytest.approx(60.52, abs=0.01)
    report = parse_cpu_report(FIXTURE.read_text())
    assert report["state"]["power_source"] == "rapl"
    assert report["state"]["power_w"] == pytest.approx(60.52, abs=0.01)


def test_rapl_counter_wraparound_uses_max_energy_range():
    before = f"/sys/class/powercap/intel-rapl:0/energy_uj|{MAX_ENERGY - 1_000_000}|{MAX_ENERGY}\n"
    after = f"/sys/class/powercap/intel-rapl:0/energy_uj|500000|{MAX_ENERGY}\n"
    # 1 000 000 µJ pour finir le tour + 500 000 µJ après le retour à zéro.
    assert parse_rapl(before, after) == pytest.approx(1.5, abs=0.01)


def test_rapl_wraparound_without_max_is_ignored():
    before = "/sys/class/powercap/intel-rapl:0/energy_uj|1000000\n"
    after = "/sys/class/powercap/intel-rapl:0/energy_uj|500000\n"
    assert parse_rapl(before, after) is None


def test_psys_and_mmio_mirrors_are_never_summed():
    blocks = split_blocks(FIXTURE.read_text())
    assert "intel-rapl:1" in blocks["POWER"]      # psys, plateforme entière
    assert "intel-rapl-mmio:0" in blocks["POWER"]  # miroir mémoire de package-0
    assert parse_rapl(blocks["POWER"], blocks["POWER2"]) == pytest.approx(60.52, abs=0.01)


def test_power_source_order_is_api_then_rapl_then_typed_then_none():
    report = parse_cpu_report(FIXTURE.read_text())

    api = cpu_state_with(report, api_power_w=88.96)
    assert api["power_source"] == "API SRBMiner package_power"
    assert api["power_w"] == 88.96

    # Une API à 0 (l'Athlon) ne masque pas RAPL déjà lu.
    rapl = cpu_state_with(report, api_power_w=0.0, typed_power_w=65.0)
    assert rapl["power_source"] == "rapl"
    assert rapl["power_w"] == pytest.approx(60.52, abs=0.01)

    typed = cpu_state_with({"state": {}}, typed_power_w=65.0)
    assert typed["power_source"] == "saisie (machines.cpu_power_w)"
    assert typed["power_w"] == 65.0

    # 2026.11.004-c5 : sans mesure ni saisie, le TDP du modèle — 65 W générique
    # quand le modèle est inconnu — et la valeur se déclare estimée.
    none = cpu_state_with({"state": {}})
    assert none["power_source"] == "estimation générique 65 W"
    assert none["power_w"] == 65.0
    assert none["power_estimated"] is True


async def test_cost_per_day_month_and_per_coin(client):
    created = await client.post(
        "/api/machines",
        json={
            "name": "cost-1",
            "host": "192.0.2.78",
            "ssh_user": "root",
            "platform_power_w": 0,
        },
    )
    assert created.status_code == 200, created.text
    machine = created.json()

    from app.database import SessionLocal
    from app.models import MachineStatus, Metric

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        db.add_all([
            # 60 s d'écart : au-delà de 300 s, l'intégration ignore le trou.
            Metric(machine_uuid=machine["uuid"], device_uuid=None,
                   ts=(now - timedelta(seconds=60)).isoformat(), power_w=100.0),
            Metric(machine_uuid=machine["uuid"], device_uuid=None,
                   ts=now.isoformat(), power_w=100.0),
            MachineStatus(machine_uuid=machine["uuid"], coin="TST", online=True,
                          total_hashrate_hs=1000.0),
        ])
        db.commit()
    finally:
        db.close()

    report = (await client.get("/api/energy?period=24h")).json()
    row = next(m for m in report["machines"] if m["uuid"] == machine["uuid"])
    # 100 W pendant 60 s = 0,0016667 kWh ; tarif par défaut 0,20 €/kWh.
    assert row["cost_eur_day"] == pytest.approx(0.0003333, abs=1e-6)
    assert row["cost_eur_month"] == pytest.approx(row["cost_eur_day"] * 30, abs=1e-9)
    assert row["avg_power_w"] == pytest.approx(100.0, abs=0.01)

    assert report["total"]["cost_eur_month"] == pytest.approx(
        report["total"]["cost_eur_day"] * 30, abs=1e-9
    )

    coin = next(c for c in report["coins"] if c["ticker"] == "TST")
    assert coin["machines"] == 1
    assert coin["electricity_eur_day"] == pytest.approx(row["cost_eur_day"], abs=1e-9)
    assert coin["net_eur_day"] is None
    assert "prix" in coin["note"]
