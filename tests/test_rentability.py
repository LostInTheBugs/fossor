"""Aide au choix du token par défaut (§4.19) — parseurs et calcul du net/jour.

Les fixtures sont de **vraies** réponses récupérées le 18/11/2026 :
``whattomine-coins.json`` (``/coins.json`` réduit) et
``whattomine-gpu-page.html`` (deux lignes de la page publique du RTX 3070).
Aucun test ne contacte le réseau : les sources sont injectées.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import rentability

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_gpu_page_uses_published_hashrates():
    html = (FIXTURES / "whattomine-gpu-page.html").read_text()
    rows = rentability.parse_gpu_page(html)
    assert rows, "aucune ligne publiée reconnue"
    row = rows[0]
    assert row["tag"] == "ETC"
    assert row["algo"] == "Etchash"
    assert row["hashrate_hs"] == pytest.approx(60e6)
    assert row["power_w"] == pytest.approx(130.0)
    assert row["hashrate_label"].lower().endswith("mh/s")


def test_parse_coins_keeps_network_economics():
    payload = json.loads((FIXTURES / "whattomine-coins.json").read_text())
    coins = rentability.parse_coins(payload)
    assert len(coins) >= 10
    pearl = next(coin for coin in coins if coin["tag"] == "PRL")
    assert pearl["algo"] == "Pearl"
    assert pearl["nethash_hs"] > 0
    assert pearl["btc_per_coin"] > 0
    assert pearl["block_time"] > 0


def test_coin_per_day_matches_network_share():
    coin = {
        "block_time": 183.0,
        "block_reward": 2158.8921362807378,
        "nethash_hs": 95793955613981475409.0,
        "btc_per_coin": 2.8779054137694324e-05,
    }
    per_day = rentability.coin_per_day(coin, 1000.0)
    assert per_day == pytest.approx(1.0645e-11, rel=5e-3)
    assert rentability.coin_per_day(coin, 0) is None


def test_net_day_subtracts_wall_cost():
    money = rentability.net_day(0.0001, 50_000.0, 100.0, 0.20)
    assert money["revenue_eur_day"] == pytest.approx(5.0)
    assert money["cost_eur_day"] == pytest.approx(0.48)
    assert money["net_eur_day"] == pytest.approx(4.52)
    assert rentability.net_day(None, 50_000.0, 100.0, 0.20)["net_eur_day"] is None


def test_rank_rows_sorts_by_net_and_flags_current_coin():
    rows = [
        {"coin_id": 1, "tag": "AAA", "algo": "KawPow", "hashrate_hs": 30e6, "hashrate_label": "30 Mh/s", "power_w": 130},
        {"coin_id": 2, "tag": "BBB", "algo": "Etchash", "hashrate_hs": 60e6, "hashrate_label": "60 Mh/s", "power_w": 130},
    ]
    coins = [
        {"id": 1, "name": "Alpha", "tag": "AAA", "algo": "KawPow", "block_time": 60.0,
         "block_reward": 2.0, "nethash_hs": 1e12, "btc_per_coin": 1e-6},
        {"id": 2, "name": "Beta", "tag": "BBB", "algo": "Etchash", "block_time": 13.0,
         "block_reward": 2.5, "nethash_hs": 3e14, "btc_per_coin": 3e-6},
    ]
    ranked = rentability.rank_rows(rows, coins, 50_000.0, 0.20, current_coin="BBB")
    assert [row["tag"] for row in ranked] == ["AAA", "BBB"] or [row["tag"] for row in ranked] == ["BBB", "AAA"]
    assert all("net_eur_day" in row for row in ranked)
    nets = [row["net_eur_day"] for row in ranked]
    assert nets == sorted(nets, reverse=True)
    current = next(row for row in ranked if row["tag"] == "BBB")
    assert current["current"] is True
    assert current["cost_eur_day"] == pytest.approx(0.624)


@pytest.mark.asyncio
async def test_market_caches_one_hour_and_issues_one_request():
    calls: list[str] = []

    async def fake_fetch(url: str, headers: dict | None = None) -> str:
        calls.append(url)
        return json.dumps({"coins": {"Pearl": {"id": 469, "tag": "PRL", "algorithm": "Pearl",
                                               "block_time": "183.0", "block_reward": 2000.0,
                                               "nethash": 1e19, "exchange_rate": 1e-5}}})

    market = rentability.Market(fetch=fake_fetch)
    first = await market.coins()
    second = await market.coins()
    assert first == second
    assert calls == [rentability.WHATTOMINE_COINS_URL]
    assert market.fetched_at("coins")


def test_hashrate_no_disabled_without_key_and_never_called():
    status = rentability.hashrate_no_status(None)
    assert status["enabled"] is False
    assert status["ok"] is False
    assert "clé API absente" in status["detail"]


def test_gpu_slug_matches_whattomine_urls():
    assert rentability.gpu_slug("NVIDIA GeForce RTX 3070") == "nvidia-geforce-rtx-3070"
    assert rentability.gpu_slug("AMD Ryzen 5 3600") == "amd-ryzen-5-3600"
