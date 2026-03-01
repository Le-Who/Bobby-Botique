"""
Tests for stage_indicators utility module.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.utils.stage_indicators import (
    STAGES_CHAT,
    STAGES_DOCUMENT,
    STAGES_PHOTO,
    STAGES_SEARCH_DEEP,
    STAGES_SEARCH_QUICK,
    update_stage,
)


class TestStageDefinitions:
    """Verify stage sequences are well-formed."""

    @pytest.mark.parametrize(
        "stages,name",
        [
            (STAGES_CHAT, "CHAT"),
            (STAGES_SEARCH_QUICK, "SEARCH_QUICK"),
            (STAGES_SEARCH_DEEP, "SEARCH_DEEP"),
            (STAGES_PHOTO, "PHOTO"),
            (STAGES_DOCUMENT, "DOCUMENT"),
        ],
    )
    def test_stages_non_empty(self, stages, name):
        assert len(stages) >= 2, f"STAGES_{name} should have at least 2 entries"

    @pytest.mark.parametrize(
        "stages,name",
        [
            (STAGES_CHAT, "CHAT"),
            (STAGES_SEARCH_QUICK, "SEARCH_QUICK"),
            (STAGES_SEARCH_DEEP, "SEARCH_DEEP"),
            (STAGES_PHOTO, "PHOTO"),
            (STAGES_DOCUMENT, "DOCUMENT"),
        ],
    )
    def test_stages_tuple_format(self, stages, name):
        for i, entry in enumerate(stages):
            assert isinstance(entry, tuple), f"STAGES_{name}[{i}] must be a tuple"
            assert len(entry) == 2, f"STAGES_{name}[{i}] must have (emoji, text)"
            emoji, text = entry
            assert isinstance(emoji, str) and len(emoji) > 0
            assert isinstance(text, str) and len(text) > 0


class TestUpdateStage:
    """Tests for the update_stage helper function."""

    @pytest.mark.asyncio
    async def test_basic_stage_update(self):
        msg = MagicMock()
        msg.edit_text = AsyncMock()

        next_idx = await update_stage(msg, STAGES_CHAT, 0)

        msg.edit_text.assert_called_once_with("🤔 Думаю...")
        assert next_idx == 1

    @pytest.mark.asyncio
    async def test_stage_with_extra_text(self):
        msg = MagicMock()
        msg.edit_text = AsyncMock()

        await update_stage(msg, STAGES_PHOTO, 0, extra_text="2 изображения")

        msg.edit_text.assert_called_once_with("🖼️ Обрабатываю изображение...\n2 изображения")

    @pytest.mark.asyncio
    async def test_index_capping_at_end(self):
        msg = MagicMock()
        msg.edit_text = AsyncMock()

        # Index beyond list length should cap to last item
        next_idx = await update_stage(msg, STAGES_CHAT, 99)

        last_emoji, last_text = STAGES_CHAT[-1]
        msg.edit_text.assert_called_once_with(f"{last_emoji} {last_text}")
        assert next_idx == len(STAGES_CHAT) - 1

    @pytest.mark.asyncio
    async def test_sequential_stages(self):
        msg = MagicMock()
        msg.edit_text = AsyncMock()

        idx = 0
        for i, (emoji, text) in enumerate(STAGES_SEARCH_DEEP):
            idx = await update_stage(msg, STAGES_SEARCH_DEEP, idx)

        # After iterating all stages, index should be at the last one
        assert idx == len(STAGES_SEARCH_DEEP) - 1

    @pytest.mark.asyncio
    async def test_edit_failure_handled_gracefully(self):
        msg = MagicMock()
        msg.edit_text = AsyncMock(side_effect=Exception("message not modified"))

        # Should not raise, should return next index
        next_idx = await update_stage(msg, STAGES_CHAT, 0)
        assert next_idx == 1
