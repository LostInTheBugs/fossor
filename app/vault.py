"""Coffre des wallets — seeds, clés privées et mots de passe.

La donnée la plus sensible de l'installation.  Trois règles structurent ce
module :

1. **Phrase maîtresse jamais stockée.**  Une clé de 32 octets est dérivée par
   Argon2id (paramètres et sel stockés avec le chiffré), et sert à chiffrer
   AES-256-GCM le vérificateur et la clé privée X25519.  Perdre la phrase =
   perdre le coffre : aucun recours, aucun secret de secours.
2. **Dépôt scellé.**  Les secrets des entrées sont des *sealed boxes* PyNaCl
   vers la clé publique X25519 du coffre.  Hermes peut déposer sans jamais
   pouvoir relire ; seul un coffre déverrouillé ouvre les entrées.
3. **Rien ne fuit.**  Ce module ne journalise jamais une valeur secrète, ne
   l'insère jamais dans un message d'erreur ni dans l'audit.  Les fonctions
   publiques renvoient des métadonnées ou, pour ``reveal`` uniquement, une
   valeur temporaire.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from nacl.public import PrivateKey, PublicKey, SealedBox
from sqlalchemy.orm import Session

from app.models import AuditLog, VaultMeta, VaultWallet

# ── Paramètres cryptographiques (documentés et stockés avec le sel) ──────────
KDF_ALGORITHM = "argon2id"
KDF_TIME_COST = 3
KDF_MEMORY_KIB = 65536
KDF_PARALLELISM = 4
KDF_HASH_LEN = 32
KDF_VERSION = 19
AEAD_ALGORITHM = "aes-256-gcm"

VERIFIER_PLAINTEXT = b"fossor-vault-verifier-v1"
AAD_VERIFIER = b"fossor-vault-verifier"
AAD_PRIVATE_KEY = b"fossor-vault-x25519"

DEFAULT_LOCK_MINUTES = 10
DEFAULT_REVEAL_SECONDS = 30
MAX_FAILURES = 5
BLOCK_MINUTES = 15
MIN_PASSPHRASE = 12

SECRET_FIELDS = ("seed", "private_key", "password")
FIELD_LABELS = {
    "seed": "seed",
    "private_key": "clé privée",
    "password": "mot de passe",
}
ORIGINS = ("fred", "hermes", "import")


class VaultError(Exception):
    """Erreur du coffre portant le code HTTP à renvoyer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ── Session en mémoire uniquement ───────────────────────────────────────────
@dataclass
class _Session:
    key: bytes
    private_key: bytes
    expires_at: float
    lock_minutes: int


@dataclass
class _Runtime:
    session: _Session | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_runtime = _Runtime()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(str(text or ""), validate=True)


def _session_seconds_override() -> int | None:
    """Durée de session en secondes (tests) — la valeur de production est en minutes."""
    raw = os.environ.get("FOSSOR_VAULT_LOCK_SECONDS")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def reveal_seconds() -> int:
    raw = os.environ.get("FOSSOR_VAULT_REVEAL_SECONDS")
    try:
        value = int(raw) if raw not in (None, "") else DEFAULT_REVEAL_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_REVEAL_SECONDS
    return max(1, min(value, 300))


# ── Dérivation et chiffrement authentifié ───────────────────────────────────
def derive_key(
    passphrase: str,
    salt: bytes,
    *,
    time_cost: int = KDF_TIME_COST,
    memory_kib: int = KDF_MEMORY_KIB,
    parallelism: int = KDF_PARALLELISM,
) -> bytes:
    """Argon2id — 32 octets, sel et paramètres stockés avec le chiffré."""
    from argon2.low_level import Type, hash_secret_raw

    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=int(time_cost),
        memory_cost=int(memory_kib),
        parallelism=int(parallelism),
        hash_len=KDF_HASH_LEN,
        type=Type.ID,
        version=KDF_VERSION,
    )


def aead_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def aead_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        raise VaultError("Chiffré illisible : phrase incorrecte ou donnée altérée", 400) from None


def seal(public_key_b64: str, plaintext: str) -> str:
    box = SealedBox(PublicKey(_b64d(public_key_b64)))
    return _b64e(box.encrypt(plaintext.encode("utf-8")))


def open_sealed(private_key: bytes, sealed_b64: str) -> str:
    try:
        box = SealedBox(PrivateKey(private_key))
        return box.decrypt(_b64d(sealed_b64)).decode("utf-8")
    except Exception:
        raise VaultError("Entrée scellée illisible : chiffré altéré", 400) from None


# ── Métadonnées du coffre ───────────────────────────────────────────────────
def get_meta(db: Session) -> VaultMeta | None:
    return db.get(VaultMeta, 1)


def is_initialized(db: Session) -> bool:
    return get_meta(db) is not None


def require_initialized(db: Session) -> VaultMeta:
    meta = get_meta(db)
    if meta is None:
        raise VaultError("Coffre non initialisé", 409)
    return meta


def _check_passphrase_policy(passphrase: str, confirm: str | None = None) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE:
        raise VaultError(f"Phrase maîtresse trop courte : {MIN_PASSPHRASE} caractères minimum", 400)
    if confirm is not None and passphrase != confirm:
        raise VaultError("Les deux saisies de la phrase maîtresse diffèrent", 400)


def init_vault(
    db: Session,
    passphrase: str,
    confirm: str,
    *,
    lock_minutes: int = DEFAULT_LOCK_MINUTES,
    actor: str = "admin",
) -> VaultMeta:
    """Crée le coffre : sel, vérificateur, paire X25519, clé privée scellée."""
    if is_initialized(db):
        raise VaultError("Coffre déjà initialisé", 409)
    _check_passphrase_policy(passphrase, confirm)
    lock_minutes = _clamp_lock_minutes(lock_minutes)
    salt = os.urandom(16)
    key = derive_key(passphrase, salt)
    verifier_nonce, verifier_ct = aead_encrypt(key, VERIFIER_PLAINTEXT, AAD_VERIFIER)
    pair = PrivateKey.generate()
    private_raw = bytes(pair)
    public_b64 = _b64e(bytes(pair.public_key))
    wrapped_nonce, wrapped_ct = aead_encrypt(key, private_raw, AAD_PRIVATE_KEY)
    meta = VaultMeta(
        id=1,
        kdf_algorithm=KDF_ALGORITHM,
        kdf_time_cost=KDF_TIME_COST,
        kdf_memory_kib=KDF_MEMORY_KIB,
        kdf_parallelism=KDF_PARALLELISM,
        kdf_salt=_b64e(salt),
        verifier_nonce=_b64e(verifier_nonce),
        verifier_ct=_b64e(verifier_ct),
        wrapped_nonce=_b64e(wrapped_nonce),
        wrapped_ct=_b64e(wrapped_ct),
        public_key=public_b64,
        lock_minutes=lock_minutes,
        failed_attempts=0,
        blocked_until=None,
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    db.add(meta)
    db.commit()
    _install_session(key, private_raw, lock_minutes)
    audit(db, actor, "vault-init", "vault", "ok", "coffre initialisé")
    return meta


def _clamp_lock_minutes(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_LOCK_MINUTES
    return max(1, min(minutes, 120))


def _install_session(key: bytes, private_raw: bytes, lock_minutes: int) -> None:
    override = _session_seconds_override()
    seconds = override if override is not None else lock_minutes * 60
    with _runtime.lock:
        _runtime.session = _Session(
            key=key,
            private_key=private_raw,
            expires_at=time.monotonic() + seconds,
            lock_minutes=lock_minutes,
        )


def _drop_session() -> None:
    with _runtime.lock:
        _runtime.session = None


def lock() -> None:
    """Verrouille : la clé et la clé privée quittent la mémoire."""
    _drop_session()


def is_unlocked() -> bool:
    with _runtime.lock:
        session = _runtime.session
        if session is None:
            return False
        if time.monotonic() >= session.expires_at:
            _runtime.session = None
            return False
        return True


def unlock_remaining() -> int:
    with _runtime.lock:
        session = _runtime.session
        if session is None:
            return 0
        remaining = session.expires_at - time.monotonic()
        if remaining <= 0:
            _runtime.session = None
            return 0
        # Arrondi au supérieur : une session de 1 s ne doit pas être déclarée
        # expirée par troncature à 0,999 s.
        return max(1, int(remaining) + (1 if remaining % 1 else 0))


def _session() -> _Session:
    with _runtime.lock:
        session = _runtime.session
        if session is None or time.monotonic() >= session.expires_at:
            _runtime.session = None
            raise VaultError("Coffre verrouillé", 423)
        return session


# ── Échecs et blocage ───────────────────────────────────────────────────────
def _blocked_until(meta: VaultMeta) -> datetime | None:
    raw = meta.blocked_until
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _check_not_blocked(meta: VaultMeta) -> None:
    until = _blocked_until(meta)
    if until is None:
        return
    now = datetime.now(timezone.utc)
    if until > now:
        remaining = int((until - now).total_seconds())
        raise VaultError(
            f"Coffre bloqué après {MAX_FAILURES} échecs : réessayer dans {remaining} s", 423
        )
    # Blocage expiré : le compteur repart à zéro.
    meta.blocked_until = None
    meta.failed_attempts = 0


def _note_failure(db: Session, meta: VaultMeta, actor: str, action: str) -> bool:
    """Compte un échec ; renvoie True quand le blocage vient d'être posé."""
    meta.failed_attempts = int(meta.failed_attempts or 0) + 1
    blocked = ""
    just_blocked = False
    if meta.failed_attempts >= MAX_FAILURES:
        meta.blocked_until = (
            datetime.now(timezone.utc) + timedelta(minutes=BLOCK_MINUTES)
        ).isoformat()
        blocked = f" ; blocage {BLOCK_MINUTES} min"
        just_blocked = True
    meta.updated_at = _now_iso()
    db.commit()
    audit(
        db,
        actor,
        action,
        "vault",
        "refusé",
        f"{meta.failed_attempts}/{MAX_FAILURES} échec(s){blocked}",
    )
    return just_blocked


def verify_passphrase(
    db: Session, passphrase: str, *, actor: str, action: str = "vault-unlock-failed"
) -> tuple[VaultMeta, bytes]:
    """Dérive la clé et valide le vérificateur ; compte les échecs."""
    meta = require_initialized(db)
    _check_not_blocked(meta)
    salt = _b64d(meta.kdf_salt)
    key = derive_key(
        passphrase,
        salt,
        time_cost=meta.kdf_time_cost,
        memory_kib=meta.kdf_memory_kib,
        parallelism=meta.kdf_parallelism,
    )
    try:
        plaintext = aead_decrypt(
            key, _b64d(meta.verifier_nonce), _b64d(meta.verifier_ct), AAD_VERIFIER
        )
    except VaultError:
        plaintext = b""
    if plaintext != VERIFIER_PLAINTEXT:
        just_blocked = _note_failure(db, meta, actor, action)
        if just_blocked:
            raise VaultError(
                f"Coffre bloqué après {MAX_FAILURES} échecs : réessayer dans "
                f"{BLOCK_MINUTES * 60} s",
                423,
            )
        raise VaultError("Phrase maîtresse incorrecte", 401)
    if meta.failed_attempts:
        meta.failed_attempts = 0
        meta.blocked_until = None
        db.commit()
    return meta, key


def unlock(db: Session, passphrase: str, *, actor: str) -> dict:
    meta, key = verify_passphrase(db, passphrase, actor=actor)
    private_raw = aead_decrypt(
        key, _b64d(meta.wrapped_nonce), _b64d(meta.wrapped_ct), AAD_PRIVATE_KEY
    )
    _install_session(key, private_raw, int(meta.lock_minutes or DEFAULT_LOCK_MINUTES))
    audit(db, actor, "vault-unlock", "vault", "ok", "coffre déverrouillé")
    return status(db)


def change_passphrase(
    db: Session, current: str, new: str, confirm: str, *, actor: str
) -> dict:
    """Re-chiffre tout : nouvelle paire X25519, nouveau sel, nouveau vérificateur."""
    _check_passphrase_policy(new, confirm)
    meta, old_key = verify_passphrase(
        db, current, actor=actor, action="vault-passphrase-failed"
    )
    if not is_unlocked():
        raise VaultError("Déverrouiller le coffre avant de changer la phrase", 423)
    old_private = aead_decrypt(
        old_key, _b64d(meta.wrapped_nonce), _b64d(meta.wrapped_ct), AAD_PRIVATE_KEY
    )
    rows = db.query(VaultWallet).all()
    # Tout ouvrir AVANT d'écrire : une entrée altérée annule le changement.
    opened: list[tuple[VaultWallet, dict[str, str]]] = []
    for row in rows:
        values: dict[str, str] = {}
        for name in SECRET_FIELDS:
            sealed = getattr(row, f"sealed_{name}")
            if sealed:
                values[name] = open_sealed(old_private, sealed)
        opened.append((row, values))

    salt = os.urandom(16)
    key = derive_key(new, salt)
    verifier_nonce, verifier_ct = aead_encrypt(key, VERIFIER_PLAINTEXT, AAD_VERIFIER)
    pair = PrivateKey.generate()
    private_raw = bytes(pair)
    public_b64 = _b64e(bytes(pair.public_key))
    wrapped_nonce, wrapped_ct = aead_encrypt(key, private_raw, AAD_PRIVATE_KEY)
    for row, values in opened:
        for name in SECRET_FIELDS:
            if name in values:
                setattr(row, f"sealed_{name}", seal(public_b64, values[name]))
        row.updated_at = _now_iso()
    meta.kdf_salt = _b64e(salt)
    meta.verifier_nonce = _b64e(verifier_nonce)
    meta.verifier_ct = _b64e(verifier_ct)
    meta.wrapped_nonce = _b64e(wrapped_nonce)
    meta.wrapped_ct = _b64e(wrapped_ct)
    meta.public_key = public_b64
    meta.failed_attempts = 0
    meta.blocked_until = None
    meta.updated_at = _now_iso()
    db.commit()
    _install_session(key, private_raw, int(meta.lock_minutes or DEFAULT_LOCK_MINUTES))
    audit(db, actor, "vault-passphrase", "vault", "ok", "phrase maîtresse changée, entrées re-chiffrées")
    return status(db)


def set_lock_minutes(db: Session, minutes: Any, *, actor: str) -> dict:
    meta = require_initialized(db)
    meta.lock_minutes = _clamp_lock_minutes(minutes)
    meta.updated_at = _now_iso()
    db.commit()
    if is_unlocked():
        with _runtime.lock:
            if _runtime.session is not None:
                override = _session_seconds_override()
                seconds = override if override is not None else meta.lock_minutes * 60
                _runtime.session.lock_minutes = meta.lock_minutes
                _runtime.session.expires_at = time.monotonic() + seconds
    audit(db, actor, "vault-settings", "vault", "ok", f"verrouillage après {meta.lock_minutes} min")
    return status(db)


# ── Audit : jamais de valeur secrète ─────────────────────────────────────────
def audit(db: Session, actor: str, action: str, ref: str, result: str, output: str = "") -> None:
    db.add(
        AuditLog(
            actor=str(actor)[:20],
            action=action[:50],
            object_ref=ref[:255],
            result=result[:200],
            output=(output or "")[:500],
        )
    )
    db.commit()


# ── Vérification d'adresse ──────────────────────────────────────────────────
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

COIN_RULES: dict[str, dict[str, Any]] = {
    "BTC": {"bech32": ("bc",), "versions": (0x00, 0x05)},
    "LTC": {"bech32": ("ltc",), "versions": (0x30, 0x32, 0x05)},
    "DOGE": {"versions": (0x1E, 0x16)},
    "DASH": {"versions": (0x4C, 0x10, 0x13)},
    "BCH": {"versions": (0x00, 0x05)},
    "ZEC": {"versions": (0x1CB8, 0x1CBD)},
    "DGB": {"versions": (0x1E, 0x3F)},
    "RVN": {"versions": (0x3C, 0x7A)},
    "KAS": {"bech32": ("kaspa",)},
    "KASPA": {"bech32": ("kaspa",)},
}


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= generator[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_decode(address: str) -> tuple[str, list[int]] | None:
    if not address or any(ord(c) < 33 or ord(c) > 126 for c in address):
        return None
    if address.lower() != address and address.upper() != address:
        return None
    text = address.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text):
        return None
    hrp = text[:pos]
    try:
        data = [_BECH32.index(c) for c in text[pos + 1 :]]
    except ValueError:
        return None
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        return None
    return hrp, data[:-6]


def _b58decode(address: str) -> bytes | None:
    num = 0
    for char in address:
        idx = _B58.find(char)
        if idx < 0:
            return None
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(address) - len(address.lstrip("1"))
    return b"\x00" * pad + raw


def verify_address(coin: str, address: str) -> tuple[str, str]:
    """Renvoie ``(statut, note)`` : ``ok``, ``invalid`` ou ``unverified``."""
    rule = COIN_RULES.get(str(coin or "").strip().upper())
    text = str(address or "").strip()
    if rule is None:
        return "unverified", "contrôle d'adresse non disponible pour ce coin"
    if not text:
        return "invalid", "adresse vide"
    prefixes = rule.get("bech32") or ()
    lowered = text.lower()
    if prefixes and lowered.startswith(tuple(p + "1" for p in prefixes)):
        decoded = _bech32_decode(text)
        if decoded is None:
            return "invalid", "somme de contrôle bech32 invalide"
        hrp, payload = decoded
        if hrp not in prefixes:
            return "invalid", f"préfixe bech32 inattendu : {hrp}"
        if len(payload) < 6:
            return "invalid", "charge utile bech32 trop courte"
        return "ok", ""
    versions = rule.get("versions") or ()
    if versions:
        raw = _b58decode(text)
        if raw is None or len(raw) < 5:
            return "invalid", "adresse base58 illisible"
        payload, checksum = raw[:-4], raw[-4:]
        if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
            return "invalid", "somme de contrôle base58 invalide"
        version = payload[0]
        if version not in versions:
            return "invalid", f"version d'adresse inattendue : 0x{version:02x}"
        return "ok", ""
    return "invalid", "format d'adresse inconnu pour ce coin"


# ── Entrées du coffre ───────────────────────────────────────────────────────
def _wallet_dict(row: VaultWallet) -> dict:
    out = {
        "id": row.id,
        "uuid": row.uuid,
        "coin": row.coin,
        "network": row.network or "",
        "address": row.address,
        "address_short": _short(row.address),
        "machine": row.machine or "",
        "wallet_path": row.wallet_path or "",
        "note": row.note or "",
        "origin": row.origin or "fred",
        "address_status": row.address_status or "unverified",
        "address_note": row.address_note or "",
        "has_seed": bool(row.sealed_seed),
        "has_private_key": bool(row.sealed_private_key),
        "has_password": bool(row.sealed_password),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    return out


def _short(address: str, keep: int = 8) -> str:
    text = str(address or "")
    if len(text) <= keep * 2 + 1:
        return text
    return f"{text[:keep]}…{text[-keep:]}"


def list_wallets(db: Session) -> list[dict]:
    rows = db.query(VaultWallet).order_by(VaultWallet.id.asc()).all()
    return [_wallet_dict(row) for row in rows]


def _clean_metadata(data: dict) -> dict:
    coin = str(data.get("coin") or "").strip().upper()
    address = str(data.get("address") or "").strip()
    if not coin:
        raise VaultError("Coin obligatoire", 400)
    if not address:
        raise VaultError("Adresse obligatoire", 400)
    return {
        "coin": coin[:20],
        "network": str(data.get("network") or "").strip()[:40],
        "address": address[:160],
        "machine": str(data.get("machine") or "").strip()[:80],
        "wallet_path": str(data.get("wallet_path") or "").strip()[:255],
        "note": str(data.get("note") or "").strip()[:500],
    }


def add_wallet(
    db: Session,
    data: dict,
    secrets: dict[str, str] | None = None,
    *,
    actor: str,
    origin: str = "fred",
) -> dict:
    """Ajout manuel : les secrets en clair sont scellés immédiatement."""
    meta = require_initialized(db)
    session = _session()
    fields = _clean_metadata(data)
    status_text, note = verify_address(fields["coin"], fields["address"])
    row = VaultWallet(
        uuid=os.urandom(16).hex(),
        **fields,
        origin=origin if origin in ORIGINS else "fred",
        address_status=status_text,
        address_note=note,
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    for name in SECRET_FIELDS:
        value = (secrets or {}).get(name)
        if value:
            if not isinstance(value, str):
                raise VaultError(f"Secret {FIELD_LABELS[name]} invalide", 400)
            setattr(row, f"sealed_{name}", seal(meta.public_key, value))
    db.add(row)
    db.commit()
    audit(
        db,
        actor,
        "vault-wallet-add",
        f"wallet:{row.id}",
        "ok",
        f"{row.coin} {_short(row.address)} ; secrets={','.join(_present(row)) or 'aucun'}",
    )
    _ = session
    return _wallet_dict(row)


def _present(row: VaultWallet) -> list[str]:
    return [name for name in SECRET_FIELDS if getattr(row, f"sealed_{name}")]


def update_wallet(
    db: Session, wallet_id: int, data: dict, secrets: dict[str, str] | None = None, *, actor: str
) -> dict:
    meta = require_initialized(db)
    _session()
    row = db.get(VaultWallet, int(wallet_id))
    if row is None:
        raise VaultError("Wallet introuvable", 404)
    fields = _clean_metadata({**{
        "coin": row.coin,
        "address": row.address,
        "network": row.network,
        "machine": row.machine,
        "wallet_path": row.wallet_path,
        "note": row.note,
    }, **{k: v for k, v in data.items() if v is not None}})
    changed = False
    for key, value in fields.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    if row.coin != fields["coin"] or row.address != fields["address"]:
        row.address_status, row.address_note = verify_address(row.coin, row.address)
    for name in SECRET_FIELDS:
        value = (secrets or {}).get(name)
        if value:
            setattr(row, f"sealed_{name}", seal(meta.public_key, value))
            changed = True
    row.updated_at = _now_iso()
    db.commit()
    audit(db, actor, "vault-wallet-edit", f"wallet:{row.id}", "ok",
          "modifié" if changed else "aucun changement")
    return _wallet_dict(row)


def delete_wallet(db: Session, wallet_id: int, *, actor: str) -> dict:
    require_initialized(db)
    _session()
    row = db.get(VaultWallet, int(wallet_id))
    if row is None:
        raise VaultError("Wallet introuvable", 404)
    db.delete(row)
    db.commit()
    audit(db, actor, "vault-wallet-delete", f"wallet:{wallet_id}", "ok", "entrée supprimée")
    return {"deleted": True, "id": int(wallet_id)}


def reveal(db: Session, wallet_id: int, name: str, passphrase: str, *, actor: str) -> dict:
    """Révélation temporaire : coffre déverrouillé **et** phrase ressaisie."""
    if name not in SECRET_FIELDS:
        raise VaultError("Champ inconnu", 400)
    require_initialized(db)
    if not is_unlocked():
        raise VaultError("Coffre verrouillé", 423)
    verify_passphrase(db, passphrase, actor=actor, action="vault-reveal-failed")
    row = db.get(VaultWallet, int(wallet_id))
    if row is None:
        raise VaultError("Wallet introuvable", 404)
    sealed = getattr(row, f"sealed_{name}")
    if not sealed:
        raise VaultError(f"Aucun(e) {FIELD_LABELS[name]} enregistré(e) pour ce wallet", 404)
    session = _session()
    value = open_sealed(session.private_key, sealed)
    audit(
        db,
        actor,
        "vault-reveal",
        f"wallet:{row.id}:{name}",
        "ok",
        f"{row.coin} {_short(row.address)} ; champ={FIELD_LABELS[name]} ; {reveal_seconds()} s",
    )
    return {
        "id": row.id,
        "field": name,
        "value": value,
        "expires_in": reveal_seconds(),
    }


# ── Dépôt Hermes : scellé, jamais relu par l'API ────────────────────────────
def public_key(db: Session) -> str:
    meta = require_initialized(db)
    return meta.public_key


def hermes_deposit(db: Session, payload: dict) -> dict:
    """Dépose un wallet scellé côté Hermes ; aucune relecture possible ici."""
    meta = require_initialized(db)
    fields = _clean_metadata(payload)
    sealed: dict[str, str] = {}
    kind = str(payload.get("secret_kind") or "").strip()
    single = payload.get("sealed_secret")
    if single:
        if kind not in SECRET_FIELDS:
            raise VaultError("secret_kind doit valoir seed, private_key ou password", 400)
        sealed[kind] = str(single)
    multi = payload.get("secrets")
    if multi is not None:
        if not isinstance(multi, dict):
            raise VaultError("secrets doit être un objet champ → sealed box base64", 400)
        for name, value in multi.items():
            if name not in SECRET_FIELDS:
                raise VaultError(f"Champ secret inconnu : {name}", 400)
            if value:
                sealed[name] = str(value)
    if not sealed:
        raise VaultError("Aucun secret scellé fourni", 400)
    for name, value in sealed.items():
        try:
            raw = _b64d(value)
        except Exception:
            raise VaultError(f"Secret {name} : base64 invalide", 400) from None
        # SealedBox = 32 octets de clé éphémère + 16 d'authentification + clair.
        if len(raw) < 49:
            raise VaultError(f"Secret {name} : sealed box trop courte", 400)
    status_text, note = verify_address(fields["coin"], fields["address"])
    row = VaultWallet(
        uuid=os.urandom(16).hex(),
        **fields,
        origin="hermes",
        address_status=status_text,
        address_note=note,
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    for name, value in sealed.items():
        setattr(row, f"sealed_{name}", value)
    db.add(row)
    db.commit()
    audit(
        db,
        "hermes",
        "vault-hermes-deposit",
        f"wallet:{row.id}",
        "ok",
        f"{row.coin} {_short(row.address)} ; champs={','.join(sorted(sealed))}",
    )
    warning = ""
    if status_text == "invalid":
        warning = f"Adresse refusée par le contrôle {row.coin} : {note}"
    return {
        "id": row.id,
        "uuid": row.uuid,
        "coin": row.coin,
        "address": row.address,
        "address_status": status_text,
        "address_warning": note,
        "warning": warning,
        "stored_fields": sorted(sealed),
        "readable_by_api": False,
    }


# ── Export chiffré ──────────────────────────────────────────────────────────
EXPORT_FORMAT = "fossor-vault-export"
EXPORT_VERSION = 1


def _collect_plaintext(db: Session, private_key: bytes) -> dict:
    entries = []
    for row in db.query(VaultWallet).order_by(VaultWallet.id.asc()).all():
        secrets = {}
        for name in SECRET_FIELDS:
            sealed = getattr(row, f"sealed_{name}")
            if sealed:
                secrets[name] = open_sealed(private_key, sealed)
        entries.append({**_wallet_dict(row), "secrets": secrets})
    return {
        "format": "fossor-vault",
        "version": EXPORT_VERSION,
        "exported_at": _now_iso(),
        "entries": entries,
    }


def export_vault(db: Session, passphrase: str, *, actor: str) -> bytes:
    """Fichier chiffré par la phrase, uniquement coffre déverrouillé."""
    require_initialized(db)
    if not is_unlocked():
        raise VaultError("Coffre verrouillé", 423)
    verify_passphrase(db, passphrase, actor=actor, action="vault-export-failed")
    session = _session()
    document = _collect_plaintext(db, session.private_key).copy()
    salt = os.urandom(16)
    key = derive_key(passphrase, salt)
    nonce, ciphertext = aead_encrypt(
        key, json.dumps(document, ensure_ascii=False).encode("utf-8"), b"fossor-vault-export"
    )
    envelope = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "kdf": {
            "algorithm": KDF_ALGORITHM,
            "time_cost": KDF_TIME_COST,
            "memory_kib": KDF_MEMORY_KIB,
            "parallelism": KDF_PARALLELISM,
            "salt": _b64e(salt),
        },
        "aead": AEAD_ALGORITHM,
        "nonce": _b64e(nonce),
        "ciphertext": _b64e(ciphertext),
    }
    audit(db, actor, "vault-export", "vault", "ok", "export chiffré produit")
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def decrypt_export(raw: bytes, passphrase: str) -> dict:
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("format") != EXPORT_FORMAT:
        raise VaultError("Fichier d'export inconnu", 400)
    kdf = doc.get("kdf") or {}
    salt = _b64d(kdf.get("salt") or "")
    key = derive_key(
        passphrase,
        salt,
        time_cost=int(kdf.get("time_cost") or KDF_TIME_COST),
        memory_kib=int(kdf.get("memory_kib") or KDF_MEMORY_KIB),
        parallelism=int(kdf.get("parallelism") or KDF_PARALLELISM),
    )
    return json.loads(
        aead_decrypt(
            key, _b64d(doc.get("nonce") or ""), _b64d(doc.get("ciphertext") or ""),
            b"fossor-vault-export",
        ).decode("utf-8")
    )


# ── Import du registre (métadonnées seulement) ──────────────────────────────
_SECRET_COLUMNS = ("mot de passe", "password", "passphrase", "seed", "phrase", "clé privée", "cle privee", "private key")


def parse_registre(text: str) -> dict:
    """Lit un REGISTRE.md et n'en garde que les métadonnées.

    Tout champ dont l'en-tête évoque un secret (mot de passe, seed, phrase,
    clé privée) est **ignoré** : l'import ne peut pas aspirer un secret.
    """
    lines = str(text or "").splitlines()
    entries: list[dict] = []
    ignored_secret_fields = 0

    def _canon(label: str) -> str:
        low = label.strip().lower()
        mapping = {
            "date": "date", "coin": "coin", "réseau": "network", "reseau": "network",
            "network": "network", "adresse": "address", "address": "address",
            "logiciel": "software", "software": "software", "machine": "machine",
            "chemin": "wallet_path", "chemin du fichier": "wallet_path",
            "wallet": "wallet_path", "path": "wallet_path", "note": "note",
            "origine": "origin", "origin": "origin",
        }
        return mapping.get(low, low.replace(" ", "_"))

    # 1) Tableaux Markdown : en-tête | … | puis lignes.
    header: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") and cell for cell in cells):
            continue
        if header is None:
            header = cells
            continue
        row = dict(zip(header, cells))
        for label in header:
            if any(word in label.lower() for word in _SECRET_COLUMNS):
                ignored_secret_fields += 1
                break
        raw_date = row.get("date") or row.get("Date") or ""
        entry = {
            "coin": (row.get("coin") or row.get("Coin") or "").strip().upper(),
            "network": (row.get("réseau") or row.get("reseau") or row.get("network") or "").strip(),
            "address": (row.get("adresse") or row.get("Adresse") or row.get("address") or "").strip(),
            "machine": (row.get("machine") or row.get("Machine") or "").strip(),
            "wallet_path": (row.get("chemin") or row.get("Chemin") or row.get("wallet") or "").strip(),
            "note": (row.get("note") or row.get("Note") or "").strip(),
            "source_date": raw_date.strip(),
            "software": (row.get("logiciel") or row.get("Logiciel") or row.get("software") or "").strip(),
        }
        if entry["coin"] and entry["address"]:
            entries.append(entry)

    # 2) Blocs « Clé: valeur » séparés par une ligne vide.
    block: dict[str, str] = {}
    for line in lines + [""]:
        stripped = line.strip()
        if not stripped:
            if block.get("coin") and block.get("address"):
                entries.append({
                    "coin": block.get("coin", "").upper(),
                    "network": block.get("network", ""),
                    "address": block.get("address", ""),
                    "machine": block.get("machine", ""),
                    "wallet_path": block.get("wallet_path", ""),
                    "note": block.get("note", ""),
                    "source_date": block.get("date", ""),
                    "software": block.get("software", ""),
                })
            block = {}
            continue
        if stripped.startswith("|"):
            continue
        for sep in (":", "—", "-"):
            if sep in stripped:
                label, _, value = stripped.partition(sep)
                key = _canon(label)
                if any(word in label.lower() for word in _SECRET_COLUMNS):
                    ignored_secret_fields += 1
                    break
                if key in {"date", "coin", "network", "address", "machine", "wallet_path", "note", "software"}:
                    block[key] = value.strip()
                break
    # Dédoublonnage sur (coin, address, wallet_path).
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for entry in entries:
        key = (entry["coin"], entry["address"], entry.get("wallet_path", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return {
        "entries": unique,
        "count": len(unique),
        "ignored_secret_fields": ignored_secret_fields,
    }


def import_from_registre(db: Session, text: str, *, actor: str, dry_run: bool = True) -> dict:
    require_initialized(db)
    if not is_unlocked():
        raise VaultError("Coffre verrouillé", 423)
    parsed = parse_registre(text)
    if dry_run:
        return {
            "dry_run": True,
            "count": parsed["count"],
            "entries": [
                {**entry, "address_short": _short(entry["address"])} for entry in parsed["entries"]
            ],
            "ignored_secret_fields": parsed["ignored_secret_fields"],
        }
    added = []
    for entry in parsed["entries"]:
        status_text, note = verify_address(entry["coin"], entry["address"])
        row = VaultWallet(
            uuid=os.urandom(16).hex(),
            coin=entry["coin"][:20],
            network=entry.get("network", "")[:40],
            address=entry["address"][:160],
            machine=entry.get("machine", "")[:80],
            wallet_path=entry.get("wallet_path", "")[:255],
            note=(entry.get("note") or entry.get("software") or "")[:500],
            origin="import",
            address_status=status_text,
            address_note=note,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        db.add(row)
        added.append(row)
    db.commit()
    audit(db, actor, "vault-import-registre", "vault", "ok",
          f"{len(added)} entrée(s) importée(s), métadonnées seules")
    return {
        "dry_run": False,
        "count": len(added),
        "ids": [row.id for row in added],
        "ignored_secret_fields": parsed["ignored_secret_fields"],
    }


# ── État ────────────────────────────────────────────────────────────────────
def status(db: Session) -> dict:
    meta = get_meta(db)
    if meta is None:
        return {
            "initialized": False,
            "locked": True,
            "unlock_remaining": 0,
            "lock_minutes": DEFAULT_LOCK_MINUTES,
            "reveal_seconds": reveal_seconds(),
            "failed_attempts": 0,
            "blocked_until": None,
            "kdf": _kdf_public(),
            "wallet_count": 0,
        }
    until = _blocked_until(meta)
    blocked_until = until.isoformat() if until and until > datetime.now(timezone.utc) else None
    return {
        "initialized": True,
        "locked": not is_unlocked(),
        "unlock_remaining": unlock_remaining(),
        "lock_minutes": int(meta.lock_minutes or DEFAULT_LOCK_MINUTES),
        "reveal_seconds": reveal_seconds(),
        "failed_attempts": int(meta.failed_attempts or 0),
        "blocked_until": blocked_until,
        "kdf": {
            "algorithm": meta.kdf_algorithm,
            "time_cost": meta.kdf_time_cost,
            "memory_kib": meta.kdf_memory_kib,
            "parallelism": meta.kdf_parallelism,
        },
        "wallet_count": int(db.query(VaultWallet).count()),
    }


def _kdf_public() -> dict:
    return {
        "algorithm": KDF_ALGORITHM,
        "time_cost": KDF_TIME_COST,
        "memory_kib": KDF_MEMORY_KIB,
        "parallelism": KDF_PARALLELISM,
    }
