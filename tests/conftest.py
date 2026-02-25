"""
Root conftest – loads .env so that ``app.config.settings`` resolves to a real
Settings object for tests that import the production modules directly.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=False)
