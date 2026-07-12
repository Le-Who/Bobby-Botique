import socket

import pytest

from app.errors import InputSanitizationError
from app.security import input_sanitizer


def test_sanitize_url_ssrf(monkeypatch):
    original_getaddrinfo = socket.getaddrinfo

    def mock_getaddrinfo(host, port, *args, **kwargs):
        if host == "local.nip.io":
            return [(2, 1, 6, "", ("127.0.0.1", 0))]
        if host == "private.nip.io":
            return [(2, 1, 6, "", ("192.168.1.1", 0))]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

    with pytest.raises(InputSanitizationError):
        input_sanitizer.sanitize_url("http://local.nip.io")

    with pytest.raises(InputSanitizationError):
        input_sanitizer.sanitize_url("http://private.nip.io")

    # Should allow public domain
    assert input_sanitizer.sanitize_url("http://example.com") == "http://example.com"
