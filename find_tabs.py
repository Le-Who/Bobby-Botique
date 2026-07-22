import re

with open(r'e:\Projects\gemaibotv2\app\templates\admin_daily.html', encoding='utf-8') as f:
    for i, line in enumerate(f):
        if 'id="tab-' in line:
            print(f"Line {i+1}: {line.strip()}")
