"""
Symmetric encryption for API keys stored in the database.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from
ADMIN_SECRET via PBKDF2. This ensures API keys are encrypted at rest
and can only be decrypted by a server that knows the ADMIN_SECRET.

The key derivation uses a fixed salt (derived from the app name) so
the same ADMIN_SECRET always produces the same Fernet key — no need
to store the salt separately.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# Fixed salt for deterministic key derivation — changing this
# invalidates all previously encrypted keys.
_SALT = b"gemaibotv2-key-encryption-salt-v1"

_fernet_instance: Fernet | None = None


def _get_fernet() -> Fernet:
    """Lazily initialize the Fernet instance from ADMIN_SECRET."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    from app.config import settings

    secret = getattr(settings, "ADMIN_SECRET", None)
    if not secret:
        raise RuntimeError("ADMIN_SECRET must be set for API key encryption")

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=480_000,  # OWASP 2023 recommendation
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))
    _fernet_instance = Fernet(key)
    return _fernet_instance


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key for database storage.

    Returns a URL-safe base64 string that includes a timestamp,
    so decryption can optionally enforce TTL.
    """
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt an API key retrieved from the database.

    Raises ValueError if the ciphertext is invalid or the key
    has been tampered with.
    """
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError("Failed to decrypt API key — ADMIN_SECRET may have changed")


def is_encrypted(value: str) -> bool:
    """Heuristic check: Fernet tokens start with 'gAAAAA' and are base64url."""
    return len(value) > 50 and value.startswith("gAAAAA")


def safe_decrypt(value: str) -> str:
    """Decrypt if encrypted, otherwise return as-is.

    This handles the migration case where old plaintext keys
    coexist with new encrypted keys in the database.
    """
    if is_encrypted(value):
        try:
            return decrypt_api_key(value)
        except (ValueError, Exception) as e:
            logging.warning("Failed to decrypt key, using as-is: %s", e)
            return value
    return value


def reset_fernet():
    """Reset the cached Fernet instance (for testing)."""
    global _fernet_instance
    _fernet_instance = None
