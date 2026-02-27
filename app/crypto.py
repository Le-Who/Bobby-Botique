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
    """Check whether a value is a Fernet token.

    Fernet tokens are base64url-encoded and always start with 'gAAAAA'
    (version byte 0x80 + 8-byte timestamp starting with zeros).
    We also verify the value is valid base64url to avoid false positives
    on strings that coincidentally match the prefix.
    """
    if len(value) <= 50 or not value.startswith("gAAAAA"):
        return False
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+=*", value))


def safe_decrypt(value: str) -> str:
    """Decrypt if encrypted, otherwise return as-is.

    This handles the migration case where old plaintext keys
    coexist with new encrypted keys in the database.

    Raises DecryptionError if the value is encrypted but cannot be decrypted
    (e.g. ADMIN_SECRET has changed).
    """
    if is_encrypted(value):
        try:
            decrypted = decrypt_api_key(value)
            logging.debug(
                "Key decrypted OK: encrypted_prefix=%s... -> decrypted_prefix=%s****, len=%d",
                value[:12], decrypted[:6], len(decrypted),
            )
            return decrypted
        except (ValueError, Exception) as e:
            from app.errors import DecryptionError

            logging.error(
                "CRITICAL: Failed to decrypt key (prefix=%s..., len=%d): %s",
                value[:12], len(value), e,
            )
            raise DecryptionError(
                f"Cannot decrypt API key (prefix={value[:12]}...): {e}"
            ) from e
    logging.debug("Key not encrypted (prefix=%s****, len=%d), using as-is", value[:6], len(value))
    return value


def reset_fernet():
    """Reset the cached Fernet instance (for testing)."""
    global _fernet_instance
    _fernet_instance = None
