"""Small paid probe of the real Responses adapter with synthetic Russian prompts.

Run explicitly; never loads .env, starts the bot, contacts Telegram or reads chats.
Results describe this local run, not a provider latency guarantee.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from contextlib import aclosing
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.openai import OpenAIProvider, close_http_clients
from app.providers.stream_types import (
    GenerationRequest,
    PromptRole,
    PromptTurn,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TextPart,
    ThinkingLevel,
    Workload,
)

CASES = (
    ("chat", "Посоветуй, как вежливо отказаться от встречи сегодня. Ответь по-русски, не более трёх предложений."),
    ("inline", "Объясни простыми словами, почему небо голубое. Ответь по-русски, не более двух предложений."),
    (
        "judge_true",
        'В игре загадан «велосипед». Игрок написал «велик». Верни только JSON {"correct":true} или {"correct":false}.',
    ),
    (
        "judge_false",
        'В игре загадан «крокодил». Игрок написал «бегемот». Верни только JSON {"correct":true} или {"correct":false}.',
    ),
    (
        "chat_context",
        "Продолжи разговор: я устал после работы, хочу спокойно отдохнуть дома. Предложи два варианта без затрат.",
    ),
)


def valid_answer(case: str, answer: str) -> bool:
    if case.startswith("judge_"):
        try:
            return json.loads(answer) == {"correct": case == "judge_true"}
        except ValueError:
            return False
    return len(answer.strip()) >= 15 and any("а" <= char.lower() <= "я" for char in answer)


async def probe(api_key: str, *, compare_legacy: bool) -> list[dict]:
    candidates = [("gpt-6-luna", "off"), ("gpt-6.1-terra", "off"), ("gpt-6.1-terra", "low")]
    if compare_legacy:
        candidates += [("gpt-4o-mini", "off"), ("gpt-4.1-mini", "off")]
    results = []
    provider = OpenAIProvider(api_key)
    try:
        # Interleave identical cases; there is no concurrency or hidden retry.
        for case, prompt in CASES:
            for model, effort in candidates:
                model_id = f"openai-direct/{model}"
                turns = []
                if case == "chat_context":
                    turns = [
                        PromptTurn(PromptRole.USER, (TextPart("Я предпочитаю тихий отдых и люблю читать."),)),
                        PromptTurn(PromptRole.MODEL, (TextPart("Хорошо, учту это."),)),
                    ]
                turns.append(PromptTurn(PromptRole.USER, (TextPart(prompt),)))
                request = GenerationRequest(
                    models=(model_id,),
                    turns=tuple(turns),
                    system_instruction="Ты дружелюбный помощник. Следуй формату запроса, отвечай кратко по-русски.",
                    thinking_level=ThinkingLevel(effort),
                    workload=Workload.INLINE
                    if case in {"inline", "judge_true", "judge_false"}
                    else Workload.INTERACTIVE,
                    allow_deferred=False,
                    allow_key_race=False,
                    allow_model_fallback=False,
                )
                started = time.perf_counter()
                first = None
                chunks = []
                row: dict[str, Any] = {"model": model, "effort": "none" if effort == "off" else effort, "case": case}
                try:
                    async with asyncio.timeout(30), aclosing(provider.stream(request, model_name=model_id)) as stream:
                        async for event in stream:
                            if isinstance(event, TextDelta):
                                if first is None:
                                    first = time.perf_counter() - started
                                chunks.append(event.text)
                            elif isinstance(event, StreamCompleted):
                                row.update(
                                    completed=True,
                                    input_tokens=event.usage.prompt,
                                    output_tokens=event.usage.completion,
                                    cached_tokens=event.usage.cached,
                                )
                            elif isinstance(event, StreamFailed):
                                row.update(completed=False, error=event.code.value, phase=event.phase.value)
                except Exception as exc:
                    # Exception text may contain transport data; record only the type.
                    row.update(completed=False, error=type(exc).__name__)
                answer = "".join(chunks)
                row.update(
                    first_text_seconds=round(first, 3) if first is not None else None,
                    total_seconds=round(time.perf_counter() - started, 3),
                    valid_answer=valid_answer(case, answer),
                    synthetic_answer=answer,
                )
                results.append(row)
                print(json.dumps({key: value for key, value in row.items() if key != "synthetic_answer"}), flush=True)
    finally:
        await close_http_clients()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, help="UTF-8 file outside the repository containing one OpenAI key")
    parser.add_argument("--compare-legacy", action="store_true", help="Also send ten small requests to older models")
    parser.add_argument("--output", type=Path, help="Write safe results including synthetic answers to this JSON file")
    args = parser.parse_args()
    api_key = (
        args.key_file.read_text(encoding="utf-8-sig").strip().strip("\"'")
        if args.key_file
        else (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEYS", "").split(",")[0]).strip()
    )
    if not api_key:
        parser.error("Provide --key-file or OPENAI_API_KEY/OPENAI_API_KEYS")
    results = asyncio.run(probe(api_key, compare_legacy=args.compare_legacy))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for model, effort in dict.fromkeys((row["model"], row["effort"]) for row in results):
        rows = [row for row in results if (row["model"], row["effort"]) == (model, effort)]
        samples = [row["first_text_seconds"] for row in rows if row.get("completed") and row["valid_answer"]]
        print(
            json.dumps(
                {
                    "summary": model,
                    "effort": effort,
                    "requests": len(rows),
                    "passed": len(samples),
                    "median_first_text_seconds": statistics.median(samples) if samples else None,
                    "range_first_text_seconds": [min(samples), max(samples)] if samples else None,
                }
            )
        )
    return 0 if all(row.get("completed") and row["valid_answer"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
