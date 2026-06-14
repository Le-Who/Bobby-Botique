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

## 2025-05-24 - [Information Disclosure] Health Endpoint Leakage
**Vulnerability:** The `/health` endpoint exposed internal system identifiers (`container_id`, `process_id`) without authentication, potentially aiding fingerprinting or targeting.
**Learning:** Publicly accessible monitoring endpoints often inadvertently expose sensitive internal state. Even "harmless" IDs can be used in chained attacks.
**Prevention:** Sanitize health check responses to include only necessary status information (e.g., "healthy", "unhealthy") and remove any identifiers or stack traces. Use authentication for detailed metrics.

## 2025-05-24 - [Server-Side Request Forgery] SSRF through DNS Rebinding in URL Sanitization
**Vulnerability:** The `sanitize_url` function relied solely on string matching (e.g., "localhost") and literal IP parsing to block local IPs. Attackers could bypass this using custom DNS domains pointing to local IPs (e.g., `127.0.0.1.nip.io`).
**Learning:** URL sanitization must perform DNS resolution on the hostname to check the actual returned IP addresses against private and loopback ranges. String matching on the hostname is not enough.
**Prevention:** Always use `socket.getaddrinfo` to resolve the hostname and validate the resolved IP addresses against private, loopback, and link-local ranges to prevent DNS rebinding attacks.
