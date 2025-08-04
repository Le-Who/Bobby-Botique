import re

def strip_markdown(text: str) -> str:
    """Removes all Markdown formatting from the text."""
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'[*_~`]', '', text)
    text = text.replace('\\', '')
    return text

def format_key_for_display(api_key: str) -> str:
    """Formats an API key for safe display."""
    if not isinstance(api_key, str) or len(api_key) < 10:
        return "Invalid Key"
    return f"{api_key[:5]}...{api_key[-4:]}"

def escape_markdown_v2(text: str) -> str:
    """
    A smart function to escape text for Telegram's MarkdownV2 parser.
    It preserves existing valid Markdown syntax while escaping literal special characters.
    """
    # Characters that need escaping in Telegram MarkdownV2
    # Note: `_` and `*` are handled by the regex logic, not this list.
    escape_chars = r'\[\]()~`>#+-=|{}.!'

    # Regex to find either a valid Markdown entity OR a character that needs escaping.
    # Groups:
    # 1: Bold (*...*)
    # 2: Italic (_..._)
    # 3: Inline code (`...`)
    # 4: Link ([...](...))
    # 5: A single character from escape_chars
    # The order is important to match longer sequences first.
    markdown_or_special_char_regex = re.compile(
        r'(\*.*?\*)|'          # Group 1: Bold
        r'(_.*?_)|'            # Group 2: Italic
        r'(`.*?`)|'            # Group 3: Inline code
        r'(\[.*?\]\(.*?\))|'    # Group 4: Link
        r'([{}])'.format(re.escape(escape_chars)) # Group 5: One of the special chars
    )

    def replacer(match):
        # If one of the valid markdown groups was found, return it unchanged.
        if match.group(1):  # Bold
            return match.group(1)
        if match.group(2):  # Italic
            return match.group(2)
        if match.group(3):  # Inline code
            return match.group(3)
        if match.group(4):  # Link
            return match.group(4)
        if match.group(5):  # A special character that needs escaping
            return '\\' + match.group(5)
        # This should not be reached
        return ''

    return markdown_or_special_char_regex.sub(replacer, text)
