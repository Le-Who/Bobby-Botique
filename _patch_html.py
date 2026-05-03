import re
import pathlib

p = pathlib.Path('app/templates/admin_dailycroc.html')
content = p.read_text(encoding='utf-8')

# Remove telegram script
content = re.sub(r'<script src="https://telegram\.org/js/telegram-web-app\.js"></script>\n?', '', content)

# Remove tg init
content = re.sub(r'const tg = window\.Telegram\.WebApp;\s*tg\.ready\(\);\s*tg\.expand\(\);\s*tg\.setHeaderColor\([^)]+\);\s*tg\.setBackgroundColor\([^)]+\);', '', content)

# Remove apiHeaders and getHeaders auth
content = re.sub(r"const apiHeaders = {[^}]+};", "const apiHeaders = { 'Content-Type': 'application/json' };", content)
content = re.sub(r"const getHeaders = {[^}]+};", "const getHeaders = {};", content)

# Fix image loading: remove loadImage completely
content = re.sub(r'async function loadImage\(imgEl\) \{.*?\n\}\n', '', content, flags=re.DOTALL)
content = re.sub(r"// Load images via authenticated fetch.*?card\.querySelectorAll\('img\[data-file-id\]'\)\.forEach\(loadImage\);", "", content, flags=re.DOTALL)

# Fix image tag
content = re.sub(r'<img class="image-preview" data-file-id="\$\{escapedId\}" alt="puzzle image">', 
                 r'<img class="image-preview" src="/api/admin/dailycroc/image?file_id=${escapedId}" alt="puzzle image">', content)

# Fix URLs
content = content.replace('/webapp/api/admin/dailycroc', '/api/admin/dailycroc')

# Replace tg.showAlert with alert
content = content.replace('tg.showAlert(', 'alert(')

# Remove haptic feedback
content = re.sub(r'tg\.HapticFeedback\.[a-zA-Z]+\([^)]+\);', '', content)

p.write_text(content, encoding='utf-8')
print("Done")
