"""Tests for app.crypto – Fernet-based API key encryption."""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _MockSettings:
    ADMIN_SECRET: str = "test-admin-secret-for-unit-tests"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_fernet():
    """Ensure each test starts with a fresh Fernet instance."""
    from app.crypto import reset_fernet
    reset_fernet()
    yield
    reset_fernet()


@pytest.fixture()
def _mock_settings():
    """Patch settings to provide ADMIN_SECRET."""
    mock = _MockSettings()
    with patch("app.crypto.settings", mock, create=True), \
         patch("app.config.settings", mock):
        yield mock


# ---------------------------------------------------------------------------
# Encrypt / Decrypt roundtrip
# ---------------------------------------------------------------------------

class TestEncryptDecryptRoundtrip:

    def test_roundtrip_simple_key(self, _mock_settings):
        from app.crypto import decrypt_api_key, encrypt_api_key

        plaintext = "AIzaSyB_test_key_1234567890"
        ciphertext = encrypt_api_key(plaintext)

        assert ciphertext != plaintext
        assert len(ciphertext) > 50  # Fernet tokens are long
        assert decrypt_api_key(ciphertext) == plaintext

    def test_roundtrip_empty_string(self, _mock_settings):
        from app.crypto import decrypt_api_key, encrypt_api_key

        ciphertext = encrypt_api_key("")
        assert decrypt_api_key(ciphertext) == ""

    def test_roundtrip_unicode(self, _mock_settings):
        from app.crypto import decrypt_api_key, encrypt_api_key

        plaintext = "key-with-ünïcödé-чары"
        assert decrypt_api_key(encrypt_api_key(plaintext)) == plaintext

    def test_different_plaintexts_produce_different_ciphertexts(self, _mock_settings):
        from app.crypto import encrypt_api_key

        c1 = encrypt_api_key("key-aaa")
        c2 = encrypt_api_key("key-bbb")
        assert c1 != c2

    def test_same_plaintext_produces_different_ciphertexts(self, _mock_settings):
        """Fernet includes a timestamp, so same plaintext → different ciphertext."""
        from app.crypto import encrypt_api_key

        c1 = encrypt_api_key("same-key")
        c2 = encrypt_api_key("same-key")
        assert c1 != c2  # Non-deterministic due to timestamp


# ---------------------------------------------------------------------------
# is_encrypted heuristic
# ---------------------------------------------------------------------------

class TestIsEncrypted:

    def test_fernet_token_detected(self, _mock_settings):
        from app.crypto import encrypt_api_key, is_encrypted

        ciphertext = encrypt_api_key("test-key")
        assert is_encrypted(ciphertext) is True

    def test_plaintext_not_detected(self):
        from app.crypto import is_encrypted

        assert is_encrypted("AIzaSyB_some_key") is False
        assert is_encrypted("tvly-abc123") is False
        assert is_encrypted("") is False
        assert is_encrypted("short") is False


# ---------------------------------------------------------------------------
# safe_decrypt
# ---------------------------------------------------------------------------

class TestSafeDecrypt:

    def test_decrypts_encrypted_value(self, _mock_settings):
        from app.crypto import encrypt_api_key, safe_decrypt

        ciphertext = encrypt_api_key("real-api-key")
        assert safe_decrypt(ciphertext) == "real-api-key"

    def test_returns_plaintext_as_is(self):
        from app.crypto import safe_decrypt

        assert safe_decrypt("AIzaSyB_plain_key") == "AIzaSyB_plain_key"
        assert safe_decrypt("tvly-abc") == "tvly-abc"
        assert safe_decrypt("") == ""

    def test_raises_on_corrupted_ciphertext(self, _mock_settings):
        """If decryption fails (e.g. wrong key), safe_decrypt raises DecryptionError."""
        # Encrypt with one secret
        from app.crypto import encrypt_api_key, reset_fernet, safe_decrypt
        from app.errors import DecryptionError
        ciphertext = encrypt_api_key("secret-key")

        # Reset and use different secret
        reset_fernet()
        different_mock = _MockSettings(ADMIN_SECRET="completely-different-secret")
        with patch("app.config.settings", different_mock), pytest.raises(DecryptionError):
            safe_decrypt(ciphertext)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:

    def test_missing_admin_secret_raises(self):
        from app.crypto import encrypt_api_key

        mock = _MockSettings(ADMIN_SECRET="")
        with patch("app.config.settings", mock), pytest.raises(RuntimeError, match="ADMIN_SECRET must be set"):
            encrypt_api_key("test")

    def test_decrypt_wrong_secret_raises_value_error(self, _mock_settings):
        from app.crypto import decrypt_api_key, encrypt_api_key, reset_fernet

        ciphertext = encrypt_api_key("secret-key")

        # Switch to different secret
        reset_fernet()
        different_mock = _MockSettings(ADMIN_SECRET="wrong-secret")
        with patch("app.config.settings", different_mock), pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_api_key(ciphertext)

    def test_decrypt_garbage_raises_value_error(self, _mock_settings):
        from app.crypto import decrypt_api_key

        with pytest.raises(ValueError):
            decrypt_api_key("not-a-valid-fernet-token")


# ---------------------------------------------------------------------------
# reset_fernet
# ---------------------------------------------------------------------------

class TestResetFernet:

    def test_reset_clears_cached_instance(self, _mock_settings):
        from app import crypto

        # Trigger lazy init
        crypto.encrypt_api_key("test")
        assert crypto._fernet_instance is not None

        crypto.reset_fernet()
        assert crypto._fernet_instance is None
