with open('app/handlers/messages.py', 'r') as f:
    content = f.read()

# I used a bad replace before since I assumed from app.utils.text_format import split_text_safe was there.
if 'from app.utils.heartbeat import' not in content:
    content = content.replace(
        'from app import prompts, state',
        'from app import prompts, state\nfrom app.utils.heartbeat import register_heartbeat, stop_heartbeat, unregister_heartbeat'
    )

with open('app/handlers/messages.py', 'w') as f:
    f.write(content)
