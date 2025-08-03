import asyncio
from typing import Dict

# --- GLOBAL APP STATE ---
ACTIVE_USER_TASKS: Dict[int, asyncio.Task] = {}
