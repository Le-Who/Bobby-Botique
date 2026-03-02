with open('app/utils/messaging.py') as f:
    content = f.read()

content = content.replace(
    'from telegram import Message',
    'from telegram import Message\nfrom app.utils.heartbeat import stop_heartbeat'
)

content = content.replace(
    'async def send_long_message(\n    message: Message,',
    'async def send_long_message(\n    message: Message,'
)

# find send_long_message body
if 'def send_long_message' in content:
    content = content.replace(
        '    """\n    Отправляет длинное message, разбивая его на части, if необходимо.\n    Использует safe HTML форматирование.\n    """',
        '    """\n    Отправляет длинное message, разбивая его на части, if необходимо.\n    Использует safe HTML форматирование.\n    """\n    stop_heartbeat(message.message_id)'
    )

if 'def send_formatted_message' in content:
    content = content.replace(
        '    """Wrapper for sending formatted messages."""\n    try:',
        '    """Wrapper for sending formatted messages."""\n    stop_heartbeat(message.message_id)\n    try:'
    )

if 'def edit_formatted_message' in content:
    content = content.replace(
        '    """Wrapper for editing formatted messages."""\n    try:',
        '    """Wrapper for editing formatted messages."""\n    stop_heartbeat(message.message_id)\n    try:'
    )

with open('app/utils/messaging.py', 'w') as f:
    f.write(content)

# Patch ai_core.py
with open('app/handlers/ai_core.py') as f:
    content = f.read()

content = content.replace(
    'from telegram import Message',
    'from telegram import Message\nfrom app.utils.heartbeat import stop_heartbeat'
)

content = content.replace(
    'async def handle_ai_response_error(\n    response_text: str,\n    placeholder_message: Message,\n    on_error_callback=None,\n) -> bool:\n    """\n    Check',
    'async def handle_ai_response_error(\n    response_text: str,\n    placeholder_message: Message,\n    on_error_callback=None,\n) -> bool:\n    """\n    Check'
)

# Wait we can just find the docstring and add it there
content = content.replace(
    '    Check if the response text contains an error prefix\n    and handle the fallback/keyboard appropriately.\n    """\n    if not response_text:',
    '    Check if the response text contains an error prefix\n    and handle the fallback/keyboard appropriately.\n    """\n    stop_heartbeat(placeholder_message.message_id)\n    if not response_text:'
)

with open('app/handlers/ai_core.py', 'w') as f:
    f.write(content)

# Patch agent.py
with open('app/handlers/agent.py') as f:
    content = f.read()

content = content.replace(
    'from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update',
    'from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update\nfrom app.utils.heartbeat import stop_heartbeat'
)

content = content.replace(
    '            try:\n                await placeholder_message.edit_text(\n                    "Обнаружен сложный запрос (изображение + поиск)',
    '            try:\n                stop_heartbeat(placeholder_message.message_id)\n                await placeholder_message.edit_text(\n                    "Обнаружен сложный запрос (изображение + поиск)'
)

content = content.replace(
    '            friendly = user_friendly_error(e)\n            await placeholder_message.edit_text(\n                friendly, reply_markup=build_retry_and_roles_keyboard()\n            )',
    '            friendly = user_friendly_error(e)\n            stop_heartbeat(placeholder_message.message_id)\n            await placeholder_message.edit_text(\n                friendly, reply_markup=build_retry_and_roles_keyboard()\n            )'
)

with open('app/handlers/agent.py', 'w') as f:
    f.write(content)

print("Done patching messaging, ai_core and agent")
