# /app/context_assembler.py
"""
Backward-compatibility facade — all logic lives in ``app.context`` subpackage.

Existing imports continue to work::

    from app.context_assembler import ContextAssembler, get_assembler
    from app.context_assembler import DEFAULT_TOKEN_BUDGET, AssembledContext
"""

from app.context import *  # noqa: F401,F403
from app.context import get_assembler  # noqa: F811 — explicit re-export
