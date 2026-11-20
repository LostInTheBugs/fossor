"""Binaire déjà installé sur une machine : adoption et import dans Fossor (§4.5).

Deux gestes, tous deux **sans réinstallation** et sans exécution :

* **adopter** : lire le SHA256 du binaire présent dans le dossier
  d'installation, le comparer à celui du profil ; identique → le profil est
  marqué installé sur cette machine ;
* **importer** : récupérer ce binaire par SFTP dans Fossor (SHA256 vérifié),
  pour pouvoir l'installer ailleurs.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import app.miners as M
from app import binaries as B
from app.models import AuditLog, MinerProfile, ProfileInstallation
from app.ssh_client import _safe_install_path, ssh_binary_sha256, ssh_get_file


class InstallError(RuntimeError):
    """Adoption ou import refusé — message affichable tel quel."""


def profile_dict(profile: MinerProfile) -> dict[str, Any]:
    return {
        "uuid": profile.uuid,
        "name": profile.name,
        "software": profile.software,
        "version": profile.version,
        "binary_path": profile.binary_path,
        "binary_kind": profile.binary_kind,
        "binary_source": profile.binary_source,
        "binary_stored": profile.binary_stored,
        "sha256": profile.sha256,
    }


def installed_path(profile: MinerProfile) -> str:
    try:
        return _safe_install_path(M.binary_path(profile_dict(profile)))
    except ValueError as exc:
        raise InstallError(str(exc)) from exc


def _expected(profile: MinerProfile) -> str:
    expected = str(profile.sha256 or "").strip().lower()
    if len(expected) != 64:
        raise InstallError("profil sans SHA256 exploitable : renseignez-le avant d'adopter")
    return expected


def record_installation(
    db: Any, profile: MinerProfile, machine: Any, digest: str, path: str, source: str
) -> ProfileInstallation:
    row = (
        db.query(ProfileInstallation)
        .filter(ProfileInstallation.profile_id == profile.id, ProfileInstallation.machine_id == machine.id)
        .first()
    )
    if row is None:
        row = ProfileInstallation(profile_id=profile.id, machine_id=machine.id)
        db.add(row)
    row.sha256 = digest
    row.path = path
    row.source = source
    db.commit()
    db.refresh(row)
    return row


async def adopt(db: Any, profile: MinerProfile, machine: Any, actor: str = "fred") -> dict[str, Any]:
    """Marque le profil installé sur la machine si le SHA256 correspond."""
    expected = _expected(profile)
    path = installed_path(profile)
    result = await ssh_binary_sha256(machine, path, actor=actor)
    if not result.get("ok"):
        raise InstallError(result.get("error") or "lecture du SHA256 impossible")
    actual = (result.get("stdout") or "").split()[0].strip().lower() if result.get("stdout") else ""
    if actual != expected:
        raise InstallError(
            f"SHA256 différent : binaire installé {actual or 'illisible'} != profil {expected} — rien adopté"
        )
    row = record_installation(db, profile, machine, actual, path, "adopted")
    db.add(
        AuditLog(
            actor=actor,
            action="binaire adopté",
            object_ref=f"profil #{profile.id} · {machine.name}",
            result="ok",
            output=f"sha256 {actual} · {path}",
        )
    )
    db.commit()
    return {
        "adopted": True,
        "profile_id": profile.id,
        "machine": machine.name,
        "path": path,
        "sha256": actual,
        "installation_id": row.id,
    }


async def import_from_machine(
    db: Any, profile: MinerProfile, machine: Any, actor: str = "fred"
) -> dict[str, Any]:
    """Récupère le binaire installé par SFTP, vérifie son SHA256, le stocke."""
    expected = _expected(profile)
    path = installed_path(profile)
    handle, tmp_name = tempfile.mkstemp(prefix="fossor-import-", suffix=".bin")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        try:
            await ssh_get_file(machine, path, tmp)
        except Exception as exc:
            raise InstallError(f"récupération du binaire impossible : {exc}") from exc
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise InstallError("binaire distant vide ou illisible")
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != expected:
            raise InstallError(
                f"SHA256 différent : binaire récupéré {digest} != profil {expected} — rien importé"
            )
        try:
            saved = B.save_binary(profile.uuid, tmp.read_bytes())
        except B.BinaryError as exc:
            raise InstallError(str(exc)) from exc
        if saved["sha256"] != expected:
            raise InstallError("SHA256 modifié pendant le dépôt — import annulé")
        profile.binary_source = "fossor"
        profile.binary_stored = saved["path"]
        profile.binary_size = saved["size"]
        profile.binary_kind = saved["kind"]
        profile.sha256 = saved["sha256"]
        record_installation(db, profile, machine, digest, path, "imported")
        db.add(
            AuditLog(
                actor=actor,
                action="binaire importé",
                object_ref=f"profil #{profile.id} · {machine.name}",
                result="ok",
                output=f"{saved['size']} octets · sha256 {digest}",
            )
        )
        db.commit()
        db.refresh(profile)
        return {
            "imported": True,
            "profile_id": profile.id,
            "machine": machine.name,
            "size": saved["size"],
            "kind": saved["kind"],
            "sha256": digest,
        }
    finally:
        tmp.unlink(missing_ok=True)


def installations(db: Any, profile_id: int) -> list[dict[str, Any]]:
    from app.models import Machine

    rows = (
        db.query(ProfileInstallation, Machine)
        .join(Machine, Machine.id == ProfileInstallation.machine_id)
        .filter(ProfileInstallation.profile_id == profile_id)
        .order_by(ProfileInstallation.id)
        .all()
    )
    return [
        {
            "id": row.id,
            "machine": machine.name,
            "machine_id": machine.id,
            "sha256": row.sha256,
            "path": row.path,
            "source": row.source,
            "created_at": row.created_at,
        }
        for row, machine in rows
    ]
