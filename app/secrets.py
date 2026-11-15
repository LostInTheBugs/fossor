from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
import os

_fernet: Fernet | None = None


def _derive_key(master_key: str) -> bytes:
    """Derive a Fernet key from the master key string using PBKDF2."""
    salt = b"fossor_salt_v1"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_key.encode()))


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        master = os.environ.get("FOSSOR_MASTER_KEY")
        if not master:
            raise RuntimeError("FOSSOR_MASTER_KEY environment variable is required")
        key = _derive_key(master)
        _fernet = Fernet(key)
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("Cannot decrypt secret: wrong key or corrupted data")
