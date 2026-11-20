"""Authentification désactivable sur les réseaux de confiance (préprod).

Fred est seul utilisateur, sur son réseau interne : l'authentification peut être
coupée pour les adresses de la liste, sans cookie.  Une adresse hors liste doit
toujours se connecter.

L'IP retenue est celle de la **connexion TCP** (``request.client.host``).
``X-Forwarded-For`` n'est lu que si un proxy de confiance a été déclaré
explicitement : sinon n'importe quel client pourrait se faire passer pour le
réseau interne en posant l'en-tête.
"""

from __future__ import annotations

import ipaddress
from typing import Any

AUTH_ENABLED_KEY = "auth_enabled"
TRUSTED_NETWORKS_KEY = "auth_trusted_networks"
TRUST_PROXY_KEY = "auth_trust_proxy"
STAY_LOGGED_IN_KEY = "auth_stay_logged_in_days"

#: Réseaux privés RFC 1918 + boucle locale.
DEFAULT_NETWORKS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.1"]
DEFAULT_STAY_DAYS = 30


def parse_networks(values: Any) -> list[str]:
    """Valide une liste de CIDR ; lève ``ValueError`` avec le texte fautif."""
    if values is None:
        return list(DEFAULT_NETWORKS)
    if isinstance(values, str):
        values = [line for line in values.replace(",", "\n").splitlines()]
    if not isinstance(values, (list, tuple)):
        raise ValueError("liste de réseaux attendue (CIDR séparés par des virgules)")
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ValueError(f"réseau invalide : « {text} »") from None
        # Une adresse seule (« 127.0.0.1 ») reste lisible telle quelle.
        out.append(text if "/" not in text else str(network))
    return out or list(DEFAULT_NETWORKS)


def read_settings(db: Any) -> dict[str, Any]:
    from app.ai_config import get_setting

    enabled = get_setting(db, AUTH_ENABLED_KEY, True)
    networks = get_setting(db, TRUSTED_NETWORKS_KEY, DEFAULT_NETWORKS)
    proxy = get_setting(db, TRUST_PROXY_KEY, False)
    days = get_setting(db, STAY_LOGGED_IN_KEY, DEFAULT_STAY_DAYS)
    try:
        parsed = parse_networks(networks)
    except ValueError:
        parsed = list(DEFAULT_NETWORKS)
    try:
        stay_days = int(days)
    except (TypeError, ValueError):
        stay_days = DEFAULT_STAY_DAYS
    if not 1 <= stay_days <= 365:
        stay_days = DEFAULT_STAY_DAYS
    return {
        "auth_enabled": True if not isinstance(enabled, bool) else enabled,
        "trusted_networks": parsed,
        "trust_proxy": bool(proxy),
        "stay_logged_in_days": stay_days,
    }


def write_settings(db: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Écrit les réglages ; ``ValueError`` si un CIDR ou une durée est invalide."""
    from app.ai_config import set_setting

    if "auth_enabled" in payload:
        set_setting(db, AUTH_ENABLED_KEY, bool(payload.get("auth_enabled")))
    if "trusted_networks" in payload:
        set_setting(db, TRUSTED_NETWORKS_KEY, parse_networks(payload.get("trusted_networks")))
    if "trust_proxy" in payload:
        set_setting(db, TRUST_PROXY_KEY, bool(payload.get("trust_proxy")))
    if "stay_logged_in_days" in payload:
        try:
            days = int(payload.get("stay_logged_in_days"))
        except (TypeError, ValueError):
            raise ValueError("durée de connexion invalide : nombre de jours attendu") from None
        if not 1 <= days <= 365:
            raise ValueError("durée de connexion hors bornes : 1 à 365 jours")
        set_setting(db, STAY_LOGGED_IN_KEY, days)
    db.commit()
    return read_settings(db)


def client_ip(request: Any, trust_proxy: bool = False) -> str:
    """IP de la connexion TCP ; ``X-Forwarded-For`` seulement si proxy déclaré."""
    direct = ""
    client = getattr(request, "client", None)
    if client is not None and getattr(client, "host", None):
        direct = str(client.host)
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for") or ""
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return direct


def network_allows(ip: str, networks: list[str]) -> bool:
    text = str(ip or "").strip()
    if text.lower().startswith("::ffff:"):
        text = text[7:]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    for raw in networks:
        try:
            network = ipaddress.ip_network(str(raw), strict=False)
        except ValueError:
            continue
        if address in network:
            return True
    return False


def evaluate(db: Any, request: Any) -> dict[str, Any]:
    """Réglages + IP + décision de contournement pour cette requête."""
    settings = read_settings(db)
    ip = client_ip(request, settings["trust_proxy"])
    bypass = (not settings["auth_enabled"]) and network_allows(ip, settings["trusted_networks"])
    return {**settings, "client_ip": ip, "bypass": bypass}


def bypass_for_request(request: Any) -> bool:
    """Version sans session (appelée par ``get_current_user``).

    Un jeton ``Authorization: Bearer`` donné par Hermes n'ouvre jamais les
    routes d'administration : on laisse la logique 403 existante s'appliquer.
    """
    header = ""
    try:
        header = request.headers.get("authorization") or ""
    except Exception:  # pragma: no cover - requête sans en-têtes
        header = ""
    if header.lower().startswith("bearer "):
        return False
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return bool(evaluate(db, request)["bypass"])
    except Exception:  # pragma: no cover - base indisponible : on garde le login
        return False
    finally:
        db.close()
