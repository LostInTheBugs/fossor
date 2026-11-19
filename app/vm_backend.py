"""Fabrique de backend des campagnes ``vm``/``hybrid`` (§4.6).

**Un seul point de décision** : l'hôte de spec est-il une machine
``os_kind=proxmox`` ? Oui → pilote Proxmox (:mod:`app.pve_remote`, plans de
:mod:`app.pve_vm`) ; non → pilote libvirt (:mod:`app.vm_remote`, plans de
:mod:`app.spec_vm`). Les deux backends exposent la **même interface**, et
``campaign_runner`` ne connaît qu'elle : ``create``, ``wait_address``,
``pin_host_key``, ``provision``, ``start_miner``, ``stop_miner``, ``collect``,
``check_backup``, ``shutdown``, ``destroy``, ``list``.

Côté Proxmox, tout ce qui se passe **dans** la VM (provision, mineur, collecte,
sauvegarde) passe par SSH depuis l'hôte — les plans sont ceux de
:mod:`app.spec_vm`, seuls le compte (racine de l'hôte, sans ``sudo``) et la clé
changent. Aucun plan n'utilise ``pct`` : un conteneur n'est jamais touché.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PVE_OS_KIND = "proxmox"
SPEC_HOST_KEY = "spec_host_id"


class VmBackendError(RuntimeError):
    """Pas d'hôte de spec, pas de modèle, ou modèle invalide — message opérateur."""


def backend_kind(host: Any) -> str:
    """Décision unique : ``proxmox`` ou ``libvirt``, selon le type d'OS de l'hôte."""
    kind = (getattr(host, "os_kind", "") or "").strip().lower()
    return PVE_OS_KIND if kind == PVE_OS_KIND else "libvirt"


def backend_for_host(host: Any) -> "LibvirtBackend | PveBackend":
    return PveBackend() if backend_kind(host) == PVE_OS_KIND else LibvirtBackend()


# ── Hôte de spec ──────────────────────────────────────────────────────
def _setting_host_id(db: Any) -> int:
    from app.ai_config import get_setting

    raw = get_setting(db, SPEC_HOST_KEY, None)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def spec_host(db: Any):
    """Machine choisie comme hôte de spec (Configuration → Spec mining).

    Le réglage ``spec_host_id`` fait foi ; à défaut, l'hôte du dernier modèle
    enregistré (Proxmox puis libvirt) est retenu — les modèles antérieurs à ce
    réglage restent donc utilisables sans migration.
    """
    from app.models import Machine

    host_id = _setting_host_id(db)
    if host_id:
        host = db.query(Machine).filter(Machine.id == host_id).first()
        if host is not None:
            return host
    from app.routers.pve import stored_pve_model
    from app.routers.vms import stored_model

    for model in (stored_pve_model(db), stored_model(db)):
        if isinstance(model, dict) and model.get("host_id"):
            host = (
                db.query(Machine)
                .filter(Machine.id == int(model["host_id"]))
                .first()
            )
            if host is not None:
                return host
    return None


def backend_for(db: Any) -> "SpecContext":
    """Hôte + modèle validé + backend, dans un seul objet pour le planificateur."""
    host = spec_host(db)
    if host is None:
        raise VmBackendError(
            "Aucune machine hôte de spec configurée et aucun modèle de VM "
            "enregistré (Configuration → Spec mining)."
        )
    if not host.vm_host:
        raise VmBackendError(f"La machine {host.name} n'est pas marquée comme hôte de VM.")
    backend = backend_for_host(host)
    raw = backend.load_model(db)
    if not raw:
        label = "Proxmox" if backend.kind == PVE_OS_KIND else "libvirt"
        raise VmBackendError(
            f"Aucun modèle de VM {label} configuré (Configuration → Spec mining)."
        )
    try:
        model = backend.validate(raw)
    except Exception as exc:  # VmError des deux pilotes : message déjà explicite
        raise VmBackendError(f"Modèle de VM invalide : {exc}")
    if int(model.get("host_id") or 0) != int(host.id):
        raise VmBackendError(
            "Le modèle de VM ne désigne pas l'hôte de spec choisi "
            f"({host.name}) — réenregistrez-le dans Configuration → Spec mining."
        )
    return SpecContext(host=host, model=model, backend=backend)


@dataclass
class SpecContext:
    host: Any
    model: dict[str, Any]
    backend: Any

    @property
    def kind(self) -> str:
        return self.backend.kind


# ── libvirt ───────────────────────────────────────────────────────────
class LibvirtBackend:
    """Pilote libvirt : délègue à :mod:`app.vm_remote` (aucune duplication)."""

    kind = "libvirt"
    model_key = "spec_vm_model"

    def load_model(self, db: Any) -> dict | None:
        from app.routers.vms import stored_model

        return stored_model(db)

    def validate(self, raw: dict) -> dict:
        from app import spec_vm as V

        return V.validate_vm_model(raw)

    async def create(self, host: Any, model: dict, row: Any, simulated: bool) -> dict:
        from app import vm_remote as VR
        from app.fossor_keys import private_key_text, public_key_text

        return await VR.create(
            host, model, row.name,
            ssh_public_key=public_key_text(),
            private_key=private_key_text(),
            simulated=simulated,
        )

    async def wait_address(self, host: Any, model: dict, row: Any, simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.wait_address(host, row.name, simulated=simulated)

    async def pin_host_key(self, host: Any, model: dict, row: Any, ip: str,
                           simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.pin_host_key(host, ip, simulated=simulated)

    async def provision(self, host: Any, model: dict, profile: dict, row: Any, *,
                        coin: str, expected_address: str = "", simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.provision(
            host, model, profile, row.ip, coin=coin, simulated=simulated,
            expected_address=expected_address,
        )

    async def start_miner(self, host: Any, model: dict, profile: dict, argv: list[str],
                          row: Any, simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.start_miner(host, profile, argv, row.ip, simulated=simulated)

    async def stop_miner(self, host: Any, model: dict, profile: dict, row: Any,
                         simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.stop_miner(host, profile, row.ip, simulated=simulated)

    async def check_binary(self, host: Any, model: dict, profile: dict, row: Any,
                           simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.check_binary(host, row.ip, profile, simulated=simulated)

    async def check_alive(self, host: Any, model: dict, profile: dict, row: Any,
                          simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.check_alive(host, row.ip, profile, simulated=simulated)

    async def miner_log_tail(self, host: Any, model: dict, profile: dict, row: Any,
                             simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.miner_log_tail(host, row.ip, profile, simulated=simulated)

    async def collect(self, host: Any, model: dict, row: Any, profile: dict | None,
                      simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.collect(host, row.name, row.ip, profile, simulated=simulated)

    async def check_backup(self, host: Any, model: dict, row: Any, path: str, *,
                           now_epoch: int, max_age_days: int, simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.check_backup(
            host, row.ip, path, now_epoch=now_epoch, max_age_days=max_age_days,
            simulated=simulated, wallet_mode=str(model.get("_wallet_mode") or "file"),
        )

    async def shutdown(self, host: Any, model: dict, row: Any, simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.shutdown(host, row.name, simulated=simulated)

    async def destroy(self, host: Any, model: dict, row: Any, *, backup: dict | None,
                      now_epoch: int, max_age_days: int, simulated: bool) -> dict:
        from app import spec_vm as V
        from app import vm_remote as VR

        description = V.OWNER_MARKER
        if not simulated:
            live = await VR.list_vms(host, simulated=False)
            found = next((item for item in live["vms"] if item["name"] == row.name), None)
            description = found["description"] if found else ""
        return await VR.destroy(
            host, row.name, description, backup=backup, now_epoch=now_epoch,
            max_age_days=max_age_days, simulated=simulated,
            keep_cache=bool(model.get("keep_image_cache")),
        )

    async def list(self, host: Any, simulated: bool) -> dict:
        from app import vm_remote as VR

        return await VR.list_vms(host, simulated=simulated)


# ── Proxmox ───────────────────────────────────────────────────────────
class PveBackend:
    """Pilote Proxmox : ``qm``/``pvesh``/``pvesm`` pour l'infra, SSH vers la VM."""

    kind = PVE_OS_KIND
    model_key = "spec_pve_model"

    def load_model(self, db: Any) -> dict | None:
        from app.routers.pve import stored_pve_model

        return stored_pve_model(db)

    def validate(self, raw: dict) -> dict:
        from app import pve_vm as P

        return P.validate_pve_model(raw)

    @staticmethod
    def vmid_of(row: Any) -> int:
        value = int(getattr(row, "vmid", 0) or 0)
        if value <= 0:
            raise VmBackendError("VMID inconnu pour cette VM Proxmox — campagne relancée sans création ?")
        return value

    async def create(self, host: Any, model: dict, row: Any, simulated: bool) -> dict:
        from app import pve_remote as PR
        from app import pve_vm as P
        from app.fossor_keys import private_key_text, public_key_text

        P.validate_name(row.name)
        vmid = int(getattr(row, "vmid", 0) or 0)
        commands: list[str] = []
        if not simulated:
            # Inspection lecture seule : refus du stockage cloud-init sans
            # `snippets` **avant** toute écriture, et VMID libres du cluster.
            report = await PR.inspect(host, simulated=False, model=model)
            if not vmid:
                if report.get("free_vmid"):
                    vmid = int(report["free_vmid"])
                else:
                    taken = set(report.get("taken") or [])
                    vmid = P.choose_vmid(model["vmid_min"], model["vmid_max"], taken)
            commands.extend(report.get("commands") or [])
        if not vmid:
            vmid = int(getattr(row, "vmid", 0) or 0) or P.choose_vmid(
                model["vmid_min"], model["vmid_max"], set()
            )
        if not P.in_range(vmid, model["vmid_min"], model["vmid_max"]):
            raise VmBackendError(
                f"VMID {vmid} hors de la plage réservée {model['vmid_min']}-{model['vmid_max']}."
            )
        result = await PR.create(
            host, model, row.name, vmid,
            ssh_public_key=public_key_text(),
            private_key=private_key_text(),
            simulated=simulated,
        )
        result["vmid"] = vmid
        result["commands"] = commands + list(result.get("commands") or [])
        return result

    async def wait_address(self, host: Any, model: dict, row: Any, simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.wait_address(
            host, self.vmid_of(row) if not simulated else int(getattr(row, "vmid", 0) or 0),
            simulated=simulated, bridge=str(model.get("bridge") or ""),
        )

    async def pin_host_key(self, host: Any, model: dict, row: Any, ip: str,
                           simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.pin_host_key(host, ip, simulated=simulated)

    async def provision(self, host: Any, model: dict, profile: dict, row: Any, *,
                        coin: str, expected_address: str = "", simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.provision(
            host, model, profile, row.ip, coin=coin, simulated=simulated,
            expected_address=expected_address,
        )

    async def start_miner(self, host: Any, model: dict, profile: dict, argv: list[str],
                          row: Any, simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.start_miner(host, profile, argv, row.ip, simulated=simulated)

    async def stop_miner(self, host: Any, model: dict, profile: dict, row: Any,
                         simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.stop_miner(host, profile, row.ip, simulated=simulated)

    async def check_binary(self, host: Any, model: dict, profile: dict, row: Any,
                           simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.check_binary(host, row.ip, profile, simulated=simulated,
                                     bridge=str(model.get("bridge") or ""))

    async def check_alive(self, host: Any, model: dict, profile: dict, row: Any,
                          simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.check_alive(host, row.ip, profile, simulated=simulated,
                                    bridge=str(model.get("bridge") or ""))

    async def miner_log_tail(self, host: Any, model: dict, profile: dict, row: Any,
                             simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.miner_log_tail(host, row.ip, profile, simulated=simulated,
                                       bridge=str(model.get("bridge") or ""))

    async def collect(self, host: Any, model: dict, row: Any, profile: dict | None,
                      simulated: bool) -> dict:
        from app import pve_remote as PR

        vmid = int(getattr(row, "vmid", 0) or 0)
        if vmid <= 0 and not simulated:
            raise VmBackendError("VMID inconnu : collecte Proxmox impossible.")
        return await PR.collect(host, vmid, row.ip, profile, simulated=simulated)

    async def check_backup(self, host: Any, model: dict, row: Any, path: str, *,
                           now_epoch: int, max_age_days: int, simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.check_backup(
            host, row.ip, path, now_epoch=now_epoch, max_age_days=max_age_days,
            simulated=simulated, wallet_mode=str(model.get("_wallet_mode") or "file"),
        )

    async def ensure_owned_live(self, host: Any, model: dict, row: Any) -> dict:
        """Propriété relue sur le nœud : plage, tag ``fossor``, ``fossor_vm=1``."""
        from app import pve_remote as PR
        from app import pve_vm as P

        vmid = self.vmid_of(row)
        config = await PR.config(host, vmid, simulated=False)
        try:
            P.ensure_owned(
                vmid, model["vmid_min"], model["vmid_max"],
                config.get("tags") or [], config.get("description") or "",
            )
        except P.VmError as exc:
            raise VmBackendError(str(exc))
        return config

    async def shutdown(self, host: Any, model: dict, row: Any, simulated: bool) -> dict:
        from app import pve_remote as PR

        vmid = int(getattr(row, "vmid", 0) or 0)
        if vmid <= 0 and not simulated:
            raise VmBackendError("VMID inconnu : extinction Proxmox impossible.")
        if not simulated:
            # Garde-fous avant toute commande : une VM hors plage, sans tag ou
            # sans ``fossor_vm=1`` n'est pas arrêtée (constat n°4).
            await self.ensure_owned_live(host, model, row)
        return await PR.shutdown(host, vmid, simulated=simulated)

    async def destroy(self, host: Any, model: dict, row: Any, *, backup: dict | None,
                      now_epoch: int, max_age_days: int, simulated: bool) -> dict:
        from app import pve_remote as PR

        vmid = int(getattr(row, "vmid", 0) or 0)
        if vmid <= 0:
            # Sans VMID, aucune destruction : la plage et le tag ne peuvent pas
            # être relus, et deviner un identifiant reviendrait à toucher une VM
            # qui n'est pas la nôtre.
            raise VmBackendError("VMID inconnu : destruction refusée.")
        return await PR.destroy(
            host, model, vmid, backup=backup, now_epoch=now_epoch,
            max_age_days=max_age_days, simulated=simulated,
        )

    async def list(self, host: Any, simulated: bool) -> dict:
        from app import pve_remote as PR

        return await PR.list_guests(host, simulated=simulated)


__all__ = [
    "LibvirtBackend", "PVE_OS_KIND", "PveBackend", "SPEC_HOST_KEY", "SpecContext",
    "VmBackendError", "backend_for", "backend_for_host", "backend_kind", "spec_host",
]
