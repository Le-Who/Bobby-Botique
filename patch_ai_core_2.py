with open('app/handlers/ai_core.py') as f:
    content = f.read()

content = content.replace(
    '        True if это была ошибка и она была обработана, False if это не ошибка\n    """\n    from app.errors',
    '        True if это была ошибка и она была обработана, False if это не ошибка\n    """\n    stop_heartbeat(placeholder_message.message_id)\n    from app.errors'
)

with open('app/handlers/ai_core.py', 'w') as f:
    f.write(content)
