"""Garde-fou : aucune adresse IPv4 d'un réseau domestique réel dans le dépôt.

Le test parcourt **tous** les fichiers suivis par git et échoue sur toute
adresse des plages privées du parc de Fred. Les plages sont assemblées à
l'exécution : le fichier du garde-fou ne contient donc lui-même aucun littéral
interdit et ne se dénonce pas.

Exceptions explicites, et seulement celles-là :
  * la notation CIDR globale du réseau domestique ;
  * le réseau libvirt de la VM de test ;
  * la notation CIDR de la plage loopback-large ;
  * l'adresse de boucle locale.

Une adresse publique de documentation (RFC 5737, 192.0.2.0/24) s'écrit à la
place d'une adresse réelle.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_no_real_wallets import REPO, _tracked_files

# Assemblé à l'exécution : aucun littéral IPv4 interdit dans ce fichier.
BANNED_PREFIXES = tuple(f"192.168.{third}." for third in ("0", "1", "8", "10"))

# Notations autorisées, telles quelles.
ALLOWED = frozenset({
    "192.168.0.0/16",
    "192.168.122.0/24",
    "10.0.0.0/8",
    "127.0.0.1",
})

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(/\d{1,2})?(?![\d.])")


def _violations(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data:  # binaire : pas d'adresse en clair
        return []
    found: list[str] = []
    text = data.decode("utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in IPV4.finditer(line):
            if match.group(0) in ALLOWED:
                continue
            address = match.group(1)
            if address.startswith(BANNED_PREFIXES):
                found.append(f"{path.relative_to(REPO)}:{lineno}: {address}")
    return found


def test_no_real_private_ip_in_tracked_files():
    assert BANNED_PREFIXES and ALLOWED, "garde-fou vide : inopérant"
    hits = [hit for path in _tracked_files() for hit in _violations(path)]
    assert not hits, "adresse IPv4 d'un réseau réel recopiée : " + ", ".join(hits)
