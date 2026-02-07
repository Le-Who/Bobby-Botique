## 2025-05-22 - [Security Enhancement] Robust URL Sanitization
**Vulnerability:** The `sanitize_url` function relied on a regex that only matched IPv4 addresses in exact dot-decimal notation, failing to block `localhost`, IPv6 loopbacks, and URLs with ports (e.g., `http://127.0.0.1:8080`).
**Learning:** Regex-based validation for URLs is brittle. `urllib.parse` splits the URL, but `netloc` includes the port, which breaks regexes expecting only an IP. `ipaddress` module is robust for IP validation.
**Prevention:** Always parse the URL to extract the hostname (stripping port and brackets) before validating. Use dedicated libraries (`ipaddress`) instead of regex for IP checks. Explicitly block `localhost`.

## 2025-05-23 - [HTML Injection] Telegram Formatter Injection
**Vulnerability:** The `_markdown_to_html` function applied regex replacements to convert markdown to HTML without first escaping the input text. This allowed attackers to inject arbitrary HTML tags by bypassing markdown syntax.
**Learning:** When converting one format to another (Markdown to HTML), always sanitize/escape the input for the target format *before* applying transformation rules.
**Prevention:** Use `html.escape(text)` at the start of any text-to-HTML conversion pipeline to neutralize special characters (`<`, `>`, `&`, `"`) before adding structural tags.

## 2025-02-18 - [MarkdownV2 Injection / DoS]
**Vulnerability:** The `TelegramFormatter` failed to properly escape special characters like `*`, `_`, and `\` in MarkdownV2 mode, causing Telegram API `BadRequest` errors when the bot output contained these characters in an "unsafe" context (e.g., orphan asterisk). This could be exploited for DoS (bot failing to reply) or potentially formatting injection.
**Learning:** Manual iterative replacement for escaping is error-prone. The `_is_safe` validation logic was inconsistent with the escaping logic, leading to false positives (thinking text was safe when it wasn't) or unnecessary fallbacks to HTML. Relying on a robust single-pass regex replacement is safer.
**Prevention:** Use comprehensive regex-based escaping that covers ALL special characters defined by the spec. Ensure validation logic aligns with escaping logic (or trust the escaping logic if robust). Verify escaping of the escape character itself (`\`).

## 2025-05-24 - [File Upload Security] Unvalidated File Signature
**Vulnerability:** The `DocumentProcessor._process_word` method accepted any file with a `.docx` extension without verifying its content type, potentially allowing processing of renamed malicious files or invalid binaries.
**Learning:** Relying solely on file extensions is insufficient for security. Libraries like `python-docx` might crash or behave unexpectedly with invalid input.
**Prevention:** Validate file signatures (magic bytes) at the beginning of the processing pipeline. For DOCX (ZIP), check for `b'\x50\x4b\x03\x04'`.

## 2025-05-25 - [Authentication] Insecure Query Parameter Auth (DEFERRED)
**Vulnerability:** The `require_auth` decorator accepts tokens via query parameters (`?token=...`), exposing credentials in logs.
**Constraint:** Removing this fallback breaks existing monitoring integrations that rely on URL-based auth.
**Prevention:** In new projects, strictly enforce header-based auth. In legacy projects, plan a deprecation window before removal.

## 2025-05-25 - [Defense in Depth] Missing Security Headers
**Vulnerability:** The Flask application lacked standard security headers (CSP, X-Frame-Options), increasing risk of clickjacking and XSS.
**Learning:** CSP implementation requires careful auditing of external resources (e.g., Google Fonts) and inline styles to avoid breaking the UI.
**Prevention:** Use `after_request` to inject `Content-Security-Policy`, `X-Content-Type-Options`, and `X-Frame-Options`.
