## 2025-05-22 - [Security Enhancement] Robust URL Sanitization
**Vulnerability:** The `sanitize_url` function relied on a regex that only matched IPv4 addresses in exact dot-decimal notation, failing to block `localhost`, IPv6 loopbacks, and URLs with ports (e.g., `http://127.0.0.1:8080`).
**Learning:** Regex-based validation for URLs is brittle. `urllib.parse` splits the URL, but `netloc` includes the port, which breaks regexes expecting only an IP. `ipaddress` module is robust for IP validation.
**Prevention:** Always parse the URL to extract the hostname (stripping port and brackets) before validating. Use dedicated libraries (`ipaddress`) instead of regex for IP checks. Explicitly block `localhost`.

## 2025-05-22 - [HTML Injection in Telegram Formatting]
**Vulnerability:** The `TelegramFormatter._markdown_to_html` method replaced Markdown patterns with HTML tags (e.g., `*text*` to `<b>text</b>`) but failed to escape the original input text. This allowed users to inject arbitrary HTML tags (e.g., `<b>bold</b>` or malformed tags) which could break formatting or exploit Telegram's HTML parser.
**Learning:** When converting one format to another (Markdown -> HTML), always escape the destination format's special characters in the source text *before* applying the conversion rules.
**Prevention:** Added `html.escape(text)` at the beginning of the conversion function to ensure all user input is treated as literal text unless transformed by the formatter's own regex rules.
