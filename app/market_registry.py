"""Token market registry and legacy amount parsing.

Ported from the v2.21 mining page (``MARKET_REG`` + ``parseMined``), which is the
functional reference for §4.3 / §6 écran 3.

Rules kept from the reference:

* a CoinGecko id is only declared here when it is **the same coin** — ``wam`` on
  CoinGecko is wam.app, not WAM Coin, so WAM has none;
* WAM trades on a DEX by atomic swap only (no reference price) and QWC is not
  listed anywhere: both stay unpriced instead of showing a fake value.

Amount parsing handles the free-text ``history[*].mined`` strings, e.g.
``"6 949,67 JTM en chaine … = 6 999,33 JTM"`` → ``6999.33`` (the value after the
last ``=`` wins), ``"2 379,25 LMT"`` → ``2379.25``, ``"0 JTM"`` → ``0.0``.
"""

from __future__ import annotations

import re
from typing import Any

# ticker -> market metadata (cg = CoinGecko id, verified by hand)
MARKET_REG: dict[str, dict[str, Any]] = {
    "PRL": {
        "name": "Pearl",
        "cg": "pearl-2",
        "cg_verified": True,
        "kind": "gpu",
        "exchanges": [
            {"name": "SafeTrade", "url": "https://safetrade.com/exchange/PRL-USDT", "pair": "PRL/USDT"},
            {"name": "BigONE", "url": "https://big.one/trade/PRL-USDT", "pair": "PRL/USDT"},
        ],
    },
    "QTC": {
        "name": "Quantus",
        "cg": "quantus",
        "cg_verified": True,
        "kind": "cpu",
        "exchanges": [
            {"name": "SafeTrade", "url": "https://safetrade.com/exchange/QUANTUS-USDT", "pair": "QUANTUS/USDT"},
        ],
    },
    "WAM": {
        "name": "WAM Coin",
        "cg": None,
        "cg_verified": False,
        "kind": "cpu",
        "exchanges": [
            {"name": "GLEEC DEX", "url": "https://dex.gleec.com/", "pair": "atomic swap", "dex": True},
        ],
        "note": "DEX atomic swap seulement, pas de prix de référence ; listing Bisq en cours",
    },
    "QWC": {
        "name": "QWC",
        "cg": None,
        "cg_verified": False,
        "kind": "cpu",
        "exchanges": [],
        "note": "Plus coté nulle part (dernier prix connu 2023, place de marché fermée)",
    },
}


def registry_for(ticker: str) -> dict[str, Any]:
    """Market metadata of *ticker* (empty dict when unknown)."""
    return dict(MARKET_REG.get((ticker or "").strip().upper(), {}))


# A number with French grouping: "6 949,67", "4300,00132350", "1 234.56".
_NUM = r"\d[\d\s\u00a0\u202f\u2009.,]*"


def to_float(raw: str | None) -> float | None:
    """Parse a legacy amount (spaces / comma / dot) into a float."""
    if raw is None:
        return None
    text = str(raw)
    for space in ("\u00a0", "\u202f", "\u2009", " ", "\u2007"):
        text = text.replace(space, "")
    text = text.strip().rstrip(".,")
    if not text or not any(ch.isdigit() for ch in text):
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    if text.count(".") > 1:  # "1.234.567,89" leftovers
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        return float(text)
    except ValueError:
        return None


def parse_mined_amount(text: str | None, ticker: str = "") -> float | None:
    """Extract the mined amount from a legacy ``mined`` sentence."""
    if text is None:
        return None
    sentence = str(text).strip()
    if not sentence:
        return None

    # 1. "… = 6 999,33 JTM" — the sum after the last '=' is the retained value.
    if "=" in sentence:
        tail = sentence.rsplit("=", 1)[1]
        match = re.match(r"\s*(" + _NUM + r")\s*([A-Za-z]{2,10})?", tail)
        if match:
            value = to_float(match.group(1))
            if value is not None:
                return value

    # 2. "<number> <TICKER>"
    symbol = (ticker or "").strip()
    if symbol:
        match = re.search(r"(" + _NUM + r")\s*" + re.escape(symbol) + r"\b", sentence, re.IGNORECASE)
        if match:
            value = to_float(match.group(1))
            if value is not None:
                return value

    # 3. leading number ("59 BTC (…)", "0 CMFD (…)").
    match = re.match(r"\s*(" + _NUM + r")", sentence)
    if match:
        return to_float(match.group(1))
    return None


_PATH_RE = re.compile(r"(?:~|/)[^\s,;()]+")


def clean_backup_path(raw: str | None) -> str:
    """Keep only path-looking tokens of a legacy ``wallet_path`` sentence.

    The legacy text also mentions where the seed/password live; §4.4 forbids
    storing anything but the path, so every non-path word is dropped.
    """
    if not raw:
        return ""
    paths: list[str] = []
    for token in _PATH_RE.findall(str(raw)):
        lowered = token.lower()
        if "seed" in lowered or "pass" in lowered or "phrase" in lowered:
            continue
        token = token.rstrip(".,;)")
        if token and token not in paths:
            paths.append(token)
    return " ; ".join(paths)


def guess_kind(*texts: str | None) -> str:
    """Infer ``cpu`` / ``gpu`` / ``both`` from the legacy free text."""
    blob = " ".join(t for t in texts if t).lower()
    cpu = any(word in blob for word in ("cpu", "thread", "xmr", "randomx", "yespower 1.0 cpu"))
    gpu = any(word in blob for word in ("gpu", "rtx", "cuda", "pearlhash", "nvidia", "rocm"))
    if cpu and not gpu:
        return "cpu"
    if gpu and not cpu:
        return "gpu"
    return "both"
