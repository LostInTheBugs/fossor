"""Type d'OS choisi à l'ajout (§4.1 ter).

Le type choisi pré-remplit une configuration adaptée à l'installation par
défaut ; la détection écrit le type **réel** dans ``os_kind_detected`` et
l'écart est signalé à l'utilisateur, qui peut appliquer le préréglage détecté.

Module pur (aucune base, aucun SSH) : les routes et l'UI ne font que
transporter ce que ``preset_fields()`` renvoie.
"""

from __future__ import annotations

from typing import Any

#: Qui peut être choisi dans le formulaire, dans cet ordre.
OS_KINDS = ("hiveos", "ubuntu", "proxmox", "other")

OS_LABELS = {
    "hiveos": "HiveOS",
    "ubuntu": "Ubuntu/Debian",
    "proxmox": "Proxmox",
    "other": "Autre",
}

#: Jamais pré-rempli dans le champ mot de passe, seulement affiché à côté.
HIVEOS_PASSWORD_HINT = "mot de passe par défaut de HiveOS : 1 (à changer)"

#: Préréglages (§4.1 ter). ``temp_user`` est le login *temporaire* de la
#: préparation automatique, pas le compte final (``ssh_user`` = fossor).
PRESETS: dict[str, dict[str, Any]] = {
    "hiveos": {
        "label": "HiveOS",
        "ssh_port": 22,
        "auth_type": "provision_pending",
        "temp_user": "user",
        "ssh_user": "fossor",
        "sudo": "sudo without password",
        "vm_host": False,
        "usage": "both",
        "collector": "hiveos",
        "secret_hint": HIVEOS_PASSWORD_HINT,
        "external_miners": True,
        "note": (
            "Collecteur HiveOS (/run/hive/last_stat.json, gpu-stats.json) ; "
            "un mineur du flight sheet est « externe »."
        ),
    },
    "ubuntu": {
        "label": "Ubuntu/Debian",
        "ssh_port": 22,
        "auth_type": "provision_pending",
        "temp_user": "",
        "ssh_user": "fossor",
        "sudo": "sudo without password",
        "vm_host": False,
        "usage": "both",
        "collector": "systemd",
        "secret_hint": "compte sudo existant, mot de passe jamais stocké après préparation",
        "external_miners": False,
        "note": "Préparation automatique avec un compte sudo existant ; collecteur nvidia-smi + systemd.",
    },
    "proxmox": {
        "label": "Proxmox",
        "ssh_port": 22,
        "auth_type": "password",
        "temp_user": "",
        "ssh_user": "root",
        "sudo": "none",
        "vm_host": True,
        "usage": "none",
        "collector": "systemd",
        "secret_hint": "",
        "external_miners": False,
        "note": "Compte root proposé, hôte de VM présumé, pas de minage direct par défaut.",
    },
    "other": {
        "label": "Autre",
        "ssh_port": None,
        "auth_type": None,
        "temp_user": "",
        "ssh_user": None,
        "sudo": None,
        "vm_host": None,
        "usage": None,
        "collector": "",
        "secret_hint": "",
        "external_miners": False,
        "note": "Aucun préréglage.",
    },
}


def os_label(kind: str | None) -> str:
    """Libellé humain d'un type d'OS ; inconnu → chaîne vide."""
    return OS_LABELS.get(str(kind or "").strip().lower(), "")


def preset_for(kind: str | None) -> dict[str, Any]:
    """Préréglage du type choisi (``other`` si le type est inconnu)."""
    return PRESETS.get(str(kind or "").strip().lower(), PRESETS["other"])


def preset_fields(kind: str | None) -> dict[str, Any]:
    """Champs à appliquer à une machine, sans les valeurs ``None``.

    Le mot de passe n'en fait jamais partie : HiveOS n'expose que le texte
    d'aide (``secret_hint``), le champ reste vide.
    """
    preset = preset_for(kind)
    fields = {
        key: preset.get(key)
        for key in ("ssh_port", "auth_type", "ssh_user", "sudo", "vm_host", "usage")
        if preset.get(key) is not None
    }
    if kind and str(kind).strip().lower() in OS_KINDS:
        fields["os_kind"] = str(kind).strip().lower()
    return fields


def apply_preset(machine: Any, kind: str | None) -> list[str]:
    """Applique le préréglage à *machine* et renvoie les champs modifiés."""
    changed: list[str] = []
    for key, value in preset_fields(kind).items():
        if getattr(machine, key, None) != value:
            setattr(machine, key, value)
            changed.append(key)
    return changed


def compare_os(chosen: str | None, detected: str | None) -> dict[str, Any]:
    """Écart entre le type choisi et l'OS réellement détecté.

    ``mismatch`` reste faux quand la détection n'a rien reconnu (``other``) :
    une bannière « OS détecté : Autre » n'apprend rien à l'utilisateur.
    """
    chosen_kind = str(chosen or "").strip().lower() or "other"
    detected_kind = str(detected or "").strip().lower()
    mismatch = bool(detected_kind) and detected_kind != chosen_kind and detected_kind != "other"
    label_detected = os_label(detected_kind)
    label_chosen = os_label(chosen_kind) or chosen_kind
    banner = (
        f"OS détecté : {label_detected} (choisi : {label_chosen})" if mismatch else ""
    )
    return {
        "chosen": chosen_kind,
        "detected": detected_kind or None,
        "mismatch": mismatch,
        "chosen_label": label_chosen,
        "detected_label": label_detected,
        "banner": banner,
        "preset_label": label_detected if mismatch else "",
    }


def public_presets() -> dict[str, Any]:
    """Préréglages exposés à l'UI (aucun secret, aucun mot de passe)."""
    return {
        "kinds": list(OS_KINDS),
        "labels": dict(OS_LABELS),
        "presets": {
            kind: {
                key: value
                for key, value in PRESETS[kind].items()
                if key in ("label", "ssh_port", "auth_type", "temp_user", "ssh_user",
                           "sudo", "vm_host", "usage", "collector", "secret_hint",
                           "external_miners", "note")
            }
            for kind in OS_KINDS
        },
    }
