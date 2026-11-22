"""025-b — Suivi → Alimentations lit exactement les valeurs de /api/health.

Machine fictive : un GPU mesuré (200 W dans ``metrics``) et un GPU estimé
(aucune mesure, modèle connu) sur une PSU 750 W Gold.  L'endpoint du Suivi doit
rendre le même ``wall_power_w`` que ``/api/health``, et le total du parc doit
être la somme de ces mêmes valeurs.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timezone

import pytest

from app.database import SessionLocal
from app.models import Device, Machine, Metric

pytestmark = pytest.mark.asyncio


async def test_followup_power_matches_health_and_splits_measured_estimated(client):
    created = await client.post(
        "/api/machines", json={"name": "rig-fictif-025b", "host": "192.0.2.61"}
    )
    assert created.status_code in (200, 201), created.text
    machine_id = created.json()["id"]

    psu = await client.post(
        f"/api/machines/{machine_id}/psus",
        json={"name": "PSU fictive 750", "rated_w": 750, "certification": "gold"},
    )
    assert psu.status_code == 201, psu.text

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        assert machine is not None
        measured_gpu = Device(
            uuid=str(uuid_lib.uuid4()), machine_id=machine_id, kind="gpu", index=0,
            model="GPU fictif mesuré", present=True, enabled_for_mining=True,
        )
        estimated_gpu = Device(
            uuid=str(uuid_lib.uuid4()), machine_id=machine_id, kind="gpu", index=1,
            model="RTX 3060", present=True, enabled_for_mining=True,
        )
        db.add_all([measured_gpu, estimated_gpu])
        db.commit()
        db.add(Metric(
            machine_uuid=machine.uuid,
            device_uuid=measured_gpu.uuid,
            ts=datetime.now(timezone.utc).isoformat(),
            power_w=200.0,
        ))
        db.commit()
        machine_uuid = machine.uuid
    finally:
        db.close()

    power_res = await client.get("/api/health/power")
    assert power_res.status_code == 200, power_res.text
    power = power_res.json()

    health_res = await client.get("/api/health")
    assert health_res.status_code == 200, health_res.text
    health = health_res.json()

    health_row = next(row for row in health["machines"] if row["uuid"] == machine_uuid)
    power_row = next(row for row in power["machines"] if row["uuid"] == machine_uuid)

    # Une seule source : le wall_power_w de /api/health, recopié tel quel.
    assert power_row["wall_power_w"] == health_row["wall_power_w"]
    assert power_row["wall_power_w"] > 0

    # Part mesurée (GPU 200 W) et part estimée (second GPU) coexistent.
    assert power_row["measured_w"] == pytest.approx(200.0, abs=0.1)
    assert power_row["estimated_w"] > 0

    # Total du parc = somme des wall_power_w de /api/health.
    expected_total = sum(float(row["wall_power_w"] or 0.0) for row in health["machines"])
    assert power["total"]["wall_power_w"] == pytest.approx(expected_total, abs=0.1)

    # Chaque alimentation porte charge, %, rendement et puissance à la prise.
    assert power_row["psus"], power_row
    psu_row = power_row["psus"][0]
    assert psu_row["name"] == "PSU fictive 750"
    assert psu_row["load_dc_w"] > 0
    assert 0 < psu_row["efficiency_pct"] <= 100
    assert psu_row["wall_w"] >= psu_row["load_dc_w"]

    # Seuils §4.2 bis et coût au tarif configuré.
    assert power["thresholds"] == {"psu_warn_pct": 80.0, "psu_crit_pct": 90.0}
    assert power["tariff"]["price_eur_kwh"] > 0
    assert power["tariff"]["hourly_eur"] == pytest.approx(
        power["total"]["wall_power_w"] / 1000.0 * power["tariff"]["price_eur_kwh"],
        abs=0.001,
    )
    assert len(power["history"]["h24"]) == 24
    assert len(power["history"]["d7"]) == 28
