"""Cross-source formatting cleanup retains exact character provenance."""

import pytest

from app.providers import tts


@pytest.mark.parametrize(
    "parts,expected",
    [
        (["Start ```hidden same", "same hidden``` Finish"], ["Start ", " Finish"]),
        (["[First visible", " second visible](https://example.test) end"], ["First visible", " second visible end"]),
        (["Before <span attr='", "value'>inside</span> after"], ["Before ", "inside after"]),
        (["one `inline", " code` two"], ["one inline", " code two"]),
        (["Before ![image", "](https://example.test) after"], ["Before ", " after"]),
        (["  🐱", "🐶\n", "\n\nend  "], ["🐱", "\n", "\nend"]),
        (["same ```same", " same```same"], ["same ", "same"]),
        (["prefix https://exam", "ple.test trailing"], ["prefix ", " trailing"]),
        (["# Ti", "tle\n## body"], ["Ti", "tle\nbody"]),
        (["```all", " removed```", "survives"], ["", "", "survives"]),
    ],
)
def test_shared_cleanup_preserves_surviving_words_in_their_original_sources(parts, expected):
    result = tts.clean_speech_parts(parts)
    assert result == expected
    assert "".join(result) == tts._clean_text_for_speech("".join(parts))


@pytest.mark.parametrize("split", range(1, 50))
def test_cleanup_semantics_match_existing_single_text_path_at_every_physical_cut(split):
    text = "  # Title\n**same** `code` [same](url) <b>same</b> 🐱🐶\n\n\nend  "
    parts = [text[:split], text[split:]]
    assert "".join(tts.clean_speech_parts(parts)) == tts._clean_text_for_speech(text)


@pytest.mark.parametrize("parts", [[""] * 33, ["я" * 131073], ["x" * 131073, "x" * 131072]])
def test_shared_cleanup_rejects_excess_sources_or_utf8_bytes_before_mapping(parts):
    from app.providers.tts_audio import TTSRequestError

    with pytest.raises(TTSRequestError, match="input_too_large"):
        tts.clean_speech_parts(parts)


def test_shared_cleanup_keeps_empty_source_slots_and_accepts_no_sources():
    assert tts.clean_speech_parts([]) == []
    assert tts.clean_speech_parts(["", "   ", ""]) == ["", "", ""]
