"""§4.15 — tarifs électriques, intégration de l'énergie et rapport /api/energy."""

from datetime import datetime, timedelta, timezone

import pytest

from app import energy

UTC = timezone.utc


def _fixed(price: float = 0.20) -> dict:
    return energy.normalize_tariff({"mode": "fixed", "fixed_price": price})


def _ranges() -> dict:
    return energy.normalize_tariff({
        "mode": "ranges",
        "ranges": [
            {"start": "22:00", "end": "06:00", "price": 0.10, "label": "HC"},
            {"start": "06:00", "end": "22:00", "price": 0.20, "label": "HP"},
        ],
    })


class TestTariffs:
    def test_default_is_fixed_twenty_cents(self):
        tariff = energy.default_tariff()
        assert tariff["mode"] == "fixed"
        assert tariff["fixed_price"] == 0.20
        assert tariff["timezone"] == "Europe/Paris"

    def test_ranges_may_cross_midnight(self):
        tariff = _ranges()
        # Europe/Paris is UTC+1 in November.
        assert energy.price_at(datetime(2026, 11, 16, 23, 0, tzinfo=UTC), tariff) == 0.10
        assert energy.price_at(datetime(2026, 11, 16, 3, 0, tzinfo=UTC), tariff) == 0.10
        assert energy.price_at(datetime(2026, 11, 16, 12, 0, tzinfo=UTC), tariff) == 0.20

    def test_ranges_reject_overlap(self):
        with pytest.raises(ValueError, match="24 h sans chevauchement"):
            energy.normalize_tariff({
                "mode": "ranges",
                "ranges": [
                    {"start": "00:00", "end": "12:00", "price": 0.2},
                    {"start": "10:00", "end": "24:00", "price": 0.1},
                ],
            })

    def test_ranges_reject_gap(self):
        with pytest.raises(ValueError, match="24 h sans chevauchement"):
            energy.normalize_tariff({
                "mode": "ranges",
                "ranges": [
                    {"start": "00:00", "end": "06:00", "price": 0.2},
                    {"start": "08:00", "end": "24:00", "price": 0.1},
                ],
            })

    def test_tempo_red_day_and_unknown_blue(self):
        red = energy.normalize_tariff({
            "mode": "tempo",
            "tempo": {"day": "red"},
        })
        assert energy.price_at(datetime(2026, 11, 16, 12, 0, tzinfo=UTC), red) == 0.7562
        assert energy.price_at(datetime(2026, 11, 16, 3, 0, tzinfo=UTC), red) == 0.1568

        unknown = energy.normalize_tariff({"mode": "tempo", "tempo": {"day": "unknown"}})
        assert energy.price_at(datetime(2026, 11, 16, 3, 0, tzinfo=UTC), unknown) == 0.1296
        assert energy.price_at(datetime(2026, 11, 16, 12, 0, tzinfo=UTC), unknown) == 0.1609


class TestIntegration:
    def test_regular_points_one_kwh(self):
        start = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        points = [(start + timedelta(minutes=i), 1000.0) for i in range(61)]
        result = energy.integrate_energy(points, _fixed())
        assert result["kwh"] == pytest.approx(1.0, rel=1e-6)
        assert result["cost_eur"] == pytest.approx(0.20, rel=1e-6)
        assert result["segments"] == 60

    def test_gap_longer_than_five_minutes_is_ignored(self):
        start = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        points = [
            (start, 1000.0),
            (start + timedelta(minutes=1), 1000.0),
            (start + timedelta(minutes=20), 1000.0),
            (start + timedelta(minutes=21), 1000.0),
        ]
        result = energy.integrate_energy(points, _fixed())
        assert result["segments"] == 2
        assert result["kwh"] == pytest.approx(2 / 60, rel=1e-6)

    def test_missing_power_breaks_the_series(self):
        start = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        points = [
            (start, 1000.0),
            (start + timedelta(minutes=1), None),
            (start + timedelta(minutes=2), 1000.0),
        ]
        result = energy.integrate_energy(points, _fixed())
        assert result["segments"] == 0

    def test_cost_follows_a_tariff_change_mid_period(self):
        # 04:00 → 06:00 UTC = 05:00 → 07:00 Paris: one HC hour then one HP hour.
        start = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
        points = [(start + timedelta(minutes=i), 1000.0) for i in range(121)]
        result = energy.integrate_energy(points, _ranges())
        assert result["kwh"] == pytest.approx(2.0, rel=1e-6)
        assert result["cost_eur"] == pytest.approx(0.30, rel=1e-6)


class TestEnergyApi:
    async def test_report_uses_metric_history_and_tariff(self, client):
        from app.database import SessionLocal
        from app.models import Device, Machine, Metric

        db = SessionLocal()
        try:
            machine = Machine(
                name="rig-energy", host="10.0.0.10", usage="gpu", enabled=True,
                cpu_power_w=0, platform_power_w=0,
            )
            db.add(machine)
            db.commit()
            db.refresh(machine)
            device = Device(
                uuid="44444444-4444-4444-8444-444444444444",
                machine_id=machine.id, kind="gpu", index=0,
                model="GeForce RTX 4090", vendor="NVIDIA",
            )
            db.add(device)
            now = datetime.now(UTC).replace(microsecond=0)
            for i in range(61):
                db.add(Metric(
                    machine_uuid=machine.uuid, device_uuid=device.uuid,
                    ts=(now - timedelta(hours=1) + timedelta(minutes=i)).isoformat(),
                    power_w=1000.0,
                ))
            db.commit()
            machine_uuid = machine.uuid
        finally:
            db.close()

        saved = await client.put("/api/energy/tariff", json={
            "tariff": {"mode": "fixed", "fixed_price": 0.20},
        })
        assert saved.status_code == 200, saved.text

        response = await client.get("/api/energy?period=24h")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tariff"]["mode"] == "fixed"
        assert body["total"]["kwh"] == pytest.approx(1.0, rel=1e-6)
        assert body["total"]["cost_eur"] == pytest.approx(0.20, rel=1e-6)
        row = next(m for m in body["machines"] if m["uuid"] == machine_uuid)
        assert row["devices"][0]["kwh"] == pytest.approx(1.0, rel=1e-6)

    async def test_bad_period_is_refused(self, client):
        response = await client.get("/api/energy?period=42h")
        assert response.status_code == 400
        assert "période inconnue" in response.json()["detail"]

    async def test_invalid_ranges_are_refused(self, client):
        response = await client.put("/api/energy/tariff", json={
            "tariff": {"mode": "ranges", "ranges": [
                {"start": "00:00", "end": "12:00", "price": 0.2},
            ]},
        })
        assert response.status_code == 400
        assert "24 h" in response.json()["detail"]

    async def test_revenue_from_pool_balance_delta(self, client):
        from app.database import SessionLocal
        from app.models import Coin, Machine, MachineStatus, PoolBalanceSample, Wallet

        now = datetime.now(UTC).replace(microsecond=0)
        db = SessionLocal()
        try:
            db.add(Coin(ticker="PRL", name="Pearl", manual_price_eur=2.0))
            wallet = Wallet(
                coin_ticker="PRL", address="prl1qrevenuetest",
                pool="luckypool", pool_host="pearl",
            )
            db.add(wallet)
            db.commit()
            db.refresh(wallet)
            db.add(PoolBalanceSample(
                wallet_id=wallet.id, ts=(now - timedelta(hours=24)).isoformat(),
                paid=5.0, unpaid=2.0, immature=3.0, total=10.0,
            ))
            db.add(PoolBalanceSample(
                wallet_id=wallet.id, ts=now.isoformat(),
                paid=5.0, unpaid=5.0, immature=5.0, total=15.0,
            ))
            machine = Machine(
                name="rig-prl", host="10.0.0.11", usage="gpu", enabled=True,
                cpu_power_w=0, platform_power_w=0,
            )
            db.add(machine)
            db.commit()
            db.refresh(machine)
            db.add(MachineStatus(
                machine_uuid=machine.uuid, ts=now.isoformat(), online=True,
                coin="PRL", total_hashrate_hs=100.0,
            ))
            db.commit()
            machine_uuid = machine.uuid
        finally:
            db.close()

        response = await client.get("/api/energy?period=24h")
        assert response.status_code == 200, response.text
        row = next(m for m in response.json()["machines"] if m["uuid"] == machine_uuid)
        # 5 PRL mined in 24 h × 2.00 €/PRL, no measured consumption.
        assert row["revenue_eur_day"] == pytest.approx(10.0, rel=1e-6)
        assert row["net_eur_day"] == pytest.approx(10.0, rel=1e-6)
