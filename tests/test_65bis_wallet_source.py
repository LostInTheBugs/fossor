"""Étape 6.5-bis — source unique des wallets de pool, et rattrapage des agrégats.

Données fictives, aucune adresse réelle, aucun appel réseau : la forme des
champs reproduit celle de la production du 2026-11-23 (sept wallets dont
``pool = ""``, l'adresse Pearl vivant dans un profil de mineur).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import history, payments
from app.database import SessionLocal
from app.models import (
    Metric,
    MetricAgg,
    MinedCampaign,
    MinerProfile,
    Wallet,
)

PEARL = "prl1qtestfixture00000000000000000000000000k9vd"
PROFILE_POOL = "stratum+tcp://pearl-eu2.luckypool.io:3360"


def _empty_wallets(db):
    """Les sept wallets de la prod : tous ``pool = ""``."""
    for index, ticker in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"), start=1):
        db.add(Wallet(coin_ticker=ticker, address=f"{ticker.lower()}1fictif{index:04d}", pool=""))
    db.commit()


def _pearl_profile(db, coin="PRL", pool=PROFILE_POOL):
    profile = MinerProfile(
        uuid="c729ed99-0000-4000-8000-000000000001",
        name="Athlon Pearl GPU (SRBMiner)",
        coin=coin,
        wallet=PEARL,
        pool=pool,
        software="srbminer",
        binary_url="",
        sha256="0" * 64,
        binary_path="srbminer_compute",
        version="3.8.3",
        cmdline="/opt/fossor/miners/srbminer/3.8.3/srbminer_compute",
        api_port=21550,
    )
    db.add(profile)
    db.add(MinedCampaign(
        legacy_key="live:athlon",
        source="legacy",
        coin_ticker=coin,
        coin_name="Pearl",
        state="active",
        start_iso="2026-11-01T00:00:00+00:00",
        pool=PROFILE_POOL,
    ))
    db.commit()
    return profile


# ── Source unique des wallets ─────────────────────────────────────────
def test_payment_wallets_reads_the_address_from_the_miner_profile():
    """Le wallet de Pearl ne vit que dans le profil : la synchro doit le voir."""
    db = SessionLocal()
    try:
        _empty_wallets(db)
        _pearl_profile(db)

        shared = payments.payment_wallets(db)
        assert [(row.coin_ticker, row.address) for row in shared] == [("PRL", PEARL)]
        assert shared[0].pool_host == "pearl-eu2.luckypool.io"
        assert shared[0].source == "profile"
        assert shared[0].id is None, "aucun Wallet en base avant matérialisation"
        assert db.query(Wallet).filter(Wallet.coin_ticker == "PRL").count() == 0
    finally:
        db.close()


def test_materialized_wallet_is_idempotent_and_feeds_the_balance_display():
    """Une seule ligne Wallet, et l'écran Tokens minés la retrouve par pièce."""
    db = SessionLocal()
    try:
        _empty_wallets(db)
        _pearl_profile(db)

        first = payments.payment_wallets(db, materialize=True)
        second = payments.payment_wallets(db, materialize=True)
        assert [row.id for row in first] == [row.id for row in second if row.coin_ticker == "PRL"]
        rows = db.query(Wallet).filter(Wallet.coin_ticker == "PRL").all()
        assert len(rows) == 1
        assert rows[0].address == PEARL
        assert rows[0].pool == "luckypool"
        assert rows[0].pool_host == "pearl-eu2.luckypool.io"
        assert rows[0].label.startswith("profil :")

        # Ce que fait l'écran : le wallet du coin, donc le même identifiant que
        # celui sous lequel le solde et les paiements sont stockés.
        by_coin = {wallet.coin_ticker: wallet for wallet in db.query(Wallet).all()}
        assert by_coin["PRL"].id == rows[0].id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sync_all_queries_the_profile_wallet(monkeypatch):
    """La synchro interroge exactement l'adresse que l'écran affiche."""
    db = SessionLocal()
    try:
        _empty_wallets(db)
        _pearl_profile(db)

        seen: list[tuple[str, str]] = []

        async def fake_sync(db_, wallet):
            seen.append((wallet.coin_ticker, wallet.address))
            return {"ok": True, "new": 0}

        async def fake_solo(db_):
            return {"ok": True, "nodes": []}

        monkeypatch.setattr(payments, "sync_luckypool_wallet", fake_sync)
        monkeypatch.setattr(payments, "sync_solo_nodes", fake_solo)

        report = await payments.sync_all(db)
        assert seen == [("PRL", PEARL)]
        assert report["ok"] is True
        assert len(report["wallets"]) == 1
        assert db.query(Wallet).filter(Wallet.coin_ticker == "PRL").count() == 1

        again = await payments.sync_all(db)
        assert seen == [("PRL", PEARL), ("PRL", PEARL)]
        assert again["ok"] is True
        assert db.query(Wallet).filter(Wallet.coin_ticker == "PRL").count() == 1
    finally:
        db.close()


# ── Agrégats : un retard de trois heures est rattrapé ────────────────
def test_hour_backlog_is_caught_up_in_one_pass():
    """Curseur trois heures en arrière → les buckets horaires sont créés.

    En prod le dernier bucket ``hour`` est à 23:00 quand on est à 02:57 : le
    décalage vient du ``cutoff`` (``now - 2 h``) et reste sous le seuil de 24 h.
    Ce test fige la règle : ce retard-là doit être rattrapé au passage suivant.
    """
    db = SessionLocal()
    try:
        now = datetime(2026, 11, 23, 2, 57, tzinfo=timezone.utc)
        start = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
        for offset in (0, 30):
            db.add(Metric(
                machine_uuid="machine-fictive",
                device_uuid="device-fictif",
                ts=(start + timedelta(minutes=offset)).isoformat(),
                hashrate_hs=1_000_000.0,
                temp_c=60.0,
                power_w=100.0,
            ))
        db.commit()
        history._set_setting(db, history.AGG_CURSOR_KEY, start.isoformat())
        db.commit()

        result = history.aggregate(db, now=now)
        assert result["hour_buckets"] >= 1
        bucket = (
            db.query(MetricAgg)
            .filter(MetricAgg.bucket == "hour", MetricAgg.ts == start.isoformat())
            .first()
        )
        assert bucket is not None and bucket.samples == 2
        cutoff = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        assert history._get_setting(db, history.AGG_CURSOR_KEY) == cutoff.isoformat()
    finally:
        db.close()
