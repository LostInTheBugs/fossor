"""Critères d'évaluation de Fred, servis par Fossor (§4.8).

Le texte des critères vit dans la règle ``criteria`` (scope ``selection``) :
il est éditable dans l'UI et **c'est lui que la veille lit** via
``GET /api/hermes/context`` — plus aucune copie figée dans un skill.

Seul le critère « Compatible avec les GPU de Fred » est calculé : il s'appuie
sur l'inventaire réel des GPU de Fossor (modèle et VRAM), jamais sur une liste
de modèles figée.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

#: Texte initial, repris mot pour mot de Fred.
DEFAULT_CRITERIA: list[dict[str, Any]] = [
    {
        "key": "fenetre",
        "label": "Fenêtre de sortie",
        "text": "Coin sorti depuis quelques heures à ~3 jours maximum ; au-delà "
        "(ex. 1 mois), c'est trop tard.",
        "linked_rule": "freshness",
    },
    {
        "key": "utilite",
        "label": "Utilité du projet",
        "text": "À quoi ça sert ? Le projet résout-il un problème concret ?",
    },
    {
        "key": "equipe",
        "label": "Crédibilité de l'équipe",
        "text": "Qui est derrière ? Compte Bitcointalk « Newbie » avec 0 merit = "
        "red flag, mais pas éliminatoire en spec.",
    },
    {
        "key": "intuition",
        "label": "Intuition de Fred",
        "text": "Critère subjectif mais explicite.",
    },
    {
        "key": "gpu",
        "label": "Compatible avec les GPU de Fred",
        "text": "Algo CUDA supporté, OU algo GPU/ASIC annoncé à court terme.",
        "computed": "gpu_inventory",
    },
]

DEFAULT_NUANCES: list[str] = [
    "CPU-only ≠ exclusion : minable en VM (RandomX etc.) sans toucher au GPU.",
    "GPU dès le mainnet sans mineur CPU distribué → ne pas attendre de phase CPU.",
    "ASIC-only dès le lancement → éliminé.",
    "Pool absent ou unique au lancement → neutre, ce n'est pas un défaut.",
    "Déjà listé sur un exchange → estimer la rentabilité réelle "
    "(prix × hashrate / hashrate réseau × émission par jour).",
    "Hors fenêtre mais signaux frais (hashrate qui bouge, listing, activité ANN) "
    "→ surveiller quand même.",
]

DEFAULT_BONUSES: list[str] = [
    "Algo déjà supporté par un mineur connu (SRBMiner, lolMiner, bzMiner…).",
    "Algo custom maison = risque malware → tester dans un OS jetable "
    "(règle VM de Fred, SPEC §4.6).",
]

DEFAULT_POUR: list[str] = [
    "Hashrate réseau au plus bas.",
    "Fenêtre de 96 h sur les premières heures.",
    "Risque de listing / envolée de prix.",
    "Zéro impact sur Pearl si CPU.",
    "Coût élec faible.",
    "Cumul de tokens.",
]

DEFAULT_CONTRE: list[str] = [
    "Pas de prix garanti.",
    "Faux projets / scams.",
    "Coût d'opportunité (on quitte Pearl).",
    "Risque malware.",
    "Complexité opérationnelle.",
    "Ratio hashrate / réseau parfois ridicule (plus de 14 jours par bloc en solo).",
    "Bascule CPU→GPU à anticiper.",
]

#: Familles d'algos CPU : le critère GPU n'a pas à se prononcer (``na``).
CPU_ALGOS = (
    "randomx",
    "cryptonight",
    "argon",
    "verushash",
    "ghostrider",
    "yescrypt",
    "cpupower",
    "scrypt-jane",
    "nighthash",
    "boundhash",
)

#: Familles d'algos GPU connues des mineurs cités par Fred.
GPU_ALGOS = (
    "ethash",
    "etchash",
    "kawpow",
    "autolykos",
    "kaspa",
    "kheavyhash",
    "blake3",
    "scrypt",
    "equihash",
    "cuckatoo",
    "cuckaroo",
    "firopow",
    "progpow",
    "zelhash",
    "beamhash",
    "octopus",
    "karlsenhash",
    "pyrinhash",
    "nexapow",
)


def _plain(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


def gpu_inventory(db: Any) -> list[dict[str, Any]]:
    """GPU réellement inventoriés : modèle, VRAM, machine, nombre."""
    from app.models import Device, Machine

    rows = (
        db.query(Device, Machine)
        .join(Machine, Machine.id == Device.machine_id)
        .filter(Device.kind == "gpu", Device.present.is_(True))
        .order_by(Machine.name, Device.index)
        .all()
    )
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for device, machine in rows:
        model = (device.model or device.vendor or "GPU").strip()
        vram = int(device.vram_mb or 0)
        key = (model, vram)
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "model": model,
                "vram_mb": vram,
                "machines": [machine.name],
                "count": 1,
            }
        else:
            entry["count"] += 1
            if machine.name not in entry["machines"]:
                entry["machines"].append(machine.name)
    return sorted(grouped.values(), key=lambda item: (-item["vram_mb"], item["model"]))


def inventory_summary(inventory: list[dict[str, Any]]) -> str:
    if not inventory:
        return "aucun GPU inventorié dans Fossor"
    parts = []
    for entry in inventory:
        gb = entry["vram_mb"] / 1024 if entry["vram_mb"] else 0
        vram = f" {gb:.0f} Go" if gb else ""
        suffix = f" ×{entry['count']}" if entry["count"] > 1 else ""
        parts.append(f"{entry['model']}{vram}{suffix}")
    return ", ".join(parts)


def gpu_criterion(db: Any, algo: Any) -> dict[str, Any]:
    """Verdict du critère GPU pour un algo, calculé sur l'inventaire réel.

    * inventaire vide → ``ko`` (rien ne peut être miné) ;
    * algo CPU connu → ``na`` (le critère GPU ne s'applique pas) ;
    * algo GPU connu → ``ok`` si au moins un GPU dédié est présent ;
    * algo inconnu ou vide → ``warn`` : à confirmer contre les modèles réels.
    """
    text = _plain(algo)
    inventory = gpu_inventory(db)
    summary = inventory_summary(inventory)
    dedicated = [entry for entry in inventory if entry["vram_mb"] >= 4096]
    if not inventory:
        return {"key": "gpu", "verdict": "ko", "note": "aucun GPU inventorié dans Fossor"}
    if not text:
        return {"key": "gpu", "verdict": "warn", "note": f"algo non renseigné — GPU : {summary}"}
    if any(name in text for name in CPU_ALGOS):
        return {"key": "gpu", "verdict": "na", "note": "algo CPU : minable en VM, sans GPU"}
    if any(name in text for name in GPU_ALGOS):
        if dedicated:
            return {"key": "gpu", "verdict": "ok", "note": f"algo GPU supporté — GPU : {summary}"}
        return {"key": "gpu", "verdict": "warn", "note": f"aucun GPU ≥ 4 Go — GPU : {summary}"}
    if re.search(r"\b(gpu|cuda|opencl)\b", text):
        return {"key": "gpu", "verdict": "ok", "note": f"algo GPU annoncé — GPU : {summary}"}
    return {
        "key": "gpu",
        "verdict": "warn",
        "note": f"algo inconnu, à confirmer sur l'inventaire — GPU : {summary}",
    }


def criteria_block(db: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Critères servis à la veille, critère GPU résolu sur l'inventaire."""
    data = dict(params or {})
    inventory = gpu_inventory(db)
    summary = inventory_summary(inventory)
    criteria: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("criteria") or DEFAULT_CRITERIA, start=1):
        entry = {
            "order": index,
            "key": item.get("key") or f"critere-{index}",
            "label": item.get("label") or "",
            "text": item.get("text") or "",
            "linked_rule": item.get("linked_rule") or "",
            "computed": item.get("computed") or "",
        }
        if entry["computed"] == "gpu_inventory":
            entry["inventory"] = inventory
            entry["inventory_summary"] = summary
            entry["evaluation"] = gpu_criterion(db, "")
        criteria.append(entry)
    return {
        "rule_id": "criteria",
        "criteria": criteria,
        "nuances": list(data.get("nuances") or DEFAULT_NUANCES),
        "bonuses": list(data.get("bonuses") or DEFAULT_BONUSES),
        "pour": list(data.get("pour") or DEFAULT_POUR),
        "contre": list(data.get("contre") or DEFAULT_CONTRE),
        "gpu_inventory": inventory,
        "gpu_inventory_summary": summary,
    }
