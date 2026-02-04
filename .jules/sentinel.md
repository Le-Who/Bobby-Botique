## 2025-05-22 - [Security Enhancement] Robust URL Sanitization
**Vulnerability:** The `sanitize_url` function relied on a regex that only matched IPv4 addresses in exact dot-decimal notation, failing to block `localhost`, IPv6 loopbacks, and URLs with ports (e.g., `http://127.0.0.1:8080`).
**Learning:** Regex-based validation for URLs is brittle. `urllib.parse` splits the URL, but `netloc` includes the port, which breaks regexes expecting only an IP. `ipaddress` module is robust for IP validation.
**Prevention:** Always parse the URL to extract the hostname (stripping port and brackets) before validating. Use dedicated libraries (`ipaddress`) instead of regex for IP checks. Explicitly block `localhost`.

## 2025-05-23 - [HTML Injection] Telegram Formatter Injection
**Vulnerability:** The `_markdown_to_html` function applied regex replacements to convert markdown to HTML without first escaping the input text. This allowed attackers to inject arbitrary HTML tags by bypassing markdown syntax.
**Learning:** When converting one format to another (Markdown to HTML), always sanitize/escape the input for the target format *before* applying transformation rules.
**Prevention:** Use `html.escape(text)` at the start of any text-to-HTML conversion pipeline to neutralize special characters (`<`, `>`, `&`, `"`) before adding structural tags.
