"""Archives de mineurs déposées dans Fossor (aucune release publique).

Le SHA256 est calculé à l'envoi, stocké sur le profil, affiché dans l'UI, puis
revérifié **côté Fossor** avant le push et **dans la VM** après le push. Un
binaire n'est jamais exécuté sans ces deux vérifications.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

MAX_BINARY_BYTES = 512 * 1024 * 1024


class BinaryError(RuntimeError):
    pass


def binary_dir() -> Path:
    base = Path(os.environ.get("FOSSOR_DATA_DIR") or "/data") / "binaries"
    base.mkdir(parents=True, exist_ok=True)
    return base


def stored_path(profile: dict) -> Path:
    uuid = str(profile.get("uuid") or "").strip()
    if not uuid or "/" in uuid or "\\" in uuid or uuid.startswith("."):
        raise BinaryError("profil sans uuid valide : archive Fossor impossible")
    return binary_dir() / f"{uuid}.tar.gz"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_binary(uuid: str, blob: bytes) -> dict:
    if not blob:
        raise BinaryError("archive vide")
    if len(blob) > MAX_BINARY_BYTES:
        raise BinaryError(f"archive trop volumineuse ({len(blob)} octets, 512 Mo max)")
    path = stored_path({"uuid": uuid})
    part = path.with_name(path.name + ".part")
    part.write_bytes(blob)
    part.replace(path)
    return {"sha256": _digest(path), "size": len(blob), "path": str(path)}


def verified_path(profile: dict) -> tuple[Path, str]:
    """Chemin local et SHA256, vérifiés avant tout envoi vers une VM."""
    path = stored_path(profile)
    if not path.is_file():
        raise BinaryError("archive du mineur absente de Fossor : envoyez-la d'abord")
    expected = str(profile.get("sha256") or "").strip().lower()
    digest = _digest(path)
    if not expected or digest != expected:
        raise BinaryError(f"archive du mineur : SHA256 local {digest} != {expected or '(vide)'}")
    return path, digest


def metadata(profile: dict) -> dict:
    path = stored_path(profile)
    present = path.is_file()
    return {
        "present": present,
        "path": str(path),
        "size": path.stat().st_size if present else 0,
        "sha256": (_digest(path) if present else str(profile.get("sha256") or "")),
        "source": str(profile.get("binary_source") or "url"),
    }
