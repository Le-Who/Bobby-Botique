# /app/state.py

import asyncio
from collections import defaultdict
from typing import Dict

# Используем defaultdict, чтобы замок для нового пользователя
# создавался автоматически при первом обращении.
# Это потокобезопасный способ управления блокировками.
USER_LOCKS: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
