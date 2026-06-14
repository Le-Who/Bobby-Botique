"""Tests for app.security — InputSanitizer and RateLimiter (pure logic, zero DB)."""

import pytest

from app.errors import InputSanitizationError
from app.security import InputSanitizer, RateLimiter

# ═══════════════════════════════════════════════════════════════════════════════
# InputSanitizer.sanitize_text
# ═══════════════════════════════════════════════════════════════════════════════


class TestSanitizeText:
    def setup_method(self):
        self.s = InputSanitizer()

    def test_clean_text_passes_through(self):
        result = self.s.sanitize_text("Hello world")
        assert "Hello" in result

    def test_strips_script_tags(self):
        result = self.s.sanitize_text("before <script>alert('xss')</script> after")
        assert "script" not in result.lower()
        assert "alert" not in result

    def test_strips_iframe_tags(self):
        result = self.s.sanitize_text("text <iframe src='evil.com'></iframe> more")
        assert "iframe" not in result.lower()

    def test_rejects_text_too_long(self):
        with pytest.raises(InputSanitizationError, match="too long"):
            self.s.sanitize_text("a" * 100_000, max_length=1000)

    def test_rejects_non_string(self):
        with pytest.raises(InputSanitizationError, match="Expected string"):
            self.s.sanitize_text(12345)

    def test_rejects_only_dangerous_content(self):
        with pytest.raises(InputSanitizationError, match="dangerous"):
            self.s.sanitize_text("<script>evil()</script>")

    def test_html_escapes_special_chars(self):
        result = self.s.sanitize_text("2 > 1 & 1 < 2")
        assert "&gt;" in result or ">" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# InputSanitizer.sanitize_filename
# ═══════════════════════════════════════════════════════════════════════════════


class TestSanitizeFilename:
    def setup_method(self):
        self.s = InputSanitizer()

    def test_clean_filename_passes(self):
        assert self.s.sanitize_filename("document.pdf") == "document.pdf"

    def test_strips_path_separators(self):
        result = self.s.sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert "\\" not in result

    def test_strips_dangerous_chars(self):
        result = self.s.sanitize_filename('file<>:"/|?*.txt')
        assert "<" not in result
        assert ">" not in result

    def test_rejects_only_dangerous_chars(self):
        with pytest.raises(InputSanitizationError, match="dangerous"):
            self.s.sanitize_filename("???***")

    def test_rejects_too_long_filename(self):
        with pytest.raises(InputSanitizationError, match="too long"):
            self.s.sanitize_filename("a" * 500)


# ═══════════════════════════════════════════════════════════════════════════════
# InputSanitizer.validate_file_extension
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateFileExtension:
    def setup_method(self):
        self.s = InputSanitizer()

    def test_allowed_image_extension(self):
        assert self.s.validate_file_extension("photo.jpg", ["image"]) is True

    def test_allowed_document_extension(self):
        assert self.s.validate_file_extension("file.pdf", ["document"]) is True

    def test_rejects_disallowed_extension(self):
        with pytest.raises(InputSanitizationError, match="not allowed"):
            self.s.validate_file_extension("malware.exe", ["image"])

    def test_empty_filename_raises(self):
        with pytest.raises(InputSanitizationError, match="empty"):
            self.s.validate_file_extension("")

    def test_no_types_allows_all_known(self):
        """When allowed_types is None, all known extensions are allowed."""
        assert self.s.validate_file_extension("doc.pdf") is True


# ═══════════════════════════════════════════════════════════════════════════════
# InputSanitizer.sanitize_url
# ═══════════════════════════════════════════════════════════════════════════════


class TestSanitizeUrl:
    def setup_method(self):
        self.s = InputSanitizer()

    def test_valid_https_url(self):
        url = "https://example.com/page"
        assert self.s.sanitize_url(url) == url

    def test_valid_http_url(self):
        url = "http://example.com"
        assert self.s.sanitize_url(url) == url

    def test_rejects_javascript_protocol(self):
        with pytest.raises(InputSanitizationError, match="[Dd]angerous protocol"):
            self.s.sanitize_url("javascript:alert(1)")

    def test_rejects_data_protocol(self):
        with pytest.raises(InputSanitizationError, match="[Dd]angerous protocol"):
            self.s.sanitize_url("data:text/html,<script>alert(1)</script>")

    def test_rejects_file_protocol(self):
        with pytest.raises(InputSanitizationError, match="[Dd]angerous protocol"):
            self.s.sanitize_url("file:///etc/passwd")

    def test_rejects_localhost(self):
        with pytest.raises(InputSanitizationError, match="[Ll]ocalhost"):
            self.s.sanitize_url("http://localhost/admin")

    def test_rejects_ip_address(self):
        with pytest.raises(InputSanitizationError, match="IP"):
            self.s.sanitize_url("http://192.168.1.1/admin")

    def test_rejects_dns_ssrf_bypass(self):
        import socket
        from unittest.mock import patch

        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            # Mock DNS resolution to return a loopback IP
            mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
            with pytest.raises(InputSanitizationError, match="[Rr]esolved IP address is not allowed"):
                self.s.sanitize_url("http://malicious.example.com/admin")

    def test_rejects_too_long_url(self):
        with pytest.raises(InputSanitizationError, match="too long"):
            self.s.sanitize_url("https://example.com/" + "a" * 10000)


# ═══════════════════════════════════════════════════════════════════════════════
# InputSanitizer.sanitize_query
# ═══════════════════════════════════════════════════════════════════════════════


class TestSanitizeQuery:
    def setup_method(self):
        self.s = InputSanitizer()

    def test_clean_query(self):
        result = self.s.sanitize_query("python asyncio tutorial")
        assert "python" in result

    def test_strips_control_chars(self):
        result = self.s.sanitize_query("hello\x00\x01\x02world")
        assert "\x00" not in result

    def test_rejects_too_long_query(self):
        with pytest.raises(InputSanitizationError, match="too long"):
            self.s.sanitize_query("a" * 10000)

    def test_rejects_only_dangerous_content(self):
        with pytest.raises(InputSanitizationError, match="dangerous"):
            self.s.sanitize_query("<script>evil()</script>")


# ═══════════════════════════════════════════════════════════════════════════════
# RateLimiter
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_requests_within_limit(self):
        rl = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            assert await rl.check_rate_limit(user_id=1) is True

    @pytest.mark.asyncio
    async def test_blocks_after_limit_exceeded(self):
        rl = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            await rl.check_rate_limit(user_id=1)
        assert await rl.check_rate_limit(user_id=1) is False

    @pytest.mark.asyncio
    async def test_different_users_independent(self):
        rl = RateLimiter(max_requests=2, window_seconds=60)
        await rl.check_rate_limit(user_id=1)
        await rl.check_rate_limit(user_id=1)
        # User 1 is at limit, but user 2 should still be allowed
        assert await rl.check_rate_limit(user_id=2) is True

    @pytest.mark.asyncio
    async def test_get_user_stats_structure(self):
        rl = RateLimiter(max_requests=10, window_seconds=60)
        await rl.check_rate_limit(user_id=1)
        await rl.check_rate_limit(user_id=1)
        stats = await rl.get_user_stats(user_id=1)
        assert stats["requests_count"] == 2
        assert stats["max_requests"] == 10
        assert stats["remaining"] == 8
        assert "reset_at" in stats

    @pytest.mark.asyncio
    async def test_stats_for_unknown_user(self):
        rl = RateLimiter(max_requests=10, window_seconds=60)
        stats = await rl.get_user_stats(user_id=99999)
        assert stats["requests_count"] == 0
        assert stats["remaining"] == 10
