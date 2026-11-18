"""UX-1c : algos des coins de base, raisons des tuiles, plage de l'auto.

Ces tests éprouvent la règle demandée, pas l'écran : WAM mine en RandomX (le
premier catalogue disait yespower), la tuile muette porte sa raison, et
l'application automatique respecte la plage horaire.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_base_coin_algos():
    from app.coins import BASE_COINS, builtin_algo

    expected = {
        "PRL": "pearlhash",
        "KSH": "yespower",
        "WAM": "randomx",
        "BIG": "yespower",
    }
    assert {coin["ticker"]: coin["algo"] for coin in BASE_COINS} == expected
    for ticker, algo in expected.items():
        assert builtin_algo(ticker) == algo
    assert builtin_algo("inconnu") == ""
    # OMS n'est pas un coin de base : sans catalogue, l'algorithme est inconnu.
    assert builtin_algo("OMS") == ""


def test_seed_adds_missing_coins_and_fixes_wrong_algo(db):
    from app.coins import algo_for_coin, seed_builtin_coins
    from app.models import Coin

    db.query(Coin).delete()
    db.add(Coin(ticker="WAM", name="Wamcoin", algo="yespower", kind="both", pool_hosts=[]))
    db.commit()

    added = seed_builtin_coins(db)  # PRL, KSH, BIG créés ; WAM corrigé
    assert added == 3
    assert algo_for_coin(db, "WAM") == "randomx"
    assert seed_builtin_coins(db) == 0
    assert db.query(Coin).filter(Coin.ticker == "WAM").one().algo == "randomx"


def test_algo_for_coin_prefers_catalogue(db):
    from app.coins import algo_for_coin
    from app.models import Coin

    db.query(Coin).delete()
    db.add(Coin(ticker="XYZ", name="Xyz", algo="kawpow", kind="gpu", pool_hosts=[]))
    db.commit()
    assert algo_for_coin(db, "XYZ") == "kawpow"
    assert algo_for_coin(db, "PRL") == "pearlhash"  # table intégrée
    assert algo_for_coin(db, "") == ""


def test_energy_totals_carry_reason(db):
    from app.energy import build_report

    report = build_report(db, period="24h", quotes={})
    total = report["total"]
    assert total["cost_note"] == "aucune machine suivie"
    assert total["revenue_note"] == "prix inconnu"
    assert total["revenue_eur_day"] is None


async def test_auto_apply_honours_window(db, monkeypatch):
    from app import auto_update, update_checker, update_settings, updater_client
    from app.ai_config import set_setting

    calls: list[str] = []
    monkeypatch.setattr(update_checker, "status", lambda: {"latest_version": "2026.11.099"})
    monkeypatch.setattr(updater_client, "pending_request", lambda: None)

    def _request(db_, target):
        calls.append(target)
        return {"request": {"target": target}}

    monkeypatch.setattr(updater_client, "request_update", _request)
    set_setting(db, "update_auto_apply", True)
    db.commit()

    local = datetime.now(auto_update.PARIS) + timedelta(minutes=5)
    narrow = {
        "start": local.strftime("%H:%M"),
        "end": (local + timedelta(minutes=1)).strftime("%H:%M"),
        "timezone": "Europe/Paris",
    }
    auto_update.save(db, window=narrow)
    assert await update_settings.auto_apply_tick(db) is None
    assert calls == []

    auto_update.save(db, window={"start": "00:00", "end": "00:00", "timezone": "Europe/Paris"})
    assert await update_settings.auto_apply_tick(db) == "apply"
    assert calls == ["2026.11.099"]
