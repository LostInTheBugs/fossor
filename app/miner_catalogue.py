"""Catalogue versionné des mineurs de minage **standard** (étape 6.4).

Les listes d'algorithmes ne sont pas devinées : chaque logiciel porte l'URL de
sa source officielle (documentation ou README du dépôt des releases) et la date
de relevé.  Le catalogue est la référence pour :

* l'**identification** d'un token (``standard`` / ``variante`` / ``propriétaire``) ;
* les **profils types** (logiciel + algorithme + gabarit d'arguments + port d'API) ;
* le **mode de campagne** : un algorithme standard mine sur l'hôte (mode
  ``hybrid``), un algorithme variante ou propriétaire reste isolé en VM (``vm``).

Les données vivent dans le code ; ``Configuration → Spec mining`` peut les
remplacer par une version éditée (réglage ``miner_catalogue``), validée à
l'enregistrement.  Aucun téléchargement ni lancement ici : ce module est pur.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

CATALOGUE_VERSION = "2026.11.22-1"
RETRIEVED = "2026-11-22"

#: Sources officielles relevées le 2026-11-22 (README/doc des releases).
SOURCES: dict[str, str] = {
    "xmrig": "https://xmrig.com/docs/algorithms (README github.com/xmrig/xmrig)",
    "cpuminer-opt": "https://github.com/JayDDee/cpuminer-opt (README, master)",
    "srbminer": "https://github.com/doktor83/SRBMiner-Multi (README, master)",
    "lolminer": "https://github.com/Lolliedieb/lolMiner-releases (README, master)",
    "t-rex": "https://github.com/trexminer/T-Rex (README, master)",
    "rigel": "https://github.com/rigelminer/rigel (README, master)",
    "bzminer": "https://github.com/bzminer/bzminer (README, main)",
    "gminer": "https://github.com/develsoftware/GMinerRelease (README, master)",
}

_CPU = ["cpu"]
_GPU = ["gpu"]
_BOTH = ["cpu", "gpu"]


def _algo(name: str, kind: str, family: str, aliases: list[str] | None = None,
          note: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,          # cpu | gpu
        "family": family,      # famille de rapprochement (randomx, yespower…)
        "aliases": aliases or [],
        "note": note,
    }


_ALGOS: dict[str, list[dict[str, Any]]] = {
    "xmrig": [
        _algo("rx/0", "cpu", "randomx", ["randomx", "monero", "xmr"]),
        _algo("rx/wow", "cpu", "randomx", ["randomwow", "wownero"]),
        _algo("rx/arq", "cpu", "randomx", ["randomarq", "arqma"]),
        _algo("rx/sfx", "cpu", "randomx", ["randomsfx", "safex"]),
        _algo("rx/graft", "cpu", "randomx", ["randomgraft", "graft"]),
        _algo("rx/keva", "cpu", "randomx", ["randomkeva", "keva"],
              "retiré des versions récentes de XMRig (CHANGELOG)"),
        _algo("rx/yada", "cpu", "randomx", ["randomyada", "yada"],
              "ajouté par XMRig (CHANGELOG, PR #2411)"),
        _algo("gr", "cpu", "ghostrider", ["ghostrider", "raptoreum"]),
        _algo("kawpow", "gpu", "kawpow", ["rvn"]),
        _algo("cn/0", "cpu", "cryptonight", ["cryptonight"]),
        _algo("cn/1", "cpu", "cryptonight"),
        _algo("cn/2", "cpu", "cryptonight"),
        _algo("cn/r", "cpu", "cryptonight", ["cryptonightr"]),
        _algo("cn/fast", "cpu", "cryptonight"),
        _algo("cn/half", "cpu", "cryptonight"),
        _algo("cn/xao", "cpu", "cryptonight"),
        _algo("cn/rto", "cpu", "cryptonight"),
        _algo("cn/rwz", "cpu", "cryptonight"),
        _algo("cn/zls", "cpu", "cryptonight"),
        _algo("cn/double", "cpu", "cryptonight"),
        _algo("cn/ccx", "cpu", "cryptonight", ["conceal"]),
        _algo("cn/upx2", "cpu", "cryptonight"),
        _algo("cn-heavy/0", "cpu", "cryptonight"),
        _algo("cn-heavy/xhv", "cpu", "cryptonight"),
        _algo("cn-heavy/tube", "cpu", "cryptonight"),
        _algo("cn-lite/0", "cpu", "cryptonight"),
        _algo("cn-lite/1", "cpu", "cryptonight"),
        _algo("cn-pico", "cpu", "cryptonight"),
        _algo("cn-pico/tlo", "cpu", "cryptonight"),
        _algo("argon2/chukwa", "cpu", "argon2"),
        _algo("argon2/chukwav2", "cpu", "argon2"),
        _algo("argon2/ninja", "cpu", "argon2"),
    ],
    "cpuminer-opt": [
        _algo("yespower", "cpu", "yespower", ["cryply", "cpupower"]),
        _algo("yespowerr16", "cpu", "yespower", ["yenten"]),
        _algo("yespower-b2b", "cpu", "yespower", ["power2b"],
              "power2b : --param-n 2048 --param-r 32 --param-key …"),
        _algo("power2b", "cpu", "power2b", ["microbitcoin"]),
        _algo("yescrypt", "cpu", "yescrypt", ["globalboost"]),
        _algo("yescryptr8", "cpu", "yescrypt", ["bitzeny"]),
        _algo("yescryptr8g", "cpu", "yescrypt", ["koto"]),
        _algo("yescryptr16", "cpu", "yescrypt", ["eli"]),
        _algo("yescryptr32", "cpu", "yescrypt", ["wavi"]),
        _algo("argon2d250", "cpu", "argon2"),
        _algo("argon2d500", "cpu", "argon2"),
        _algo("argon2d1000", "cpu", "argon2"),
        _algo("argon2d4096", "cpu", "argon2"),
    ],
    "srbminer": [
        _algo("argon2d_16000", "both", "argon2"),
        _algo("argon2d_dynamic", "both", "argon2"),
        _algo("autolykos2", "gpu", "autolykos2", ["ergo"]),
        _algo("blake3_decred", "gpu", "blake3", ["decred"]),
        _algo("cpupower", "cpu", "yespower"),
        _algo("cryptonight_gpu", "both", "cryptonight", ["cn/gpu"]),
        _algo("cryptonight_turtle", "both", "cryptonight"),
        _algo("curvehash", "both", "curvehash"),
        _algo("etchash", "gpu", "etchash", ["etc"]),
        _algo("ethash", "gpu", "ethash", ["eth", "ethw"]),
        _algo("evrprogpow", "gpu", "progpow"),
        _algo("firopow", "gpu", "firopow"),
        _algo("fishhash", "gpu", "fishhash", ["ironfish"]),
        _algo("flex", "cpu", "flex"),
        _algo("ghostrider", "cpu", "ghostrider", ["raptoreum"]),
        _algo("heavyhash", "gpu", "heavyhash"),
        _algo("kawpow", "gpu", "kawpow", ["rvn"]),
        _algo("lyra2v2_webchain", "both", "lyra2"),
        _algo("meowpow", "gpu", "meowpow"),
        _algo("mike", "cpu", "mike"),
        _algo("minotaurx", "cpu", "minotaurx"),
        _algo("noid", "gpu", "noid"),
        _algo("oggpow", "gpu", "oggpow"),
        _algo("panthera", "cpu", "panthera"),
        _algo("pearlhash", "gpu", "pearlhash", ["pearl"]),
        _algo("phihash", "gpu", "phihash"),
        _algo("progpow_epic", "gpu", "progpow"),
        _algo("progpow_zano", "gpu", "progpow"),
        _algo("qhash", "gpu", "qhash"),
        _algo("quantus", "gpu", "quantus"),
        _algo("randomalpha", "cpu", "randomx"),
        _algo("randomarq", "cpu", "randomx"),
        _algo("randomc64", "cpu", "randomx"),
        _algo("randomdrgx", "cpu", "randomx"),
        _algo("randomepic", "cpu", "randomx"),
        _algo("randomjuno", "cpu", "randomx"),
        _algo("randompcn", "cpu", "randomx"),
        _algo("randomscash", "cpu", "randomx"),
        _algo("randomsnap", "cpu", "randomx"),
        _algo("randomvirel", "cpu", "randomx"),
        _algo("randomx", "cpu", "randomx", ["rx/0", "monero"]),
        _algo("randomy", "cpu", "randomx"),
        _algo("randomyada", "cpu", "randomx", ["rx/yada"]),
        _algo("randomzqvx", "cpu", "randomx"),
        _algo("rinhash", "cpu", "rinhash"),
        _algo("sha256d_csd", "gpu", "sha256d"),
        _algo("verthash", "gpu", "verthash"),
        _algo("verushash", "cpu", "verushash"),
        _algo("xelishashv3", "both", "xelishash"),
        _algo("xhash", "gpu", "xhash"),
        _algo("yescrypt", "both", "yescrypt"),
        _algo("yescryptr16", "both", "yescrypt"),
        _algo("yescryptr32", "both", "yescrypt"),
        _algo("yescryptr8", "both", "yescrypt"),
        _algo("yespower", "both", "yespower"),
        _algo("yespower2b", "cpu", "yespower", ["power2b"]),
        _algo("yespoweradvc", "cpu", "yespower"),
        _algo("yespowereqpay", "cpu", "yespower"),
        _algo("yespowerinterchained", "cpu", "yespower"),
        _algo("yespowerltncg", "cpu", "yespower"),
        _algo("yespowermgpc", "cpu", "yespower"),
        _algo("yespowermwc", "cpu", "yespower"),
        _algo("yespowerr16", "cpu", "yespower"),
        _algo("yespowersugar", "cpu", "yespower"),
        _algo("yespowertide", "both", "yespower"),
        _algo("yespowerurx", "cpu", "yespower"),
    ],
    "lolminer": [
        _algo("alephium", "gpu", "alephium", ["alph"]),
        _algo("autolykos2", "gpu", "autolykos2", ["ergo"]),
        _algo("beamhash3", "gpu", "beamhash", ["beam"]),
        _algo("cuckoo29", "gpu", "cuckoo"),
        _algo("cuckarood29", "gpu", "cuckoo"),
        _algo("cuckaroom29", "gpu", "cuckoo"),
        _algo("cuckaroo30", "gpu", "cuckoo", ["ctxc"]),
        _algo("cuckatoo31", "gpu", "cuckatoo"),
        _algo("cuckatoo32", "gpu", "cuckatoo"),
        _algo("equihash144_5", "gpu", "equihash"),
        _algo("equihash192_7", "gpu", "equihash"),
        _algo("equihash210_9", "gpu", "equihash"),
        _algo("etchash", "gpu", "etchash", ["etc"]),
        _algo("ethash", "gpu", "ethash", ["eth", "ethw"]),
        _algo("ironfish", "gpu", "fishhash"),
        _algo("kaspa", "gpu", "kheavyhash"),
        _algo("karlsen", "gpu", "karlsenhash", ["kls"]),
        _algo("karlsenhashv2", "gpu", "karlsenhash"),
        _algo("nexa", "gpu", "nexa"),
        _algo("octopus", "gpu", "octopus", ["cfx"]),
        _algo("pyrin", "gpu", "pyrinhash"),
        _algo("pyrinv2", "gpu", "pyrinhash"),
        _algo("radiant", "gpu", "sha512256d", ["rxd"]),
        _algo("rethereum", "gpu", "rethereum"),
        _algo("sha3x", "gpu", "sha3x"),
        _algo("ton", "gpu", "sha256ton", ["gram"]),
        _algo("zelhash", "gpu", "zelhash", ["flux"]),
    ],
    "t-rex": [
        _algo("autolykos2", "gpu", "autolykos2", ["ergo"]),
        _algo("etchash", "gpu", "etchash", ["etc"]),
        _algo("ethash", "gpu", "ethash", ["eth", "ethw"]),
        _algo("kawpow", "gpu", "kawpow", ["rvn"]),
        _algo("blake3", "gpu", "blake3", ["alephium", "alph"]),
        _algo("octopus", "gpu", "octopus", ["cfx"]),
        _algo("firopow", "gpu", "firopow", ["firo"]),
    ],
    "rigel": [
        _algo("abelian", "gpu", "abelian"),
        _algo("alephium", "gpu", "alephium", ["alph"]),
        _algo("autolykos2", "gpu", "autolykos2", ["ergo"]),
        _algo("etchash", "gpu", "etchash", ["etc"]),
        _algo("ethash", "gpu", "ethash", ["ethw"]),
        _algo("fishhash", "gpu", "fishhash", ["ironfish"]),
        _algo("karlsenhashv2", "gpu", "karlsenhash", ["karlsen"]),
        _algo("kawpow", "gpu", "kawpow", ["rvn"]),
        _algo("nexapow", "gpu", "nexa"),
        _algo("octopus", "gpu", "octopus", ["cfx"]),
        _algo("progpowz", "gpu", "progpow"),
        _algo("sha256ton", "gpu", "sha256ton", ["gram"]),
        _algo("sha3x", "gpu", "sha3x"),
        _algo("sha512256d", "gpu", "sha512256d", ["radiant"]),
        _algo("xelishashv3", "gpu", "xelishash"),
    ],
    "bzminer": [
        _algo("bramring", "gpu", "bramring"),
        _algo("c29", "gpu", "cuckoo", ["tari"]),
        _algo("cn/gpu", "both", "cryptonight", ["cn", "conceal", "ryo"]),
        _algo("ergo", "gpu", "autolykos2", ["autolykos2"]),
        _algo("etchash", "gpu", "etchash", ["etc"]),
        _algo("ethash", "gpu", "ethash", ["ethw", "ethereumpow"]),
        _algo("kawpow", "gpu", "kawpow", ["rvn", "xna", "neoxa", "meowcoin", "clore"]),
        _algo("nexa", "gpu", "nexa"),
        _algo("pearlhash", "gpu", "pearlhash", ["pearl"]),
        _algo("quantus", "both", "quantus"),
        _algo("randomx", "cpu", "randomx", ["rx/0", "xmr", "monero", "zeph", "sal"]),
        _algo("sha256d", "both", "sha256d"),
        _algo("verus", "cpu", "verushash"),
        _algo("warthog", "both", "warthog"),
        _algo("xelis", "both", "xelis"),
    ],
    "gminer": [
        _algo("ethash", "gpu", "ethash", ["eth"]),
        _algo("etchash", "gpu", "etchash", ["etc"]),
        _algo("kawpow", "gpu", "kawpow", ["rvn"]),
        _algo("autolykos2", "gpu", "autolykos2", ["ergo"]),
        _algo("kheavyhash", "gpu", "kheavyhash", ["kaspa"]),
        _algo("cortex", "gpu", "cortex"),
        _algo("beamhash", "gpu", "beamhash", ["beam"]),
        _algo("equihash144_5", "gpu", "equihash"),
        _algo("equihash125_4", "gpu", "equihash"),
    ],
}

_SOFTWARE_META: dict[str, dict[str, Any]] = {
    "xmrig": {
        "name": "XMRig", "platforms": _BOTH,
        "release_url": "https://github.com/xmrig/xmrig/releases",
        "api_port": 18080, "api_kind": "xmrig",
        "cmdline": "{binary} -o {pool} -u {wallet} -p x -a {algo} --rig-id {worker}"
                   " --http-host 127.0.0.1 --http-port {api_port} --no-color",
    },
    "cpuminer-opt": {
        "name": "cpuminer-opt", "platforms": _CPU,
        "release_url": "https://github.com/JayDDee/cpuminer-opt/releases",
        "api_port": 18081, "api_kind": "cpuminer",
        "cmdline": "{binary} -a {algo} -o {pool} -u {wallet} -p x"
                   " --api-bind 127.0.0.1:{api_port}",
    },
    "srbminer": {
        "name": "SRBMiner-Multi", "platforms": _BOTH,
        "release_url": "https://github.com/doktor83/SRBMiner-Multi/releases",
        "api_port": 18082, "api_kind": "srbminer",
        "cmdline": "{binary} --algorithm {algo} --pool {pool} --wallet {wallet}"
                   " --worker {worker} --api-enable --api-port {api_port}",
    },
    "lolminer": {
        "name": "lolMiner", "platforms": _GPU,
        "release_url": "https://github.com/Lolliedieb/lolMiner-releases/releases",
        "api_port": 18083, "api_kind": "lolminer",
        "cmdline": "{binary} --algo {algo} --pool {pool} --user {wallet}.{worker}"
                   " --apiport {api_port}",
    },
    "t-rex": {
        "name": "T-Rex", "platforms": _GPU,
        "release_url": "https://github.com/trexminer/T-Rex/releases",
        "api_port": 18084, "api_kind": "trex",
        "cmdline": "{binary} -a {algo} -o {pool} -u {wallet} -p x -w {worker}"
                   " --api-bind 127.0.0.1:{api_port}",
    },
    "rigel": {
        "name": "Rigel", "platforms": _GPU,
        "release_url": "https://github.com/rigelminer/rigel/releases",
        "api_port": 18085, "api_kind": "rigel",
        "cmdline": "{binary} -a {algo} -o {pool} -u {wallet} -w {worker}"
                   " --api-bind 127.0.0.1:{api_port}",
    },
    "bzminer": {
        "name": "bzMiner", "platforms": _BOTH,
        "release_url": "https://github.com/bzminer/bzminer/releases",
        "api_port": 18086, "api_kind": "bzminer",
        "cmdline": "{binary} -a {algo} -p {pool} -w {wallet} --worker {worker}"
                   " --http-port {api_port}",
    },
    "gminer": {
        "name": "GMiner", "platforms": _GPU,
        "release_url": "https://github.com/develsoftware/GMinerRelease/releases",
        "api_port": 18087, "api_kind": "gminer",
        "cmdline": "{binary} --algo {algo} --server {pool} --user {wallet}"
                   " --worker {worker} --api {api_port}",
    },
}


def _build() -> dict[str, Any]:
    softwares: list[dict[str, Any]] = []
    for software_id, meta in _SOFTWARE_META.items():
        softwares.append(
            {
                "id": software_id,
                **meta,
                "source": SOURCES[software_id],
                "retrieved": RETRIEVED,
                "algos": copy.deepcopy(_ALGOS[software_id]),
            }
        )
    return {
        "version": CATALOGUE_VERSION,
        "retrieved": RETRIEVED,
        "note": "Algles relevés dans la documentation officielle de chaque mineur ; "
                "aucune liste devinée. Modifiable dans Configuration → Spec mining.",
        "softwares": softwares,
    }


CATALOGUE: dict[str, Any] = _build()


def catalogue() -> dict[str, Any]:
    """Catalogue intégré (copie : l'appelant ne peut pas le muter)."""
    return copy.deepcopy(CATALOGUE)


def _norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "algo"


def software_ids() -> list[str]:
    return [item["id"] for item in CATALOGUE["softwares"]]


def algorithm_count() -> int:
    return sum(len(item["algos"]) for item in CATALOGUE["softwares"])


def _entries() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for software in CATALOGUE["softwares"]:
        for algo in software["algos"]:
            rows.append((software, algo))
    return rows


def summary() -> dict[str, Any]:
    """Vue compacte du catalogue pour le contexte Hermes."""
    return {
        "version": CATALOGUE["version"],
        "retrieved": CATALOGUE["retrieved"],
        "algorithm_count": algorithm_count(),
        "softwares": [
            {
                "id": item["id"],
                "name": item["name"],
                "source": item["source"],
                "release_url": item["release_url"],
                "algo_count": len(item["algos"]),
            }
            for item in CATALOGUE["softwares"]
        ],
        "algorithms": [
            {"software": software["id"], "algo": algo["name"], "kind": algo["kind"], "family": algo["family"]}
            for software, algo in _entries()
        ],
    }


def lookup(raw_algo: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Entrées (logiciel, algo) qui portent exactement ce nom ou un alias."""
    wanted = _norm(raw_algo)
    if not wanted:
        return []
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for software, algo in _entries():
        names = [algo["name"], *algo.get("aliases", [])]
        if any(_norm(name) == wanted for name in names):
            found.append((software, algo))
    return found


#: Ordre de préférence par famille pour le profil type.
_PREFERENCE: dict[str, list[str]] = {
    "randomx": ["xmrig", "bzminer", "srbminer"],
    "cryptonight": ["xmrig", "srbminer", "bzminer"],
    "argon2": ["xmrig", "cpuminer-opt", "srbminer"],
    "ghostrider": ["xmrig", "srbminer"],
    "yespower": ["cpuminer-opt", "srbminer"],
    "yescrypt": ["cpuminer-opt", "srbminer"],
    "power2b": ["cpuminer-opt", "srbminer"],
    "etchash": ["lolminer", "t-rex", "rigel", "gminer", "bzminer", "srbminer"],
    "ethash": ["lolminer", "t-rex", "rigel", "gminer", "bzminer", "srbminer"],
    "kawpow": ["t-rex", "rigel", "gminer", "bzminer", "srbminer"],
    "autolykos2": ["lolminer", "t-rex", "rigel", "gminer", "bzminer", "srbminer"],
    "karlsenhash": ["lolminer", "rigel", "srbminer"],
    "pearlhash": ["bzminer", "srbminer"],
    "kheavyhash": ["lolminer", "gminer", "srbminer"],
}
_DEFAULT_ORDER = list(_SOFTWARE_META)


def _rank(family: str, software_id: str) -> tuple[int, int]:
    order = _PREFERENCE.get(family, _DEFAULT_ORDER)
    if software_id in order:
        return (0, order.index(software_id))
    return (1, _DEFAULT_ORDER.index(software_id) if software_id in _DEFAULT_ORDER else 99)


def _template(software: dict[str, Any], algo: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{software['id']}-{_slug(algo['name'])}",
        "name": f"{software['name']} · {algo['name']}",
        "software": software["id"],
        "software_name": software["name"],
        "algo": algo["name"],
        "family": algo["family"],
        "kind": algo["kind"],
        "cmdline": software["cmdline"],
        "api_port": software["api_port"],
        "api_kind": software["api_kind"],
        "binary_url": software["release_url"],
        "source": software["source"],
        "retrieved": software["retrieved"],
    }


def profile_templates() -> list[dict[str, Any]]:
    """Profils types : un par couple (logiciel, algorithme)."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for software, algo in _entries():
        template = _template(software, algo)
        if template["id"] in seen:  # collisions de slug : suffixe stable
            index = 2
            while f"{template['id']}-{index}" in seen:
                index += 1
            template["id"] = f"{template['id']}-{index}"
        seen.add(template["id"])
        rows.append(template)
    return rows


def template_for(raw_algo: Any) -> dict[str, Any] | None:
    """Meilleur profil type pour cet algorithme (None si aucun mineur standard)."""
    matches = lookup(raw_algo)
    if not matches:
        return None
    ranked = sorted(matches, key=lambda item: (_rank(item[1]["family"], item[0]["id"]), item[1]["name"]))
    software, algo = ranked[0]
    return _template(software, algo)


def _family_guess(raw_algo: str) -> str:
    text = _norm(raw_algo)
    if re.search(r"(^|/)rx(/|-)|randomx|random[a-z]", text):
        return "randomx"
    if "yespower" in text or text.startswith("cpupower"):
        return "yespower"
    if "yescrypt" in text:
        return "yescrypt"
    if "power2b" in text:
        return "power2b"
    if "cryptonight" in text or re.search(r"(^|/)cn([/-]|$)", text):
        return "cryptonight"
    if "argon" in text:
        return "argon2"
    if "etchash" in text:
        return "etchash"
    if "ethash" in text:
        return "ethash"
    if "kawpow" in text:
        return "kawpow"
    if "autolykos" in text:
        return "autolykos2"
    if "karlsen" in text:
        return "karlsenhash"
    if "pearl" in text:
        return "pearlhash"
    if "yespower" in text:
        return "yespower"
    return ""


def _miners_for_family(family: str, raw_algo: str) -> list[dict[str, Any]]:
    """Mineurs de la même famille, avec l'algorithme exact qu'ils exposent."""
    rows: list[dict[str, Any]] = []
    for software, algo in _entries():
        if algo["family"] != family:
            continue
        rows.append(
            {
                "software": software["id"],
                "software_name": software["name"],
                "algo": algo["name"],
                "kind": algo["kind"],
                "template": _template(software, algo),
            }
        )
    rows.sort(key=lambda item: (_rank(family, item["software"]), item["algo"]))
    return rows


def identify(raw_algo: Any, own_miner: bool = False) -> dict[str, Any]:
    """Identifie un algorithme annoncé : standard, variante ou propriétaire.

    * ``standard`` : nom exact du catalogue → la liste des mineurs compatibles
      est factuelle ;
    * ``variante`` : famille connue mais nom/paramètres non documentés — le
      projet publie souvent son propre mineur, aucun mineur standard n'est
      annoncé compatible sans preuve ;
    * ``propriétaire`` : algorithme inconnu **et** mineur du projet publié.
    """
    text = str(raw_algo or "").strip()
    matches = lookup(text)
    if matches:
        exact = any(_norm(algo["name"]) == _norm(text) for _software, algo in matches)
        family = matches[0][1]["family"]
        # RandomX/CryptoNight/yespower ont des variantes aux paramètres propres :
        # un simple nom de famille + mineur du projet ne prouve pas rx/0.
        ambiguous = family in ("randomx", "cryptonight", "yespower", "yescrypt", "argon2")
        if own_miner and (not exact or (ambiguous and "/" not in text)):
            return {
                "algo": text,
                "classification": "variante",
                "family": family,
                "miners": _miners_for_family(family, text),
                "miners_compatible": False,
                "template": None,
                "reason": f"« {text} » n'est qu'un nom de famille (pas un algorithme exact) et "
                          "le projet publie son propre mineur : paramètres non prouvés.",
            }
        miners = _miners_for_family(family, text)
        template = template_for(text)
        names = sorted({item["software_name"] for item in miners})
        return {
            "algo": text,
            "classification": "standard",
            "family": family,
            "miners": miners,
            "miners_compatible": True,
            "template": template,
            "reason": "algorithme documenté par " + ", ".join(names) + ".",
        }
    family = _family_guess(text)
    if family:
        miners = _miners_for_family(family, text)
        return {
            "algo": text,
            "classification": "variante",
            "family": family,
            "miners": miners,
            "miners_compatible": False,
            "template": None,
            "reason": f"famille « {family} » reconnue, mais « {text} » n'est pas un nom "
                      "documenté : paramètres probablement modifiés (variante).",
        }
    if own_miner:
        return {
            "algo": text,
            "classification": "proprietaire",
            "family": "",
            "miners": [],
            "miners_compatible": False,
            "template": None,
            "reason": "algorithme inconnu des mineurs standard et mineur propre au projet : "
                      "seul ce binaire peut miner.",
        }
    return {
        "algo": text,
        "classification": "variante",
        "family": "",
        "miners": [],
        "miners_compatible": False,
        "template": None,
        "reason": f"« {text or 'algorithme non renseigné'} » n'est documenté par aucun "
                  "mineur standard du catalogue.",
    }


_NO_PROFILE_RE = re.compile(
    r"aucun\s+profil|pas\s+de\s+profil|aucun\s+profile|pas\s+de\s+profile|"
    r"aucun\s+mineur|pas\s+de\s+mineur|sans\s+profil|sans\s+mineur",
    re.IGNORECASE,
)


def _own_miner(candidate: dict[str, Any]) -> bool:
    if candidate.get("own_miner"):
        return True
    for link in candidate.get("links") or []:
        low = str(link).lower()
        if re.search(r"(^|[/_-])[a-z0-9._-]*miner([/_.-]|$)", low) and "xmrig" not in low:
            return True
    return False


def review_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Bloc catalogue d'un candidat : identification + mineurs + avertissements."""
    algo = str(candidate.get("algo") or "").strip()
    found = identify(algo, own_miner=_own_miner(candidate))
    found["warnings"] = catalogue_warnings(candidate, found)
    return found


def catalogue_warnings(candidate: dict[str, Any], review: dict[str, Any] | None = None) -> list[str]:
    """Contradictions évidentes entre l'identification et les arguments.

    Cas visé : un « contre » affirme « aucun profil / aucun mineur » alors que
    l'algorithme est standard et qu'un profil type existe.  Avertissement
    visible, jamais bloquant.
    """
    review = review or identify(str(candidate.get("algo") or ""), own_miner=_own_miner(candidate))
    if review.get("classification") != "standard" or not review.get("template"):
        return []
    hits = [text for text in candidate.get("contre") or [] if _NO_PROFILE_RE.search(str(text))]
    if not hits:
        return []
    template = review["template"]
    return [
        "contradiction : l'algorithme « " + str(candidate.get("algo") or "?") + " » est standard "
        f"({template['software_name']} {template['algo']} disponible) mais un « contre » affirme "
        "l'absence de profil ou de mineur : « " + str(hits[0])[:160] + " »."
    ]


def instantiate(
    template: dict[str, Any],
    *,
    wallet: str,
    pool: str,
    worker: str,
    binary: str,
    api_port: int | None = None,
    coin: str = "",
    version: str = "",
) -> dict[str, Any]:
    """Instancie un profil type pour **une** machine (arguments rendus).

    Le binaire reste celui de la source officielle du catalogue ; le wallet et
    le pool viennent de la campagne.  Aucune substitution inconnue n'est
    laissée dans la ligne de commande.
    """
    values = {
        "binary": str(binary),
        "wallet": str(wallet),
        "pool": str(pool),
        "worker": str(worker),
        "algo": str(template["algo"]),
        "api_port": str(api_port or template["api_port"]),
    }
    rendered = template["cmdline"]
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    if "{" in rendered or "}" in rendered:
        raise ValueError("gabarit d'arguments : variable non résolue")
    return {
        "name": f"{template['software']}-{_slug(template['algo'])}-{worker}"[:60],
        "coin": coin.upper(),
        "software": template["software"],
        "version": version,
        "algo": template["algo"],
        "wallet": wallet,
        "pool": pool,
        "worker": worker,
        "cmdline": rendered,
        "api_port": int(api_port or template["api_port"]),
        "api_kind": template["api_kind"],
        "binary_url": template["binary_url"],
        "binary_source": "url",
        "execution_mode": "hybrid",
        "template_id": template["id"],
    }


def mode_for(raw_algo: Any, software: str = "") -> dict[str, Any]:
    """Mode de campagne déduit du catalogue (§6.4).

    Standard → ``hybrid`` (mineur officiel sur l'hôte, nœud et wallet en VM).
    Variante/propriétaire → ``vm`` (le binaire du projet reste isolé).
    """
    found = identify(raw_algo, own_miner=str(software or "").lower() in ("custom", "projet"))
    standard = found["classification"] == "standard"
    if standard:
        template = found.get("template") or {}
        reason = (
            f"algorithme standard « {found['algo']} » : {template.get('software_name', 'mineur')} "
            f"{template.get('algo', '')} sur l'hôte, nœud et wallet dans la VM."
        )
    else:
        reason = (
            f"algorithme {found['classification']} « {found['algo'] or 'inconnu'} » : "
            "binaire du projet isolé dans la VM (nœud, wallet et mineur)."
        )
    return {
        "mode": "hybrid" if standard else "vm",
        "reason": reason,
        "classification": found["classification"],
        "template": found.get("template"),
    }


def validate_catalogue(data: Any) -> dict[str, Any]:
    """Valide un catalogue édité avant enregistrement (Configuration)."""
    if not isinstance(data, dict):
        raise ValueError("catalogue : objet JSON attendu")
    softwares = data.get("softwares")
    if not isinstance(softwares, list) or not softwares:
        raise ValueError("catalogue : liste « softwares » vide")
    ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(softwares, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"catalogue : logiciel #{index} invalide")
        software_id = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", software_id):
            raise ValueError(f"catalogue : identifiant de logiciel invalide (#{index})")
        if software_id in ids:
            raise ValueError(f"catalogue : doublon de logiciel « {software_id} »")
        ids.add(software_id)
        for key in ("name", "source", "release_url", "cmdline"):
            if not str(item.get(key) or "").strip():
                raise ValueError(f"catalogue : « {software_id} » sans {key}")
        algos = item.get("algos")
        if not isinstance(algos, list) or not algos:
            raise ValueError(f"catalogue : « {software_id} » sans algorithme")
        cleaned_algos: list[dict[str, Any]] = []
        for algo in algos:
            if not isinstance(algo, dict):
                raise ValueError(f"catalogue : algorithme invalide dans « {software_id} »")
            name = str(algo.get("name") or "").strip()
            kind = str(algo.get("kind") or "").strip().lower()
            if not name:
                raise ValueError(f"catalogue : algorithme sans nom dans « {software_id} »")
            if kind not in ("cpu", "gpu", "both"):
                raise ValueError(f"catalogue : type invalide pour « {software_id}/{name} »")
            pair = (software_id, name.lower())
            if pair in pairs:
                raise ValueError(f"catalogue : doublon « {software_id}/{name} »")
            pairs.add(pair)
            cleaned_algos.append(
                {
                    "name": name,
                    "kind": kind,
                    "family": str(algo.get("family") or _family_guess(name) or "autre"),
                    "aliases": [str(alias) for alias in (algo.get("aliases") or [])],
                    "note": str(algo.get("note") or ""),
                }
            )
        cleaned.append({**item, "id": software_id, "algos": cleaned_algos})
    return {
        "version": str(data.get("version") or CATALOGUE_VERSION),
        "retrieved": str(data.get("retrieved") or RETRIEVED),
        "note": str(data.get("note") or ""),
        "softwares": cleaned,
    }


def effective_catalogue(db: Any) -> dict[str, Any]:
    """Catalogue en vigueur : version éditée en Configuration, sinon intégrée."""
    try:
        from app.ai_config import get_setting

        stored = get_setting(db, "miner_catalogue", None)
        if isinstance(stored, str) and stored.strip():
            stored = json.loads(stored)
        if isinstance(stored, dict) and stored.get("softwares"):
            return validate_catalogue(stored)
    except Exception:
        pass
    return catalogue()
