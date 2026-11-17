"""Validation of the machine payload (API side, HTTP 422, French messages).

The production defect: ``POST /api/machines`` accepted an empty ``host`` and
the machine was stored as "RIG — rig7095FA" with ``host=''``; detection then
failed with "Name or service not known".  Every field the SSH layer relies on
is now validated here *before* anything is written.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, NoReturn

from fastapi import HTTPException

NAME_MAX = 100
HOST_MAX = 255
USER_MAX = 50

# One or more DNS labels; a single label (``rig1``) is accepted on purpose
# (LAN hostnames), but empty strings, spaces and broken labels are not.
DNS_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)
AUTH_TYPES = ("password", "fossor_key", "key", "provision_pending", "provision")


def reject(message: str) -> None:
    raise HTTPException(status_code=422, detail=message)


def validate_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        reject("Nom : le nom de la machine est obligatoire.")
    if len(name) > NAME_MAX:
        reject(f"Nom : {NAME_MAX} caractères maximum.")
    return name


def validate_host(value: Any) -> str:
    host = value.strip() if isinstance(value, str) else ""
    if not host:
        reject(
            "Hôte : l'hôte est obligatoire — indiquez une adresse IPv4, une adresse "
            "IPv6 ou un nom DNS (ex. 192.0.2.10 ou rig.exemple.net)."
        )
    if len(host) > HOST_MAX:
        reject(f"Hôte : {HOST_MAX} caractères maximum.")
    if any(char.isspace() for char in host):
        reject("Hôte : les espaces ne sont pas autorisés.")
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    if "%" in candidate or not DNS_RE.fullmatch(candidate):
        reject(
            "Hôte : adresse invalide — indiquez une adresse IPv4, une adresse IPv6 "
            "ou un nom DNS valide (ex. 192.0.2.10 ou rig.exemple.net)."
        )
    return candidate


def validate_port(value: Any) -> int:
    if isinstance(value, bool):
        reject("Port SSH : le port doit être un nombre entre 1 et 65535.")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        reject("Port SSH : le port doit être un nombre entre 1 et 65535.")
    if not 1 <= port <= 65535:
        reject("Port SSH : le port doit être un nombre entre 1 et 65535.")
    return port


def validate_ssh_user(value: Any, field: str = "Login") -> str:
    user = value.strip() if isinstance(value, str) else ""
    if not user:
        reject(f"{field} : le login est obligatoire.")
    if any(char.isspace() for char in user):
        reject(f"{field} : les espaces ne sont pas autorisés.")
    if len(user) > USER_MAX:
        reject(f"{field} : {USER_MAX} caractères maximum.")
    return user


def validate_auth_type(value: Any) -> str:
    auth_type = value.strip() if isinstance(value, str) else ""
    if auth_type not in AUTH_TYPES:
        reject(
            "Mode d'authentification : valeur inconnue (attendu : password, "
            "fossor_key ou provision_pending)."
        )
    return auth_type


def validate_machine_payload(data: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Validate and normalise a create (``partial=False``) or PATCH payload."""
    if not isinstance(data, dict):
        reject("Données invalides : objet JSON attendu.")

    cleaned: dict[str, Any] = {}
    if not partial or "name" in data:
        cleaned["name"] = validate_name(data.get("name"))
    if not partial or "host" in data:
        cleaned["host"] = validate_host(data.get("host"))
    if not partial or "ssh_port" in data:
        cleaned["ssh_port"] = validate_port(data.get("ssh_port", 22))
    if not partial or "ssh_user" in data:
        cleaned["ssh_user"] = validate_ssh_user(data.get("ssh_user", "root"))
    if "auth_type" in data:
        cleaned["auth_type"] = validate_auth_type(data.get("auth_type"))
    elif not partial:
        cleaned["auth_type"] = "provision_pending"

    if "sudo" in data:
        sudo = data.get("sudo") if isinstance(data.get("sudo"), str) else ""
        if sudo not in ("none", "sudo without password", "sudo with password"):
            reject("Sudo : valeur inconnue.")
        cleaned["sudo"] = sudo
    if "usage" in data:
        usage = data.get("usage") if isinstance(data.get("usage"), str) else ""
        if usage not in ("both", "cpu", "gpu", "none"):
            reject("Usage : valeur inconnue.")
        cleaned["usage"] = usage
    return cleaned
