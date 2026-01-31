## 2025-05-22 - [Security Enhancement] Robust URL Sanitization
**Vulnerability:** The `sanitize_url` function relied on a regex that only matched IPv4 addresses in exact dot-decimal notation, failing to block `localhost`, IPv6 loopbacks, and URLs with ports (e.g., `http://127.0.0.1:8080`).
**Learning:** Regex-based validation for URLs is brittle. `urllib.parse` splits the URL, but `netloc` includes the port, which breaks regexes expecting only an IP. `ipaddress` module is robust for IP validation.
**Prevention:** Always parse the URL to extract the hostname (stripping port and brackets) before validating. Use dedicated libraries (`ipaddress`) instead of regex for IP checks. Explicitly block `localhost`.
