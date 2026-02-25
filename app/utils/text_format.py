import re
import html
from typing import List, Tuple

# Constants
MAX_MESSAGE_LENGTH = 4096


def format_text(text: str, parse_mode: str = "HTML") -> Tuple[str, str]:
    """
    Formats text for Telegram safely.

    Args:
        text (str): The input text (Markdown/CommonMark style from LLM).
        parse_mode (str): The desired parse mode. Defaults to 'HTML'.

    Returns:
        Tuple[str, str]: (formatted_text, parse_mode)
    """
    if not text:
        return "", parse_mode

    if parse_mode == "HTML":
        return markdown_to_html(text), "HTML"

    # Fallback/Pass-through for other modes or plain text handling if needed
    return text, parse_mode


def markdown_to_html(text: str) -> str:
    """
    Converts Markdown text to Telegram-supported HTML.
    Safely handles code blocks, inline code, bold, italic, and links.
    """
    if not text:
        return ""

    # 1. Split text by code blocks to avoid formatting inside them
    # Pattern to capture ```...``` blocks including language specifier
    # We use a capture group () to include the delimiters in the split result
    # Non-greedy match for content
    segments = re.split(r"(```(?:.|\n)*?```)", text)

    html_parts = []

    for segment in segments:
        if segment.startswith("```") and segment.endswith("```") and len(segment) >= 6:
            # --- Code Block Processing ---
            # Remove the backticks
            inner = segment[3:-3]

            # Detect language
            language = ""
            content = inner

            # Try to split first line
            if "\n" in inner:
                first_line_end = inner.find("\n")
                possible_lang = inner[:first_line_end].strip()

                # Heuristic: Valid language is usually short, alphanumeric
                if (
                    possible_lang
                    and len(possible_lang) < 20
                    and all(c.isalnum() or c in "+-#." for c in possible_lang)
                ):
                    language = possible_lang
                    content = inner[first_line_end + 1 :]  # Skip newline
                else:
                    # Treat identifying line as content if it doesn't look like a language
                    pass

            # Escape the content for HTML
            escaped_content = html.escape(content.strip())

            if not escaped_content:
                # Empty block
                continue

            if language:
                html_parts.append(
                    f'<pre><code class="language-{language}">{escaped_content}</code></pre>'
                )
            else:
                html_parts.append(f"<pre>{escaped_content}</pre>")

        else:
            # --- Regular Text Processing ---

            # 0. Clean up MarkdownV2 style escaping (if any slipped through)
            # Remove backslashes before non-special characters or punctuation that doesn't need it in HTML
            # e.g. \. -> .   \( -> (   \) -> )   \- -> -   \= -> =
            # We be careful not to break \\ (literal backslash) if it was intended, but usually it's better to clean.
            segment = re.sub(r"\\([.\-()!=[\]{}|#+])", r"\1", segment)

            # 1. Escape HTML characters (important to do first!)
            # This turns < into &lt;, etc.
            escaped_text = html.escape(segment)

            # 2. Process Inline formatting
            # Order matters slightly, but usually regexes are distinct enough.

            # Inline Code: `code`
            # Pattern: `...` (non-greedy)
            escaped_text = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped_text)

            # Bold: **text**
            escaped_text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped_text)

            # Italic: __text__ (Standard Markdown allows this)
            escaped_text = re.sub(r"__(.+?)__", r"<i>\1</i>", escaped_text)

            # Italic: *text* (Only if not part of **)
            # This regex uses lookarounds to ensure we don't match inside **
            escaped_text = re.sub(
                r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", escaped_text
            )

            # Italic: _text_ (Standard Markdown)
            # Match _text_ but not __text__ or snake_case_text
            # Use negative lookbehind and lookahead to avoid matching inside __ or words
            # This is tricky because snake_case is common.
            # We strictly enforce white space or boundary checks, or just accept that snake_case might break if we are aggressive.
            # Safe heuristic: _text_ where _ is preceded/followed by non-word or space/start/end.
            # But standard Markdown is: _text_ works anywhere if surrounded by whitespace or punctuation.
            # Minimal safe version:
            escaped_text = re.sub(
                r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)", r"<i>\1</i>", escaped_text
            )

            # Links: [text](url)
            # Since we already escaped HTML, the url might contain &amp; etc.
            # We match strict []() pattern.
            link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"
            escaped_text = re.sub(link_pattern, r'<a href="\2">\1</a>', escaped_text)

            html_parts.append(escaped_text)

    return "".join(html_parts)


def split_text_safe(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """
    Splits text into chunks safe for Telegram, respecting HTML structure.
    Tries to split by newlines, then spaces, avoiding splitting inside tags.
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        # Initial cut
        limit = max_length
        cut_point = -1

        # 1. Try to split by block close tags </pre>, </code>, </b>, </i>, </a>
        # This is safest to keep tags balanced.
        # We look for the last occurrence of a closing tag within the limit.

        # Search for closing tag boundaries
        # This is a simplified heuristic. For perfect splitting, we need a tokenizer.
        # For now, we favor splitting at newlines, then specific tag boundaries.

        candidates = [
            text.rfind("</pre>", 0, limit),
            text.rfind("\n\n", 0, limit),
            text.rfind("\n", 0, limit),
            text.rfind(". ", 0, limit),
        ]

        # Filter valid indices
        valid_candidates = [c for c in candidates if c != -1]

        if valid_candidates:
            # Pick the largest index (closest to limit)
            cut_point = max(valid_candidates)

            # If valid candidate is a tag, we might need to adjust cut_point to include the tag
            if cut_point == candidates[0]:  # </pre>
                cut_point += 6  # len('</pre>')
            elif cut_point == candidates[1]:  # \n\n
                cut_point += 2  # Include both newlines
            elif cut_point == candidates[2]:  # \n
                cut_point += 1  # Include the newline

        else:
            # Force split at space
            cut_point = text.rfind(" ", 0, limit)

        if cut_point == -1 or cut_point == 0:
            # Last resort: hard cut
            cut_point = limit

        # Check if cut_point is inside an HTML tag.
        # This occurs if the last '<' before the cut_point does not have a matching '>' before the cut_point.
        last_open_angle = text.rfind("<", 0, cut_point)
        if last_open_angle != -1:
            last_close_angle_after_open = text.find(">", last_open_angle, cut_point)
            if last_close_angle_after_open == -1:
                # We are between '<' and '>'. We should cut BEFORE the '<' so we don't break the tag string itself.
                # However, if last_open_angle is 0, setting cut_point to 0 will cause an infinite loop.
                # In that case, the tag itself is longer than max_length, so we are forced to cut inside it.
                if last_open_angle > 0:
                    cut_point = last_open_angle

        chunk = text[:cut_point]
        remaining = text[
            cut_point:
        ].lstrip()  # Remove leading whitespace from next chunk

        # Check tag balance
        # If open tags > close tags, we need to close them in this chunk and open in next
        # Implementation of tag balancing is complex.
        # Optimized: store full match in stack to avoid re-searching with regex
        open_tags = []
        # Find all tags in chunk
        tag_iter = re.finditer(r"<(/?)(\w+)[^>]*>", chunk)
        for match in tag_iter:
            is_close = match.group(1) == "/"
            tag_name = match.group(2)
            full_match = match.group(0)

            if tag_name in ["br", "img", "hr"]:
                continue  # Void tags
            if not is_close:
                open_tags.append((tag_name, full_match))
            else:
                if open_tags and open_tags[-1][0] == tag_name:
                    open_tags.pop()

        # If open_tags is not empty, append closing tags to chunk
        # and prepend opening tags to remaining
        closing_str = ""
        opening_str = ""
        for tag_name, full_match in reversed(open_tags):
            closing_str += f"</{tag_name}>"
            opening_str = full_match + opening_str
        chunk += closing_str
        if remaining:
            remaining = opening_str + remaining

        # Infinite loop prevention: Force a hard cut if no text content was processed
        if remaining == text:
            cut_point = limit
            chunk = text[:cut_point]
            remaining = text[cut_point:].lstrip()
            chunks.append(chunk)
            text = remaining
            continue

        chunks.append(chunk)
        text = remaining

    return chunks


def strip_formatting(text: str) -> str:
    """Removes all HTML tags and invisible characters."""
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode entities
    text = html.unescape(text)
    return text.strip()
