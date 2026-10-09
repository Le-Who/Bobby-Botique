"""Run the actual AudioWorklet with an independent continuous PCM oracle."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


def _processor_probe(rate: int, blocks: list[int], *, empty_inputs: bool = False) -> dict:
    node = shutil.which("node")
    if node is None:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required AudioWorklet regression needs Node.js")
        pytest.skip("AudioWorklet regression needs Node.js")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/browser/audio_processor_probe.cjs")],
        input=json.dumps({"rate": rate, "blocks": blocks, "emptyInputs": empty_inputs}),
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        cwd=root,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
@pytest.mark.parametrize("blocks", [[128], [64, 193, 17, 256]])
def test_one_second_produces_exactly_16000_samples_with_continuous_phase(rate, blocks):
    report = _processor_probe(rate, blocks)
    assert report["inputSamples"] == rate
    assert report["samples"] == 16000
    assert report["maximumPcmError"] <= 1, "render quantum boundaries must not reset the resampling phase"
    assert report["postedChunks"] > 0
    assert report["transferListsCorrect"] is True


def test_empty_inputs_preserve_phase_and_do_not_publish_audio():
    report = _processor_probe(44100, [1, 127, 256], empty_inputs=True)
    assert report["samples"] == 16000
    assert report["maximumPcmError"] <= 1
    assert report["emptyInputChangedState"] is False


def test_pcm_conversion_clips_signed_endpoints_and_uses_the_first_channel():
    report = _processor_probe(16000, [1600])
    assert report["clippedPcm"] == [-32768, -32768, -16384, 0, 16383, 32767, 32767]
    assert report["secondChannelIgnored"] is True
