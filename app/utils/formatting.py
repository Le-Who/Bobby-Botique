import re
import html
import logging
from typing import Tuple, Optional

# Regex to find either a valid Markdown entity OR a character that needs escaping.
# Groups:
# 1: Bold (*...*)
# 2: Italic (_..._)
# 3: Inline code (`...`)
# 4: Link ([...](...))
# 5: A single character from escape_chars
# Note: We include * and _ in special chars to escape them if they are not part of valid formatting
MARKDOWN_SPECIAL_CHARS = r'_*[]()~`>#+-=|{}.!'
MARKDOWN_REGEX = re.compile(
    r'(\*.*?\*)|'          # Group 1: Bold (*text*)
    r'(_.*?_)|'            # Group 2: Italic (_text_)
    r'(`.*?`)|'            # Group 3: Inline code (`text`)
    r'(\[.*?\]\(.*?\))|'   # Group 4: Link ([text](url))
    r'([' + re.escape(MARKDOWN_SPECIAL_CHARS) + r'])' # Group 5: Special char
)

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

def escape_format_chars(text: str) -> str:
    """Экранирует фигурные скобки { и } для безопасного форматирования строк Python"""
    if not text:
        return text
    return text.replace('{', '{{').replace('}', '}}')

def escape_markdown_v2(text: str) -> str:
    """
    A smart function to escape text for Telegram's MarkdownV2 parser.
    It preserves existing valid Markdown syntax while escaping literal special characters.
    """
    # Characters that need escaping in Telegram MarkdownV2
    # Include ALL special characters to ensure safety.
    # We include `_` and `*` here so they are caught by Group 5 if not matched by Groups 1-2.
    escape_chars = r'\[\]()~`>#+-=|{}.!_*\\'

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

    return MARKDOWN_REGEX.sub(replacer, text)

class TelegramFormatter:
    """
    Финальная, упрощенная система форматирования для Telegram.
    Специально оптимизирована для работы с текстами от AI моделей.
    """
    
    @classmethod
    def format_text(cls, text: str, preserve_formatting: bool = True) -> Tuple[str, str]:
        """
        Форматирует текст для отправки в Telegram.
        
        Args:
            text: Исходный текст
            preserve_formatting: Сохранять ли форматирование
            
        Returns:
            Tuple[str, str]: (отформатированный_текст, parse_mode)
        """
        if not preserve_formatting or not text:
            return cls._strip_all_formatting(text), None
        
        # Пытаемся применить MarkdownV2
        formatted_text, success = cls._apply_markdown_v2(text)
        if success:
            return formatted_text, 'MarkdownV2'
        
        # Если MarkdownV2 не удался, пробуем HTML
        formatted_text, success = cls._apply_html_formatting(text)
        if success:
            return formatted_text, 'HTML'
        
        # Если ничего не получилось, возвращаем очищенный текст
        return cls._strip_all_formatting(text), None
    
    @classmethod
    def _apply_markdown_v2(cls, text: str) -> Tuple[str, bool]:
        """
        Применяет MarkdownV2 форматирование.
        
        Returns:
            Tuple[str, bool]: (отформатированный_текст, успех)
        """
        try:
            # Подготавливаем текст для MarkdownV2
            formatted = cls._prepare_markdown_v2(text)
            
            return formatted, True
            
        except Exception as e:
            logging.debug(f"MarkdownV2 formatting failed: {e}")
            return text, False
    
    @classmethod
    def _apply_html_formatting(cls, text: str) -> Tuple[str, bool]:
        """
        Применяет HTML форматирование как fallback.
        
        Returns:
            Tuple[str, bool]: (отформатированный_текст, успех)
        """
        try:
            html_text = cls._markdown_to_html(text)
            return html_text, True
            
        except Exception as e:
            logging.debug(f"HTML formatting failed: {e}")
            return text, False
    
    @classmethod
    def _prepare_markdown_v2(cls, text: str) -> str:
        """Подготавливает текст для MarkdownV2."""
        # Заменяем ** на * для жирного текста (Common Markdown -> Telegram MarkdownV2)
        text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)
        
        # Используем надежную функцию экранирования
        return escape_markdown_v2(text)
    
    @classmethod
    def _escape_char_safely(cls, text: str, char: str) -> str:
        """Безопасно экранирует символ."""
        escaped_char = '\\' + char
        
        if char in '[]()':
            # Для скобок проверяем контекст
            if char == '[':
                # Экранируем [ только если за ним не следует ]
                text = re.sub(r'\[(?!.*?\])', escaped_char, text)
            elif char == ']':
                # Экранируем ] только если перед ним не следует [
                text = re.sub(r'(?<!\[.*?)\]', escaped_char, text)
            elif char == '(':
                # Экранируем ( только если он не является частью ссылки
                text = re.sub(r'\((?!.*?\))', escaped_char, text)
            elif char == ')':
                # Экранируем ) только если перед ним не следует (
                text = re.sub(r'(?<!\(.*?)\)', escaped_char, text)
        else:
            # Для остальных символов просто экранируем
            text = text.replace(char, escaped_char)
        
        return text
    
    @classmethod
    def _markdown_to_html(cls, text: str) -> str:
        """Конвертирует Markdown в HTML."""
        # Экранируем HTML теги в исходном тексте для безопасности
        text = html.escape(text)

        # Код
        text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
        
        # Жирный текст
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.*?)\*', r'<b>\1</b>', text)
        
        # Курсив
        text = re.sub(r'_(.*?)_', r'<i>\1</i>', text)
        
        # Ссылки
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
        
        return text
    
    @classmethod
    def _is_safe_for_markdown_v2(cls, text: str) -> bool:
        """Проверяет, безопасен ли текст для MarkdownV2."""
        try:
            # Проверяем баланс скобок
            if not cls._check_bracket_balance(text):
                return False
            
            # Проверяем, что нет неэкранированных специальных символов
            # Добавляем _, *, \ в список проверяемых символов
            special_chars = r'\[\]()~`>#+-=|{}.!_*\\'
            for char in special_chars:
                if char in text:
                    # Проверяем, что символ экранирован или является частью валидного Markdown
                    if not cls._is_char_safe(text, char):
                        return False
            
            return True
            
        except Exception:
            return False
    
    @classmethod
    def _check_bracket_balance(cls, text: str) -> bool:
        """Проверяет баланс скобок в тексте."""
        stack = []
        brackets = {'(': ')', '[': ']'}
        
        for char in text:
            if char in brackets:
                stack.append(char)
            elif char in brackets.values():
                if not stack:
                    return False
                if brackets[stack.pop()] != char:
                    return False
        
        return len(stack) == 0
    
    @classmethod
    def _is_char_safe(cls, text: str, char: str) -> bool:
        """Проверяет, безопасен ли символ в контексте."""
        # Ищем все вхождения символа
        for match in re.finditer(re.escape(char), text):
            pos = match.start()
            
            # Проверяем, экранирован ли символ
            if pos > 0 and text[pos-1] == '\\':
                continue
            
            # Если сам символ - обратный слеш, и он не экранирован (проверено выше),
            # то он должен экранировать следующий символ
            if char == '\\':
                if pos < len(text) - 1:
                    continue
                return False

            # Проверяем, является ли символ частью валидного Markdown
            if not cls._is_part_of_valid_markdown(text, pos):
                return False
        
        return True
    
    @classmethod
    def _is_part_of_valid_markdown(cls, text: str, pos: int) -> bool:
        """Проверяет, является ли символ частью валидного Markdown."""
        char = text[pos]
        
        if char in '[]()':
            # Проверяем, является ли это частью ссылки
            return cls._is_part_of_link(text, pos)
        elif char in '*_`':
            # Проверяем, является ли это частью форматирования
            return cls._is_part_of_formatting(text, pos)
        
        return False
    
    @classmethod
    def _is_part_of_formatting(cls, text: str, pos: int) -> bool:
        """Проверяет, является ли позиция частью форматирования."""
        # Поскольку текст предварительно обработан escape_markdown_v2,
        # любые неэкранированные символы форматирования (*, _, `)
        # являются частью валидной разметки (matched by Groups 1-3).
        return True
    
    @classmethod
    def _is_part_of_link(cls, text: str, pos: int) -> bool:
        """Проверяет, является ли позиция частью ссылки."""
        # Ищем ближайшие [ и ] до позиции
        before_text = text[:pos]
        after_text = text[pos:]
        
        # Проверяем, есть ли [ перед позицией и ] после
        has_open_bracket = '[' in before_text
        has_close_bracket = ']' in after_text
        
        return has_open_bracket and has_close_bracket
    
    @classmethod
    def _strip_all_formatting(cls, text: str) -> str:
        """Полностью удаляет все форматирование из текста."""
        # Удаляем Markdown
        text = strip_markdown(text)
        
        # Удаляем HTML теги
        text = re.sub(r'<[^>]*>', '', text)
        
        # Удаляем экранирующие символы
        text = text.replace('\\', '')
        
        return text.strip()
