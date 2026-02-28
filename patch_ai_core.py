with open('app/handlers/ai_core.py', 'r') as f:
    content = f.read()

# Add stop_heartbeat where it needs to be inside handle_ai_response_error
if 'def handle_ai_response_error' in content:
    content = content.replace(
        '    Returns:\n        bool: True if ошибка была обработана, False if response успешный\n    """',
        '    Returns:\n        bool: True if ошибка была обработана, False if response успешный\n    """\n    stop_heartbeat(placeholder_message.message_id)'
    )

with open('app/handlers/ai_core.py', 'w') as f:
    f.write(content)
