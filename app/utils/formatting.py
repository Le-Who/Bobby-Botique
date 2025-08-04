import re
import logging
from typing import Optional

def strip_markdown(text: str) -> str:
    """Removes all Markdown formatting from the text."""
    if not text:
        return ""
    
    # Remove links but keep the text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Remove bold, italic, code formatting
    text = re.sub(r'[*_~`]', '', text)
    # Remove backslashes
    text = text.replace('\\', '')
    return text.strip()

def format_key_for_display(api_key: str) -> str:
    """Formats an API key for safe display."""
    if not isinstance(api_key, str) or len(api_key) < 10:
        return "Invalid Key"
    return f"{api_key[:5]}...{api_key[-4:]}"

def safe_markdown_v2(text: str) -> str:
    """
    Creates a safe MarkdownV2 text by escaping special characters while preserving valid links.
    This function intelligently escapes text while keeping MarkdownV2 links intact.
    """
    if not text:
        return ""
    
    # Characters that need escaping in Telegram MarkdownV2
    special_chars = ['_', '*', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    
    # First, let's protect valid MarkdownV2 links
    import re
    
    # Pattern to match valid MarkdownV2 links: [text](url)
    link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    
    def protect_link(match):
        link_text = match.group(1)
        link_url = match.group(2)
        
        # Escape the link text (but not the brackets and parentheses)
        escaped_text = link_text
        for char in special_chars:
            escaped_text = escaped_text.replace(char, f'\\{char}')
        
        # Return the protected link
        return f'[{escaped_text}]({link_url})'
    
    # Replace all valid links with protected versions
    result = re.sub(link_pattern, protect_link, text)
    
    # Now escape the remaining special characters (but not in link positions)
    # We need to be careful not to escape brackets and parentheses that are part of links
    
    # Escape special characters outside of link patterns
    for char in special_chars:
        # Only escape if not already escaped and not part of a link
        result = re.sub(f'(?<!\\\\){re.escape(char)}', f'\\{char}', result)
    
    return result

def create_simple_formatted_text(text: str, bold_parts: Optional[list] = None, italic_parts: Optional[list] = None) -> str:
    """
    Creates a simple formatted text with basic bold and italic formatting.
    This is much more reliable than complex MarkdownV2 formatting.
    
    Args:
        text: The base text
        bold_parts: List of strings to make bold
        italic_parts: List of strings to make italic
    
    Returns:
        Formatted text that's safe for Telegram
    """
    if not text:
        return ""
    
    result = text
    
    # Apply bold formatting
    if bold_parts:
        for part in bold_parts:
            if part in result:
                # Escape special characters in the part
                escaped_part = ""
                for char in part:
                    if char in ['_', '*', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
                        escaped_part += f'\\{char}'
                    else:
                        escaped_part += char
                result = result.replace(part, f"*{escaped_part}*")
    
    # Apply italic formatting
    if italic_parts:
        for part in italic_parts:
            if part in result:
                # Escape special characters in the part
                escaped_part = ""
                for char in part:
                    if char in ['_', '*', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
                        escaped_part += f'\\{char}'
                    else:
                        escaped_part += char
                result = result.replace(part, f"_{escaped_part}_")
    
    return result

def format_list(items: list, prefix: str = "•") -> str:
    """
    Creates a simple formatted list without complex Markdown.
    
    Args:
        items: List of items to format
        prefix: Prefix for each item (default: bullet point)
    
    Returns:
        Formatted list as plain text
    """
    if not items:
        return ""
    
    formatted_items = []
    for item in items:
        # Clean the item and add prefix
        clean_item = strip_markdown(str(item)).strip()
        if clean_item:
            formatted_items.append(f"{prefix} {clean_item}")
    
    return "\n".join(formatted_items)

def format_source_link(display_text: str, url: str) -> str:
    """
    Creates a safe MarkdownV2 link for Telegram.
    
    Args:
        display_text: Text to display
        url: The URL
    
    Returns:
        Formatted MarkdownV2 link
    """
    if not display_text or not url:
        return ""
    
    # Clean the display text
    clean_text = strip_markdown(display_text).strip()
    if not clean_text:
        clean_text = "Источник"
    
    # Escape special characters in the display text
    special_chars = ['_', '*', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    escaped_text = clean_text
    for char in special_chars:
        escaped_text = escaped_text.replace(char, f'\\{char}')
    
    # Create MarkdownV2 link
    return f"[{escaped_text}]({url})"

def create_safe_link(display_text: str, url: str) -> str:
    """
    Creates a safe MarkdownV2 link with automatic fallback to plain text if needed.
    
    Args:
        display_text: Text to display
        url: The URL
    
    Returns:
        Formatted link that's safe for Telegram
    """
    try:
        link = format_source_link(display_text, url)
        # Test if the link is safe
        if is_safe_for_markdown_v2(link):
            return link
        else:
            # Fallback to plain text
            return f"{display_text}: {url}"
    except Exception:
        # Fallback to plain text
        return f"{display_text}: {url}"

def is_safe_for_markdown_v2(text: str) -> bool:
    """
    Checks if text is safe for MarkdownV2 formatting.
    
    Args:
        text: Text to check
    
    Returns:
        True if text is safe, False otherwise
    """
    if not text:
        return True
    
    # Check for common problematic patterns
    problematic_patterns = [
        r'\[\[.*?\]\]',  # Double brackets
        r'\{.*?\}',      # Curly braces
        r'`.*?`.*?`',    # Multiple backticks
        r'\*.*?\*.*?\*', # Multiple asterisks
        r'_.*?_.*?_',    # Multiple underscores
    ]
    
    for pattern in problematic_patterns:
        if re.search(pattern, text):
            return False
    
    # Check for valid link patterns
    # Valid links should be in format [text](url)
    link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    links = re.findall(link_pattern, text)
    
    for link_text, link_url in links:
        # Check if link text contains unescaped special characters
        special_chars_in_text = ['*', '_', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars_in_text:
            if char in link_text and f'\\{char}' not in link_text:
                # If special character is not escaped, it's not safe
                return False
    
    return True

def sanitize_for_telegram(text: str, prefer_plain: bool = False) -> tuple[str, bool]:
    """
    Sanitizes text for Telegram, choosing between MarkdownV2 and plain text.
    
    Args:
        text: Text to sanitize
        prefer_plain: If True, prefer plain text over MarkdownV2
    
    Returns:
        Tuple of (sanitized_text, is_markdown)
    """
    if not text:
        return "", False
    
    # If we prefer plain text or text is not safe for MarkdownV2
    if prefer_plain or not is_safe_for_markdown_v2(text):
        return strip_markdown(text), False
    
    # Try to create safe MarkdownV2
    try:
        safe_text = safe_markdown_v2(text)
        return safe_text, True
    except Exception as e:
        logging.warning(f"Failed to create safe MarkdownV2: {e}")
        return strip_markdown(text), False
