"""Configuration export / import (§4.13).

* identifiers are ``uuid`` (never SQL ids), so an export can be replayed on an
  empty database or merged into another instance;
* secrets are **excluded by default**: with ``include_secrets`` each machine
  secret and each AI provider key is encrypted with a key derived from a user
  passphrase (PBKDF2-SHA256, 600 000 iterations, random salt,
  Fernet/AES-128-CBC+HMAC).  The master key is never used to encrypt the file
  and never leaves the process;
* the inbound Hermes token is **never** exported, not even encrypted: it is
  local to the instance that issued it;
* an import is validated, diffed (``dry_run``), and always preceded by an
  automatic backup of the current configuration.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.orm import Session

from app.ai_config import NON_EXPORTED_SETTINGS, PROVIDER_FIELDS, PROVIDER_TYPES, WHICHES
from app.models import AiProvider, AppSetting, Device, Machine
from app.secrets import decrypt_secret, encrypt_secret

FORMAT_VERSION = 1
KDF_ITERATIONS = 600_000
KDF_MIN_ITERATIONS = 200_000
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

MACHINE_FIELDS = (
    "name", "host", "ssh_port", "ssh_user", "auth_type", "sudo",
    "os_kind", "host_key", "usage", "vm_host", "enabled",
)
DEVICE_FIELDS = (
    "machine_uuid", "kind", "index", "vendor", "model", "vram_mb", "pci_bus",
    "driver", "cores", "threads", "ram_mb", "flags", "nv_uuid", "present",
    "enabled_for_mining", "integrated",
)
MACHINE_REQUIRED = ("uuid", "name", "host")
DEVICE_REQUIRED = ("uuid", "machine_uuid", "kind")


class ConfigError(ValueError):
    """Invalid import document — surfaced as HTTP 400."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def app_version() -> str:
    return VERSION_FILE.read_text().strip()


# ── Secrets ────────────────────────────────────────────────────────────
def _fernet_for(passphrase: str, salt: bytes, iterations: int) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


def _settings_snapshot(db: Session) -> dict[str, Any]:
    """Exported settings; the hashed Hermes token never leaves the instance."""
    snapshot: dict[str, Any] = {}
    for row in db.query(AppSetting).order_by(AppSetting.key).all():
        if row.key in NON_EXPORTED_SETTINGS:
            continue
        try:
            snapshot[row.key] = json.loads(row.value)
        except (TypeError, ValueError):
            snapshot[row.key] = row.value
    return snapshot


def _encrypt_secrets(
    machine_secrets: dict[str, str],
    ai_secrets: dict[str, str],
    passphrase: str,
    ssh_key: str | None = None,
    notification_secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    salt = os.urandom(16)
    fernet = _fernet_for(passphrase, salt, KDF_ITERATIONS)
    block = {
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "cipher": "fernet",
        "tokens": {uuid: fernet.encrypt(plain.encode()).decode() for uuid, plain in machine_secrets.items()},
        "ai": {which: fernet.encrypt(plain.encode()).decode() for which, plain in ai_secrets.items()},
        # §4.14: notification secrets (Discord webhook, Telegram token, SMTP
        # password) follow the file, encrypted with the export passphrase only.
        "notifications": {
            name: fernet.encrypt(plain.encode()).decode()
            for name, plain in (notification_secrets or {}).items()
        },
    }
    # §4.1 bis: Fossor's own private key follows the configuration (encrypted
    # with the export passphrase, never with the master key of the file).
    if ssh_key:
        block["ssh_key"] = fernet.encrypt(ssh_key.encode()).decode()
    return block


def _decrypt_secrets(
    doc: dict, passphrase: str | None
) -> tuple[dict[str, str], dict[str, str], str | None, dict[str, str]]:
    """Decrypt the ``secrets`` block; raise ConfigError before any write.

    Returns ``(machine secrets by uuid, AI provider keys by ``which``,
    Fossor private key or None, notification secrets by name)``.
    """
    block = doc.get("secrets")
    if not block:
        return {}, {}, None, {}
    if not passphrase:
        raise ConfigError("Ce fichier contient des secrets chiffrés : une phrase de passe est requise")
    if block.get("cipher") != "fernet" or block.get("kdf") != "pbkdf2-sha256":
        raise ConfigError("Bloc « secrets » non pris en charge (chiffrement inconnu)")
    try:
        salt = base64.b64decode(block.get("salt") or "")
        iterations = int(block.get("iterations") or 0)
    except (ValueError, TypeError):
        raise ConfigError("Bloc « secrets » illisible (sel ou itérations invalides)")
    if iterations < KDF_MIN_ITERATIONS:
        raise ConfigError(f"Bloc « secrets » refusé : {iterations} itérations < {KDF_MIN_ITERATIONS}")
    if len(salt) < 8:
        raise ConfigError("Bloc « secrets » illisible (sel trop court)")
    fernet = _fernet_for(passphrase, salt, iterations)
    tokens = block.get("tokens") or {}
    ai_tokens = block.get("ai") or {}
    notification_tokens = block.get("notifications") or {}
    if not isinstance(tokens, dict) or not isinstance(ai_tokens, dict) or not isinstance(notification_tokens, dict):
        raise ConfigError("Bloc « secrets » illisible (tokens)")

    def _open(values: dict) -> dict[str, str]:
        opened: dict[str, str] = {}
        for name, token in values.items():
            try:
                opened[name] = fernet.decrypt(str(token).encode()).decode()
            except InvalidToken:
                raise ConfigError("Phrase de passe incorrecte (impossible de déchiffrer les secrets)")
        return opened

    ssh_key = None
    if block.get("ssh_key"):
        try:
            ssh_key = fernet.decrypt(str(block["ssh_key"]).encode()).decode()
        except InvalidToken:
            raise ConfigError("Phrase de passe incorrecte (clé SSH de Fossor illisible)")

    return _open(tokens), _open(ai_tokens), ssh_key, _open(notification_tokens)


# ── Export ─────────────────────────────────────────────────────────────
def build_export(db: Session, include_secrets: bool = False, passphrase: str | None = None) -> dict:
    machines = db.query(Machine).order_by(Machine.id).all()
    devices = db.query(Device).order_by(Device.id).all()
    machine_uuid = {machine.id: machine.uuid for machine in machines}

    doc: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "app_version": app_version(),
        "exported_at": _now(),
        "include_secrets": bool(include_secrets),
        "machines": [
            {
                "uuid": machine.uuid,
                "has_secret": machine.secret_ref is not None,
                **{field: getattr(machine, field) for field in MACHINE_FIELDS},
                "created_at": machine.created_at,
                "updated_at": machine.updated_at,
            }
            for machine in machines
        ],
        "devices": [
            {
                "uuid": device.uuid,
                "machine_uuid": machine_uuid.get(device.machine_id),
                **{field: getattr(device, field) for field in DEVICE_FIELDS if field != "machine_uuid"},
                "created_at": device.created_at,
                "updated_at": device.updated_at,
            }
            for device in devices
        ],
        "settings": _settings_snapshot(db),
        "ai": {
            "providers": [
                {
                    "which": provider.which,
                    "has_api_key": provider.api_key_ref is not None,
                    **{field: getattr(provider, field) for field in PROVIDER_FIELDS},
                }
                for provider in db.query(AiProvider).order_by(AiProvider.which).all()
            ]
        },
        "secrets": None,
    }

    if include_secrets:
        if not passphrase:
            raise ConfigError("phrase de passe requise pour inclure les secrets")
        plain: dict[str, str] = {}
        for machine in machines:
            if machine.secret_ref:
                plain[machine.uuid] = decrypt_secret(machine.secret_ref)
        ai_plain: dict[str, str] = {}
        for provider in db.query(AiProvider).all():
            if provider.api_key_ref:
                ai_plain[provider.which] = decrypt_secret(provider.api_key_ref)
        ssh_key = None
        try:
            from app.ai_config import get_setting as _get_setting
            from app.fossor_keys import PRIVATE_KEY_SETTING, private_key_text

            if _get_setting(db, PRIVATE_KEY_SETTING, None):
                ssh_key = private_key_text(db)
        except Exception:
            ssh_key = None
        notif_plain: dict[str, str] = {}
        try:
            from app import notifications as _notifications

            notif_plain = _notifications.all_plain_secrets(db)
        except Exception:
            notif_plain = {}
        doc["secrets"] = _encrypt_secrets(
            plain, ai_plain, passphrase, ssh_key, notif_plain
        )
    return doc


def data_dir() -> Path:
    return Path(os.environ.get("FOSSOR_DATA_DIR") or "/data")


def backup_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_current_config(db: Session) -> str:
    """Write the current config (no secrets) to ``<data>/backups/config-<ts>.json``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir() / f"config-{stamp}.json"
    suffix = 1
    while path.exists():
        suffix += 1
        path = backup_dir() / f"config-{stamp}-{suffix}.json"
    path.write_text(
        json.dumps(build_export(db, include_secrets=False), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)


# ── Validation ─────────────────────────────────────────────────────────
def _validate_ai(doc: dict) -> dict[str, dict]:
    """Validate the optional ``ai`` block; return providers by ``which``."""
    ai = doc.get("ai")
    if ai is None:
        return {}
    if not isinstance(ai, dict):
        raise ConfigError("Le champ « ai » doit être un objet JSON")
    providers = ai.get("providers")
    if providers is None:
        return {}
    if not isinstance(providers, list):
        raise ConfigError("ai.providers doit être une liste")
    incoming: dict[str, dict] = {}
    for position, provider in enumerate(providers):
        if not isinstance(provider, dict):
            raise ConfigError(f"ai.providers[{position}] n'est pas un objet")
        which = provider.get("which")
        if which not in WHICHES:
            raise ConfigError(f"ai.providers[{position}] : which invalide ({which!r})")
        if which in incoming:
            raise ConfigError(f"ai.providers[{position}] : which en double ({which})")
        if provider.get("provider_type") not in PROVIDER_TYPES:
            raise ConfigError(f"ai.providers[{position}] : provider_type invalide")
        incoming[which] = provider
    return incoming


def validate_document(doc: Any) -> None:
    """Strict schema check; every failure is a clear ConfigError (HTTP 400)."""
    if not isinstance(doc, dict):
        raise ConfigError("Le document importé doit être un objet JSON")
    for field in ("format_version", "app_version", "machines", "devices", "settings"):
        if field not in doc:
            raise ConfigError(f"Champ obligatoire manquant : « {field} »")

    format_version = doc["format_version"]
    if format_version != FORMAT_VERSION:
        raise ConfigError(
            f"format_version inconnu : {format_version!r} (cette version de Fossor lit {FORMAT_VERSION})"
        )
    if not isinstance(doc["app_version"], str) or not doc["app_version"].strip():
        raise ConfigError("Champ « app_version » invalide")
    if not isinstance(doc["machines"], list) or not isinstance(doc["devices"], list):
        raise ConfigError("Les champs « machines » et « devices » doivent être des listes")
    if not isinstance(doc["settings"], dict):
        raise ConfigError("Le champ « settings » doit être un objet JSON")

    machine_uuids: set[str] = set()
    for position, machine in enumerate(doc["machines"]):
        if not isinstance(machine, dict):
            raise ConfigError(f"machines[{position}] n'est pas un objet")
        for field in MACHINE_REQUIRED:
            if not machine.get(field):
                raise ConfigError(f"machines[{position}] : champ « {field} » manquant")
        if machine["uuid"] in machine_uuids:
            raise ConfigError(f"machines[{position}] : uuid en double ({machine['uuid']})")
        machine_uuids.add(machine["uuid"])

    device_uuids: set[str] = set()
    for position, device in enumerate(doc["devices"]):
        if not isinstance(device, dict):
            raise ConfigError(f"devices[{position}] n'est pas un objet")
        for field in DEVICE_REQUIRED:
            if not device.get(field):
                raise ConfigError(f"devices[{position}] : champ « {field} » manquant")
        if device["kind"] not in ("cpu", "gpu"):
            raise ConfigError(f"devices[{position}] : kind invalide ({device['kind']!r})")
        if device["uuid"] in device_uuids:
            raise ConfigError(f"devices[{position}] : uuid en double ({device['uuid']})")
        device_uuids.add(device["uuid"])
        if device["machine_uuid"] not in machine_uuids:
            raise ConfigError(
                f"devices[{position}] : machine_uuid inconnu ({device['machine_uuid']})"
            )

    _validate_ai(doc)


# ── Diff ───────────────────────────────────────────────────────────────
def _machine_values(row: dict) -> dict:
    return {field: row[field] for field in MACHINE_FIELDS if field in row}


def _device_values(row: dict) -> dict:
    return {field: row[field] for field in DEVICE_FIELDS if field in row}


def compute_diff(db: Session, doc: dict) -> dict:
    """Additions / modifications / deletions compared by uuid, without writing."""
    machines = {machine.uuid: machine for machine in db.query(Machine).all()}
    devices = {device.uuid: device for device in db.query(Device).all()}
    machine_uuid_by_id = {machine.id: machine.uuid for machine in db.query(Machine).all()}
    settings = _settings_snapshot(db)

    incoming_machines = {row["uuid"]: row for row in doc["machines"]}
    incoming_devices = {row["uuid"]: row for row in doc["devices"]}
    incoming_settings = doc["settings"]
    incoming_ai = _validate_ai(doc)
    current_ai = {provider.which: provider for provider in db.query(AiProvider).all()}

    machine_modified = []
    for uuid, row in incoming_machines.items():
        current = machines.get(uuid)
        if current is None:
            continue
        if any(getattr(current, key) != value for key, value in _machine_values(row).items()):
            machine_modified.append({"uuid": uuid, "name": row.get("name")})

    device_modified = []
    for uuid, row in incoming_devices.items():
        current = devices.get(uuid)
        if current is None:
            continue
        incoming_values = _device_values(row)
        current_values = {
            key: getattr(current, key) for key in incoming_values if key != "machine_uuid"
        }
        current_values["machine_uuid"] = machine_uuid_by_id.get(current.machine_id)
        if current_values != incoming_values:
            device_modified.append({"uuid": uuid, "model": row.get("model"), "kind": row.get("kind")})

    ai_modified = [
        {"which": which, "name": row.get("name") or which}
        for which, row in incoming_ai.items()
        if which in current_ai
        and (
            any(getattr(current_ai[which], field) != row.get(field) for field in PROVIDER_FIELDS)
            or bool(current_ai[which].api_key_ref) != bool(row.get("has_api_key"))
        )
    ]

    return {
        "machines": {
            "added": [{"uuid": u, "name": r.get("name")} for u, r in incoming_machines.items() if u not in machines],
            "modified": machine_modified,
            "removed": [{"uuid": u, "name": m.name} for u, m in machines.items() if u not in incoming_machines],
            "unchanged": len(incoming_machines) - len(machine_modified)
            - len([u for u in incoming_machines if u not in machines]),
        },
        "devices": {
            "added": [{"uuid": u, "model": r.get("model"), "kind": r.get("kind")}
                      for u, r in incoming_devices.items() if u not in devices],
            "modified": device_modified,
            "removed": [{"uuid": u, "model": d.model, "kind": d.kind}
                        for u, d in devices.items() if u not in incoming_devices],
            "unchanged": len(incoming_devices) - len(device_modified)
            - len([u for u in incoming_devices if u not in devices]),
        },
        "settings": {
            "added": sorted(k for k in incoming_settings if k not in settings),
            "modified": sorted(k for k in incoming_settings if k in settings and settings[k] != incoming_settings[k]),
            "removed": sorted(k for k in settings if k not in incoming_settings),
        },
        "ai": {
            "added": [{"which": w, "name": r.get("name") or w} for w, r in incoming_ai.items() if w not in current_ai],
            "modified": ai_modified,
            "removed": [{"which": w} for w in current_ai if w not in incoming_ai],
        },
    }


# ── Import ─────────────────────────────────────────────────────────────
def _apply_values(target, values: dict) -> bool:
    changed = False
    for key, value in values.items():
        if getattr(target, key) != value:
            setattr(target, key, value)
            changed = True
    if changed:
        target.updated_at = _now()
    return changed


def apply_import(
    db: Session,
    doc: dict,
    mode: str = "merge",
    passphrase: str | None = None,
) -> dict:
    """Backup, then apply *doc*.  Returns counters and the backup path."""
    validate_document(doc)
    if mode not in ("merge", "replace"):
        raise ConfigError("mode invalide : « merge » ou « replace » attendu")

    # Decrypt before touching anything: a wrong passphrase must write nothing.
    machine_secrets, ai_secrets, ssh_key, notif_secrets = _decrypt_secrets(doc, passphrase)

    backup_path = backup_current_config(db)

    machines = {machine.uuid: machine for machine in db.query(Machine).all()}
    devices = {device.uuid: device for device in db.query(Device).all()}
    incoming_machines = {row["uuid"]: row for row in doc["machines"]}
    incoming_devices = {row["uuid"]: row for row in doc["devices"]}
    incoming_ai = _validate_ai(doc)

    stats = {
        "machines": {"created": 0, "updated": 0, "deleted": 0},
        "devices": {"created": 0, "updated": 0, "deleted": 0},
        "settings": {"created": 0, "updated": 0, "deleted": 0},
        "ai": {"created": 0, "updated": 0, "deleted": 0},
    }

    if mode == "replace":
        for uuid, device in list(devices.items()):
            if uuid not in incoming_devices:
                db.delete(device)
                stats["devices"]["deleted"] += 1
        for uuid, machine in list(machines.items()):
            if uuid not in incoming_machines:
                db.delete(machine)
                stats["machines"]["deleted"] += 1
        db.flush()

    for uuid, row in incoming_machines.items():
        values = _machine_values(row)
        machine = machines.get(uuid)
        if machine is None:
            machine = Machine(uuid=uuid, created_at=row.get("created_at") or _now())
            db.add(machine)
            stats["machines"]["created"] += 1
        else:
            stats["machines"]["updated"] += 1
        for key, value in values.items():
            setattr(machine, key, value)
        machine.updated_at = _now()
        db.flush()
        machines[uuid] = machine
        if uuid in machine_secrets:
            machine.secret_ref = encrypt_secret(machine_secrets[uuid])
    db.flush()

    for uuid, row in incoming_devices.items():
        values = _device_values(row)
        machine = machines[row["machine_uuid"]]
        device = devices.get(uuid)
        if device is None:
            device = Device(
                uuid=uuid,
                machine_id=machine.id,
                kind=row["kind"],
                created_at=row.get("created_at") or _now(),
            )
            db.add(device)
            stats["devices"]["created"] += 1
        else:
            stats["devices"]["updated"] += 1
        device.machine_id = machine.id
        for key, value in values.items():
            if key == "machine_uuid":
                continue
            setattr(device, key, value)
        device.updated_at = _now()
        db.flush()

    existing_settings = {row.key: row for row in db.query(AppSetting).all()}
    if mode == "replace":
        for key, row in list(existing_settings.items()):
            if key in NON_EXPORTED_SETTINGS:
                continue  # the inbound token is local, never imported away
            if key not in doc["settings"]:
                db.delete(row)
                stats["settings"]["deleted"] += 1
    for key, value in doc["settings"].items():
        if key in NON_EXPORTED_SETTINGS:
            continue
        row = existing_settings.get(key)
        if row is None:
            db.add(AppSetting(key=key, value=json.dumps(value, ensure_ascii=False), updated_at=_now()))
            stats["settings"]["created"] += 1
        else:
            row.value = json.dumps(value, ensure_ascii=False)
            row.updated_at = _now()
            stats["settings"]["updated"] += 1

    # AI providers (§4.12): same uuid-less upsert on ``which``.
    existing_ai = {provider.which: provider for provider in db.query(AiProvider).all()}
    if mode == "replace":
        for which, provider in list(existing_ai.items()):
            if which not in incoming_ai:
                db.delete(provider)
                stats["ai"]["deleted"] += 1
    for which, row in incoming_ai.items():
        values = {field: row[field] for field in PROVIDER_FIELDS if field in row}
        provider = existing_ai.get(which)
        if provider is None:
            provider = AiProvider(which=which, created_at=row.get("updated_at") or _now())
            db.add(provider)
            stats["ai"]["created"] += 1
        else:
            stats["ai"]["updated"] += 1
        for key, value in values.items():
            setattr(provider, key, value)
        provider.updated_at = _now()
        if which in ai_secrets:
            provider.api_key_ref = encrypt_secret(ai_secrets[which])
    db.flush()

    # §4.1 bis: restore Fossor's own SSH identity when the file carries it.
    if ssh_key:
        from app.fossor_keys import restore_private_key

        restore_private_key(ssh_key, db)

    # §4.14: notification secrets (write-only, encrypted with the master key).
    if notif_secrets:
        from app import notifications as _notifications

        _notifications.apply_secrets(db, notif_secrets)

    db.commit()
    return {"mode": mode, "backup": backup_path, "stats": stats}
