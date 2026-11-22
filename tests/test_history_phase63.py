"""Phase 6.3 — historique, agrégation, rétention, séries et export (§10.7).

Données fictives uniquement : machines 192.0.2.x, wallets « TEST… ».
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from app import history
from app.database import SessionLocal
from app.models import (
    Device,
    Machine,
    Metric,
    MetricAgg,
    PlugPowerSample,
    PoolBalance,
    PoolBalanceSample,
    PriceSample,
    Psu,
    Wallet,
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _machine(db, name: str = "rig-hist", uuid: str = "11111111-1111-1111-1111-111111111111") -> Machine:
    machine = Machine(name=name, host="192.0.2.10", uuid=uuid, enabled=True)
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def _device(db, machine: Machine, kind: str = "gpu", index: int = 0) -> Device:
    device = Device(
        machine_id=machine.id,
        uuid=f"dev-{machine.id}-{kind}-{index}",
        kind=kind,
        index=index,
        model="Fictive RTX",
        present=True,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def _wallet(db, ticker: str = "TST", address: str = "TESTWALLET0000000000000000000000000000000000") -> Wallet:
    wallet = Wallet(coin_ticker=ticker, address=address, label="essai", pool="luckypool")
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


# ── Rétention ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_retention_defaults_clamp_and_persist(client):
    response = await client.get("/api/history/retention")
    assert response.status_code == 200
    assert response.json() == {"raw_days": 30, "hourly_days": 365, "daily_days": 0}

    response = await client.put(
        "/api/history/retention", json={"raw_days": 7, "hourly_days": 400}
    )
    assert response.status_code == 200
    assert response.json()["raw_days"] == 7
    assert response.json()["hourly_days"] == 400

    response = await client.put(
        "/api/history/retention", json={"raw_days": 0, "hourly_days": 99999}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["raw_days"] == 1, "0 jour ramené à la borne basse"
    assert body["hourly_days"] == 3650
    assert body["daily_days"] == 0, "le journalier reste illimité"


# ── Agrégation ────────────────────────────────────────────────────────
def test_aggregate_hours_then_days_and_idempotent():
    db = SessionLocal()
    try:
        machine = _machine(db)
        gpu1 = _device(db, machine, "gpu", 0)
        gpu2 = _device(db, machine, "gpu", 1)
        moment = datetime.now(timezone.utc) - timedelta(hours=72)
        base = moment.replace(minute=0, second=0, microsecond=0)
        for offset in (timedelta(minutes=0), timedelta(minutes=10)):
            ts = _iso(base + offset)
            db.add(Metric(machine_uuid=machine.uuid, device_uuid=gpu1.uuid, ts=ts,
                          hashrate_hs=100.0, power_w=100.0, temp_c=60.0))
            db.add(Metric(machine_uuid=machine.uuid, device_uuid=gpu2.uuid, ts=ts,
                          hashrate_hs=150.0, power_w=150.0, temp_c=70.0))
            db.add(PlugPowerSample(machine_uuid=machine.uuid, ts=ts, power_w=400.0, dc_w=250.0, source="psu"))
        db.add(Metric(machine_uuid=machine.uuid, device_uuid=gpu1.uuid,
                      ts=_iso(base + timedelta(hours=1)), hashrate_hs=120.0,
                      power_w=110.0, temp_c=61.0))
        db.commit()

        first = history.aggregate(db, now=datetime.now(timezone.utc))
        assert first["hour_buckets"] == 5, "2 heures machine + 3 buckets device"
        assert first["day_buckets"] == 3, "1 jour × (1 machine + 2 devices)"

        hours = (
            db.query(MetricAgg)
            .filter(MetricAgg.bucket == "hour", MetricAgg.scope == "machine")
            .order_by(MetricAgg.ts.asc())
            .all()
        )
        assert len(hours) == 2
        assert hours[0].power_avg_w == 250.0, "somme des devices au même instant"
        assert hours[0].hashrate_avg_hs == 250.0
        assert hours[0].power_plug_avg_w == 400.0
        assert hours[0].temp_avg_c == 65.0, "moyenne des températures devices"
        assert hours[0].samples == 2

        device_hour = (
            db.query(MetricAgg)
            .filter(MetricAgg.bucket == "hour", MetricAgg.scope == "device", MetricAgg.scope_uuid == gpu1.uuid)
            .first()
        )
        assert device_hour.power_avg_w == 100.0

        day = (
            db.query(MetricAgg)
            .filter(MetricAgg.bucket == "day", MetricAgg.scope == "machine")
            .first()
        )
        assert day is not None
        assert day.samples == 3, "3 instants machine agrégés dans la journée"

        # Second passage : aucun doublon.
        before = db.query(MetricAgg).count()
        history.aggregate(db, now=datetime.now(timezone.utc))
        assert db.query(MetricAgg).count() == before
    finally:
        db.close()


# ── Purge ─────────────────────────────────────────────────────────────
def test_purge_respects_retention_and_keeps_daily():
    db = SessionLocal()
    try:
        machine = _machine(db, name="rig-purge", uuid="22222222-2222-2222-2222-222222222222")
        now = datetime.now(timezone.utc)
        old = _iso(now - timedelta(days=2))
        recent = _iso(now - timedelta(hours=1))
        db.add(Metric(machine_uuid=machine.uuid, ts=old, power_w=10.0))
        db.add(Metric(machine_uuid=machine.uuid, ts=recent, power_w=20.0))
        db.add(MetricAgg(scope="machine", scope_uuid=machine.uuid, bucket="hour",
                         ts=_iso(now - timedelta(days=400)), samples=1))
        db.add(MetricAgg(scope="machine", scope_uuid=machine.uuid, bucket="hour",
                         ts=_iso(now - timedelta(hours=3)), samples=1))
        db.add(MetricAgg(scope="machine", scope_uuid=machine.uuid, bucket="day",
                         ts=_iso(now - timedelta(days=500)), samples=1))
        db.commit()

        result = history.purge(
            db, {"raw_days": 1, "hourly_days": 365, "daily_days": 0}, now=now
        )
        assert result["metrics"] == 1
        assert result["hour_buckets"] == 1
        assert result["day_buckets"] == 0
        assert db.query(Metric).count() == 1
        assert (
            db.query(MetricAgg).filter(MetricAgg.bucket == "day").count() == 1
        ), "le journalier n'est jamais purgé quand daily_days = 0"
        assert (
            db.query(MetricAgg).filter(MetricAgg.bucket == "hour").count() == 1
        )
    finally:
        db.close()


# ── Puissance à la prise ──────────────────────────────────────────────
def test_plug_power_uses_psu_efficiency_and_falls_back_to_dc():
    db = SessionLocal()
    try:
        machine = _machine(db, name="rig-plug", uuid="33333333-3333-3333-3333-333333333333")
        device = _device(db, machine)
        db.add(Metric(machine_uuid=machine.uuid, device_uuid=device.uuid,
                      ts=_iso(datetime.now(timezone.utc)), power_w=250.0, temp_c=55.0))
        db.add(Psu(machine_id=machine.id, uuid="psu-1", name="Fictive 500",
                   rated_w=500.0, certification="gold", derate_pct=100.0))
        db.commit()

        sample = history.record_plug_sample(db, machine, 250.0)
        db.commit()
        assert sample is not None
        assert sample.source == "psu"
        assert sample.power_w is not None and sample.power_w > 250.0, "rendement < 100 %"

        bare = _machine(db, name="rig-sans-psu", uuid="44444444-4444-4444-4444-444444444444")
        fallback = history.record_plug_sample(db, bare, 90.0)
        assert fallback is not None
        assert fallback.source == "dc"
        assert fallback.power_w == 90.0, "aucune valeur inventée sans PSU"
    finally:
        db.close()


# ── Séries ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_series_machine_raw_hourly_empty_and_errors(client):
    db = SessionLocal()
    try:
        machine = _machine(db, name="rig-series", uuid="55555555-5555-5555-5555-555555555555")
        device = _device(db, machine)
        now = datetime.now(timezone.utc)
        db.add(Metric(machine_uuid=machine.uuid, device_uuid=device.uuid,
                      ts=_iso(now - timedelta(hours=2)), hashrate_hs=1000.0,
                      power_w=80.0, temp_c=50.0))
        db.add(MetricAgg(scope="machine", scope_uuid=machine.uuid, bucket="hour",
                         ts=_iso(now - timedelta(days=5)), hashrate_avg_hs=900.0,
                         power_avg_w=70.0, power_plug_avg_w=90.0, temp_avg_c=49.0, samples=12))
        db.commit()
        uuid = machine.uuid
    finally:
        db.close()

    response = await client.get(
        "/api/history/series", params={"scope": "machine", "target": uuid, "range": "24h"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resolution"] == "raw"
    assert body["count"] == 1
    assert body["points"][0]["hashrate_hs"] == 1000.0

    response = await client.get(
        "/api/history/series", params={"scope": "machine", "target": uuid, "range": "30d"}
    )
    body = response.json()
    assert body["resolution"] == "hour"
    assert body["count"] == 1
    assert body["points"][0]["power_plug_w"] == 90.0

    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    response = await client.get(
        "/api/history/series",
        params={"scope": "machine", "target": uuid, "range": "custom",
                "start": future, "end": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()},
    )
    assert response.status_code == 200
    assert response.json()["count"] == 0, "période vide : aucun point, pas d'erreur"

    response = await client.get(
        "/api/history/series", params={"scope": "machine", "target": "inconnu", "range": "24h"}
    )
    assert response.status_code == 404

    response = await client.get(
        "/api/history/series", params={"scope": "machine", "target": uuid, "range": "n'importe"}
    )
    assert response.status_code == 422

    response = await client.get(
        "/api/history/series",
        params={"scope": "machine", "target": uuid, "range": "custom", "start": future},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_series_coin_balance_and_value(client):
    db = SessionLocal()
    try:
        wallet = _wallet(db, "TST")
        now = datetime.now(timezone.utc)
        db.add(PoolBalanceSample(wallet_id=wallet.id, ts=_iso(now - timedelta(hours=5)),
                                 paid=1.0, total=1.0))
        db.add(PoolBalanceSample(wallet_id=wallet.id, ts=_iso(now - timedelta(hours=4)),
                                 paid=2.0, total=2.0))
        db.add(PriceSample(ticker="TST", ts=_iso(now - timedelta(hours=6)), eur=3.0, source="coingecko"))
        db.commit()
    finally:
        db.close()

    response = await client.get(
        "/api/history/series", params={"scope": "coin", "target": "TST", "range": "24h"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["points"][0]["value_eur"] == 3.0
    assert body["points"][1]["value_eur"] == 6.0


# ── Export CSV ────────────────────────────────────────────────────────
def _seed_gains(db):
    wallet = _wallet(db, "TST")
    unpriced = _wallet(db, "NOP", address="TESTNOPRICE00000000000000000000000000000")
    now = datetime.now(timezone.utc)
    db.add(PoolBalanceSample(wallet_id=wallet.id, ts=_iso(now - timedelta(hours=10)), paid=1.0))
    db.add(PoolBalanceSample(wallet_id=wallet.id, ts=_iso(now - timedelta(hours=8)), paid=1.5))
    db.add(PoolBalanceSample(wallet_id=unpriced.id, ts=_iso(now - timedelta(hours=9)), paid=10.0))
    db.add(PoolBalanceSample(wallet_id=unpriced.id, ts=_iso(now - timedelta(hours=7)), paid=12.0))
    db.add(PriceSample(ticker="TST", ts=_iso(now - timedelta(hours=9)), eur=2.0, source="coingecko"))
    db.add(PoolBalance(wallet_id=wallet.id, pool="luckypool", coin_ticker="TST",
                       last_payment_at=_iso(now - timedelta(hours=8)),
                       last_payment_amount=0.5, last_payment_tx="txfictive0001"))
    db.commit()


def test_csv_format_bom_separator_and_no_secret():
    db = SessionLocal()
    try:
        _seed_gains(db)
        payload = history.csv_export(db)
    finally:
        db.close()

    assert payload.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM pour Excel"
    text = payload.decode("utf-8-sig")
    assert "Données fournies à titre informatif, pas de conseil fiscal" in text
    assert "\r\n" in text

    lines = [line for line in text.splitlines() if not line.startswith("#")]
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=";"))
    assert rows[0] == history.EXPORT_HEADER
    assert len(rows) == 3, "deux réceptions (une par coin), hors commentaires"

    by_coin = {row[0]: row for row in rows[1:]}
    assert by_coin["TST"][1] == "0,5", "décimale française"
    assert by_coin["TST"][3] == "1", "0,5 × 2 €"
    assert by_coin["TST"][4] == "coingecko"
    assert by_coin["TST"][5] == "pool"
    assert by_coin["TST"][6] == "txfictive0001"

    assert by_coin["NOP"][1] == "2"
    assert by_coin["NOP"][3] == "", "aucun prix : valeur vide"
    assert by_coin["NOP"][4] == "inconnu"

    assert "TESTWALLET0000000000000000000000000000000000" not in text
    assert "TESTNOPRICE" not in text
    assert "192.0.2." not in text


@pytest.mark.asyncio
async def test_export_endpoint_filters_coin_and_sets_headers(client):
    db = SessionLocal()
    try:
        _seed_gains(db)
    finally:
        db.close()

    response = await client.get("/api/history/export.csv", params={"coin": "TST"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    body = response.content.decode("utf-8-sig")
    rows = [line for line in body.splitlines() if not line.startswith("#")]
    assert len(rows) == 2, "en-tête + une seule réception TST"
    assert "NOP" not in body

    response = await client.get("/api/history/export.csv", params={"start": "pas-une-date"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_maintenance_endpoint_returns_bounded_summary(client):
    response = await client.post("/api/history/maintenance")
    assert response.status_code == 200
    body = response.json()
    assert "aggregate" in body and "purge" in body
    assert "truncated" in body["aggregate"]
