## 2025-05-22 - [Security Enhancement] Robust URL Sanitization
**Vulnerability:** The `sanitize_url` function relied on a regex that only matched IPv4 addresses in exact dot-decimal notation, failing to block `localhost`, IPv6 loopbacks, and URLs with ports (e.g., `http://127.0.0.1:8080`).
**Learning:** Regex-based validation for URLs is brittle. `urllib.parse` splits the URL, but `netloc` includes the port, which breaks regexes expecting only an IP. `ipaddress` module is robust for IP validation.
**Prevention:** Always parse the URL to extract the hostname (stripping port and brackets) before validating. Use dedicated libraries (`ipaddress`) instead of regex for IP checks. Explicitly block `localhost`.

## 2025-05-22 - [Security Fix] HTML Injection in Telegram Formatting
**Vulnerability:** The `TelegramFormatter._markdown_to_html` method performed Regex replacements to convert Markdown to HTML without first escaping the input text. This allowed attackers (or malicious LLM output) to inject arbitrary HTML tags (like `<script>`, although Telegram filters most) or mess up formatting with tags like `<b>`.
**Learning:** When converting custom markup to HTML, **always** escape the raw input text using `html.escape()` *before* applying any regex replacements that introduce HTML tags. This ensures that user-provided `<` and `>` are treated as literal characters, while the parser-generated tags remain valid.
**Prevention:** Add `html.escape(text)` at the start of any text-to-HTML conversion function.
