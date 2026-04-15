# app/utils/audio_processor.py
"""Backward-compatibility re-exports from multimodal_processor.

All new code should import directly from ``app.utils.multimodal_processor``.
"""

from app.utils.multimodal_processor import (  # noqa: F401
    THINKING_CONFIG_HIGH as THINKING_CONFIG,
    transcribe_voice,
)
