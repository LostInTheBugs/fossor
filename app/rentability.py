"""Aide au choix du token par défaut (§4.19).

Deux sources publiques, lues **côté serveur** et mises en cache une heure :

* **WhatToMine** — ``/coins.json`` donne l'économie de chaque coin (algorithme,
  temps de bloc, récompense, hashrate réseau en H/s, cours en BTC) et la page
  publique d'un modèle de GPU (``/gpus/<id>-<modele>``) publie ses hashrates
  par algorithme (« 60.00 Mh/s @ 130W »).
* **Hashrate.no** — API sous clé.  Sans clé enregistrée dans les réglages, la
  source est **désactivée** et aucune requête n'est émise; l'UI l'affiche.

Le revenu d'un device se calcule sans dépendre d'une unité d'affichage :

    coins/jour = hashrate_Hs / nethash_Hs × (86400 / block_time) × block_reward
    revenu_€   = coins/jour × exchange_rate_BTC × prix_BTC_€
    coût_€     = watts / 1000 × 24 × tarif_€/kWh          (§4.15)
    net_€      = revenu − coût

Aucune donnée réelle de l'installation ne sort : seuls des noms de modèles
publics sont envoyés à WhatToMine.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

WHATTOMINE_COINS_URL = "https://whattomine.com/coins.json"
WHATTOMINE_GPUS_URL = "https://whattomine.com/gpus"
WHATTOMINE_GPU_URL = "https://whattomine.com/gpus/{slug}"
HASHRATE_NO_GPUS_URL = "https://api.hashrate.no/v2/gpus"
USER_AGENT = "Fossor/0.1 (+https://github.com/LostInTheBugs/fossor)"
CACHE_TTL_S = 3600

#: Multiplicateurs des unités de hashrate affichées par WhatToMine.
UNITS: dict[str, float] = {
    "h": 1.0,
    "kh": 1e3,
    "mh": 1e6,
    "gh": 1e9,
    "th": 1e12,
    "ph": 1e15,
}

_HASHRATE_RE = re.compile(r"([0-9][0-9 .,]*)\s*([kKmMgGtTpP]?)[Hh]/s")

#: Une ligne publiée sur la page d'un GPU, avec son hashrate et sa conso.
_GPU_ROW_RE = re.compile(
    r'<a href="/coins/(\d+)-[^"]*">([^<]+)\(([A-Za-z0-9]+)\)</a><br>\s*'
    r"([^<]+?)\s*<span[^>]*>\s*([0-9][0-9.,]*)\s*([kKmMgGtTpP]?[Hh]/s)"
    r"\s*@\s*([0-9][0-9.,]*)\s*W",
    re.S,
)

_GPU_LINK_RE = re.compile(r"/gpus/(\d+)-([a-z0-9\-]+)")


def _float(value: Any) -> float | None:
    """``"1 234,5"``, ``"1234.5"``, ``"2.5"`` — nombre ou ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u202f", "").replace(" ", "")
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def parse_hashrate(text: str) -> tuple[float | None, str]:
    """``"60.00 Mh/s"`` -> ``(6e7, "60.00 Mh/s")``; ``("", "")`` si absent."""
    match = _HASHRATE_RE.search(str(text or ""))
    if not match:
        return None, ""
    number = _float(match.group(1).replace(" ", ""))
    if number is None:
        return None, ""
    unit = (match.group(2) or "").lower()
    hashrate_hs = number * UNITS.get(f"{unit}h", 1.0)
    return hashrate_hs, str(text).strip()


def gpu_slug(model: str) -> str:
    """``"NVIDIA GeForce RTX 3070"`` -> ``"nvidia-geforce-rtx-3070"``."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(model or "").strip().lower())
    return slug.strip("-")


def parse_gpu_index(html: str) -> dict[str, str]:
    """Slug de modèle -> page WhatToMine (l'identifiant numérique est requis)."""
    out: dict[str, str] = {}
    for gpu_id, slug in _GPU_LINK_RE.findall(html or ""):
        out.setdefault(slug, WHATTOMINE_GPU_URL.format(slug=f"{gpu_id}-{slug}"))
    return out


def parse_gpu_page(html: str) -> list[dict[str, Any]]:
    """Hashrates publiés d'un modèle, une ligne par coin minable.

    Chaque ligne porte le hashrate **tel que publié** (unité comprise) et la
    consommation du modèle pour ce coin.
    """
    rows: list[dict[str, Any]] = []
    for match in _GPU_ROW_RE.findall(html or ""):
        coin_id, coin, tag, algo, number, unit, watts = match
        hashrate_hs, _ = parse_hashrate(f"{number} {unit}")
        rows.append(
            {
                "coin_id": int(coin_id),
                "coin": coin.strip(),
                "tag": tag.strip().upper(),
                "algo": " ".join(algo.split()),
                "hashrate_hs": hashrate_hs,
                "power_w": _float(watts),
                "hashrate_label": f"{number.strip()} {unit}",
            }
        )
    return rows


def parse_coins(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """``coins.json`` -> liste de coins avec leur économie (H/s, BTC)."""
    coins = (payload or {}).get("coins") or payload or {}
    out: list[dict[str, Any]] = []
    for name, data in coins.items():
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "key": name,
                "id": data.get("id"),
                "name": data.get("name") or name,
                "tag": str(data.get("tag") or "").upper(),
                "algo": data.get("algorithm") or "",
                "block_time": _float(data.get("block_time")),
                "block_reward": _float(data.get("block_reward24") or data.get("block_reward")),
                "nethash_hs": _float(data.get("nethash")),
                "btc_per_coin": _float(data.get("exchange_rate24") or data.get("exchange_rate")),
                "revenue_btc_day": _float(data.get("btc_revenue")),
                "timestamp": data.get("timestamp"),
            }
        )
    return out


def coin_per_day(coin: dict[str, Any], hashrate_hs: float | None) -> float | None:
    """Coins minés par jour pour un hashrate donné (H/s)."""
    if not hashrate_hs or hashrate_hs <= 0:
        return None
    nethash = coin.get("nethash_hs")
    block_time = coin.get("block_time")
    reward = coin.get("block_reward")
    if not nethash or not block_time or reward is None:
        return None
    if nethash <= 0 or block_time <= 0:
        return None
    return (hashrate_hs / nethash) * (86400.0 / block_time) * reward


def net_day(
    btc_per_day: float | None,
    btc_eur: float | None,
    power_w: float | None,
    tariff_eur_kwh: float | None,
) -> dict[str, float | None]:
    """Revenu, coût électricité et net journaliers (§4.15)."""
    revenue = None
    if btc_per_day is not None and btc_eur:
        revenue = btc_per_day * btc_eur
    cost = None
    if power_w is not None and tariff_eur_kwh is not None:
        cost = (float(power_w) / 1000.0) * 24.0 * float(tariff_eur_kwh)
    net = None
    if revenue is not None and cost is not None:
        net = revenue - cost
    return {
        "revenue_eur_day": None if revenue is None else round(revenue, 4),
        "cost_eur_day": None if cost is None else round(cost, 4),
        "net_eur_day": None if net is None else round(net, 4),
    }


def rank_rows(
    rows: list[dict[str, Any]],
    coins: list[dict[str, Any]],
    btc_eur: float | None,
    tariff: float | None,
    current_coin: str = "",
    power_override: float | None = None,
) -> list[dict[str, Any]]:
    """Classe des lignes (coin + hashrate) par net/jour décroissant."""
    by_id = {coin.get("id"): coin for coin in coins if coin.get("id") is not None}
    by_tag = {coin.get("tag"): coin for coin in coins if coin.get("tag")}
    out: list[dict[str, Any]] = []
    for row in rows:
        coin = by_id.get(row.get("coin_id")) or by_tag.get(row.get("tag"))
        if not coin:
            continue
        hashrate_hs = row.get("hashrate_hs")
        power = power_override if power_override is not None else row.get("power_w")
        per_day = coin_per_day(coin, hashrate_hs)
        btc_day = None
        if per_day is not None and coin.get("btc_per_coin"):
            btc_day = per_day * float(coin["btc_per_coin"])
        money = net_day(btc_day, btc_eur, power, tariff)
        out.append(
            {
                "coin": coin.get("name"),
                "tag": coin.get("tag"),
                "algo": coin.get("algo") or row.get("algo") or "",
                "hashrate_hs": hashrate_hs,
                "hashrate_label": row.get("hashrate_label") or "",
                "power_w": power,
                "coins_day": None if per_day is None else round(per_day, 6),
                "btc_day": None if btc_day is None else float(f"{btc_day:.10g}"),
                **money,
                "current": bool(current_coin) and str(coin.get("tag") or "").upper() == current_coin.upper(),
            }
        )
    out.sort(key=lambda item: (item["net_eur_day"] is None, -(item["net_eur_day"] or 0.0)))
    for index, item in enumerate(out, start=1):
        item["rank"] = index
    return out


def proposal(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Meilleur net positif : la proposition de token par défaut."""
    for row in rows:
        if row.get("net_eur_day") is not None and row["net_eur_day"] > 0:
            return row
    return rows[0] if rows else None


class Market:
    """Récupération + cache mémoire (1 h) des sources publiques."""

    def __init__(
        self,
        fetch: Callable[[str, dict[str, str] | None], Awaitable[str]] | None = None,
        ttl: int = CACHE_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch or _http_get
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def _cached(self, key: str, loader: Callable[[], Awaitable[Any]]) -> Any:
        now = self._clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        async with self._lock:
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < self._ttl:
                return hit[1]
            value = await loader()
            self._cache[key] = (self._clock(), value)
            return value

    async def coins(self) -> list[dict[str, Any]]:
        async def loader() -> list[dict[str, Any]]:
            text = await self._fetch(WHATTOMINE_COINS_URL, {"Accept": "application/json"})
            return parse_coins(json.loads(text))

        return await self._cached("coins", loader)

    async def gpu_rows(self, model: str) -> list[dict[str, Any]]:
        slug = gpu_slug(model)
        # L'index est résolu **hors** du verrou de ``_cached`` : un ``asyncio.Lock``
        # n'est pas réentrant, et l'appeler depuis le chargeur le bloquait à vie.
        index = await self._cached("gpu-index", lambda: self._fetch(WHATTOMINE_GPUS_URL, None))
        url = parse_gpu_index(index).get(slug)
        if not url:
            raise LookupError(f"modèle {model!r} absent de WhatToMine")

        async def loader() -> list[dict[str, Any]]:
            return parse_gpu_page(await self._fetch(url, None))

        return await self._cached(f"gpu:{slug}", loader)

    def fetched_at(self, key: str) -> str | None:
        hit = self._cache.get(key)
        if not hit:
            return None
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hit[0]))

    def clear(self) -> None:
        self._cache.clear()


async def _http_get(url: str, headers: dict[str, str] | None = None) -> str:
    import httpx

    merged = {"User-Agent": USER_AGENT}
    merged.update(headers or {})
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        response = await client.get(url, headers=merged)
        response.raise_for_status()
        return response.text


def hashrate_no_status(db=None) -> dict[str, Any]:
    """État de la source Hashrate.no — désactivée sans clé, sans requête."""
    key = ""
    if db is not None:
        from app.ai_config import get_setting

        key = str(get_setting(db, "hashrate_no_api_key", "") or "").strip()
    if not key:
        return {
            "name": "Hashrate.no",
            "url": "https://hashrate.no",
            "enabled": False,
            "ok": False,
            "detail": "désactivée : clé API absente (réglage chiffré hashrate_no_api_key)",
        }
    return {
        "name": "Hashrate.no",
        "url": "https://hashrate.no",
        "enabled": True,
        "ok": True,
        "detail": "clé enregistrée — source disponible",
    }


async def fetch_hashrate_no_gpus(api_key: str) -> list[dict[str, Any]]:
    """Liste des GPU publiée par Hashrate.no (appelée seulement avec une clé)."""
    text = await _http_get(HASHRATE_NO_GPUS_URL, {"x-api-key": api_key})
    payload = json.loads(text)
    return payload if isinstance(payload, list) else list(payload.values())
