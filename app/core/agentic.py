from __future__ import annotations

import asyncio
import dataclasses
import gc
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from google.genai import types

from app.config import settings
from app.prompt_registry import get_registry
from app.providers.base import _build_thinking_config
from app.providers.gemini import get_cached_genai_client
from app.search_services import parallel_search
from app.utils.stage_indicators import STAGES_AGENTIC_RESEARCH
from app.web_reader import read_url

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class AgenticResult:
    """Return value of AgenticSearch.run()."""

    answer: str
    total_tokens: int = 0
    llm_calls: int = 0


class AgenticSearch:
    def __init__(
        self,
        model_name: str,
        api_key: str,
        on_key_used: Callable[[], Awaitable[None]] | None = None,
    ):
        self.model_name = model_name
        self.api_key = api_key
        # Reuse cached genai.Client to avoid per-request TLS/TCP setup
        self.client = get_cached_genai_client(api_key)
        self.max_iterations = settings.AGENTIC_MAX_ITERATIONS
        self.max_pages = settings.AGENTIC_MAX_PAGES
        # Called after every generate_content call so the handler can
        # increment key usage (+1 per LLM invocation).
        self._on_key_used = on_key_used

    def _get_system_instruction(self) -> str:
        """Compose the RESEARCH_AGENT_SYSTEM prompt with configuration injected."""
        registry = get_registry()
        try:
            prompt = registry.get_task_prompt("research_agent_system", max_pages=str(self.max_pages))
            return prompt
        except KeyError:
            logger.error("research_agent_system prompt not found in registry. Falling back.")
            return "You are a research agent. Search the web and answer the user."

    def _get_tools(self) -> list[Any]:
        """Define the tools available to the agent."""
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="search_web",
                        description="Perform parallel web searches. Prioritize diverse queries to find the best sources.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "queries": types.Schema(
                                    type=types.Type.ARRAY,
                                    items=types.Schema(type=types.Type.STRING),
                                    description="List of 1 to 3 search queries to execute in parallel.",
                                )
                            },
                            required=["queries"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="read_page",
                        description="Extract clean text content from a specific URL. Use this to read the full context of a page.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "url": types.Schema(
                                    type=types.Type.STRING,
                                    description="The target URL to read.",
                                )
                            },
                            required=["url"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="conclude_research",
                        description="End the research phase and provide the final answer to the user.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "answer": types.Schema(
                                    type=types.Type.STRING,
                                    description="The final, formatted markdown answer to present to the user.",
                                )
                            },
                            required=["answer"],
                        ),
                    ),
                ]
            )
        ]

    async def _execute_tool(
        self,
        call: types.FunctionCall,
        user_id: int | None = None,
        chat_id: int | None = None,
    ) -> dict:
        """Execute a tool requested by the model and return its result."""
        name = call.name
        args = call.args

        try:
            if name == "search_web":
                safe_args = args if isinstance(args, dict) else {}
                queries = safe_args.get("queries", [])
                if not queries:
                    return {"error": "No queries provided."}
                logger.info("Agent requested search: %s", queries)
                results = await parallel_search(queries, user_id=user_id, chat_id=chat_id, max_results=10)
                return {"results": results}

            elif name == "read_page":
                safe_args = args if isinstance(args, dict) else {}
                url = safe_args.get("url")
                if not url:
                    return {"error": "No URL provided."}
                logger.info("Agent requested read_page: %s", url)
                content = await read_url(url, timeout=12.0)
                return {"content": content}

            elif name == "conclude_research":
                # We handle conclude differently in the main loop, but just in case
                return {"status": "concluded"}
            else:
                return {"error": f"Unknown tool: {name}"}

        except Exception as e:
            logger.error(f"Error executing tool {name}: {e}", exc_info=True)
            return {"error": f"Execution failed: {str(e)}"}

    async def _notify_key_used(self) -> None:
        """Fire the on_key_used callback (if set) after each LLM call."""
        if self._on_key_used:
            try:
                await self._on_key_used()
            except Exception as e:
                logger.warning("on_key_used callback failed: %s", e)

    @staticmethod
    def _extract_token_count(response: Any) -> int:
        """Safely extract total_token_count from response.usage_metadata."""
        try:
            meta = getattr(response, "usage_metadata", None)
            if meta:
                return getattr(meta, "total_token_count", 0) or 0
        except Exception:
            pass
        return 0

    async def run(
        self,
        query: str,
        on_status: Callable[[str], Awaitable[None]],
        user_id: int | None = None,
        chat_id: int | None = None,
        history: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
    ) -> AgenticResult:
        """
        Execute the agentic research loop.

        Args:
            query: The user's question.
            on_status: Async callback for UI updates.
            user_id: Optional user ID for tracking/personalized facts.
            chat_id: Optional chat ID.
            history: Optional conversation history from prior turns.

        Returns:
            AgenticResult with the final answer, total tokens, and LLM call count.
        """
        await on_status(STAGES_AGENTIC_RESEARCH[0][1])  # "Планирую исследование..."
        # 1. Prepare configuration and context
        contents: list[Any] = []
        concluding_answer: str | None = None
        total_tokens = 0
        llm_calls = 0

        try:
            # Inject conversation history so the agent has context from prior turns
            if history:
                # Take only the last 10 entries to avoid token overflow
                for entry in history[-10:]:
                    role = entry.get("role", "user")
                    parts_data = entry.get("parts", [])
                    text_parts = []
                    for p in parts_data:
                        if isinstance(p, str):
                            text_parts.append(types.Part.from_text(text=p))
                        elif isinstance(p, dict) and "text" in p:
                            text_parts.append(types.Part.from_text(text=p["text"]))
                    if text_parts:
                        contents.append(types.Content(role=role, parts=text_parts))

            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=query)]))

            config = types.GenerateContentConfig(
                system_instruction=self._get_system_instruction(),  # Changed from system_instruction to self._get_system_instruction()
                temperature=0.2,  # Changed from 0.4 to 0.2
                tools=self._get_tools(),
            )

            tc = _build_thinking_config(self.model_name, thinking_level)
            if tc:
                config.thinking_config = tc

            # 2. Main reasoning loop
            pages_read = 0
            iterations = 0
            while iterations < self.max_iterations:
                iterations += 1
                logger.info(
                    "Agent loop iteration %d/%d for query '%s'",
                    iterations,
                    self.max_iterations,
                    query[:30],
                )

                try:
                    # Model thinks and decides (requires tools)
                    response = await self.client.aio.models.generate_content(
                        model=self.model_name,
                        contents=contents,
                        config=config,
                    )
                    # Track usage: +1 API call, accumulate tokens
                    llm_calls += 1
                    total_tokens += self._extract_token_count(response)
                    await self._notify_key_used()
                except Exception as e:
                    logger.error("GenAI call failed in loop: %s", e)
                    return AgenticResult(
                        answer="❌ Возникла ошибка при обращении к языковой модели.",
                        total_tokens=total_tokens,
                        llm_calls=llm_calls,
                    )

                if not response.candidates:
                    logger.warning("Agent returned no candidates")
                    break

                candidate = response.candidates[0]
                message_content = candidate.content
                if not message_content:
                    logger.warning("Agent returned empty message content")
                    break

                # Append model's response to history
                contents.append(message_content)

                parts = message_content.parts or []
                fc_parts = [p for p in parts if p.function_call]

                if fc_parts:
                    function_responses = []
                    concluding_answer = None

                    for part in fc_parts:
                        call = part.function_call
                        assert call is not None

                        # Convert call args to pure dict
                        if hasattr(call, "args") and call.args:
                            # Google SDK's arg objects behave like dicts but sometimes aren't
                            try:
                                call_args = dict(call.args)
                            except (ValueError, TypeError):
                                call_args = {}
                        else:
                            call_args = {}

                        call_name = str(call.name) if call.name else "unknown"

                        if call_name == "conclude_research":
                            logger.info("Agent decided to conclude research.")
                            concluding_answer = str(
                                call_args.get(
                                    "answer",
                                    "❌ Агент завершил работу, но не предоставил ответ.",
                                )
                            )
                            break

                        # Enforce page limits
                        if call_name == "read_page":
                            if pages_read >= self.max_pages:
                                logger.info(
                                    "Agent hit max pages limit (%d). Denying read_page.",
                                    self.max_pages,
                                )
                                function_responses.append(
                                    types.Part.from_function_response(
                                        name=call_name,
                                        response={
                                            "error": f"Max page limit ({self.max_pages}) reached. Analyze existing data or conclude."
                                        },
                                    )
                                )
                                continue
                            pages_read += 1
                            await on_status(STAGES_AGENTIC_RESEARCH[2][1])  # "Читаю источник..."
                        elif call_name == "search_web":
                            await on_status(STAGES_AGENTIC_RESEARCH[1][1])  # "Ищу информацию..."

                        # Execute tool
                        result = await self._execute_tool(call, user_id, chat_id)

                        # Should not happen since we execute directly without return_exceptions,
                        # but keeping it safe if _execute_tool decides to return an exception
                        result_dict = {"error": str(result)} if isinstance(result, Exception) else dict(result)  # type: ignore[unreachable]

                        function_responses.append(
                            types.Part.from_function_response(name=call_name, response=result_dict)
                        )

                    if concluding_answer:
                        # Agent has reached a conclusion
                        logger.info("Agent concluded research successfully.")
                        await on_status(STAGES_AGENTIC_RESEARCH[4][1])  # "Формирую итоговый ответ..."
                        return AgenticResult(
                            answer=concluding_answer,
                            total_tokens=total_tokens,
                            llm_calls=llm_calls,
                        )

                    # Append all tool results to history for the model to see in the next turn
                    if function_responses:
                        contents.append(
                            types.Content(
                                role="user",
                                parts=function_responses,
                            )
                        )

                    # Update status indicating we are processing/refining
                    if iterations < self.max_iterations:
                        await on_status(STAGES_AGENTIC_RESEARCH[3][1])  # "Уточняю запрос..."

                    continue  # Loop again with the new context
                else:
                    # No function calls - this means the model decided to answer directly as text
                    logger.info("Agent provided direct text answer without using conclude_research.")
                    direct_text = str(parts[0].text) if parts and parts[0].text else "❌ Отсутствует текст в ответе."
                    return AgenticResult(
                        answer=direct_text,
                        total_tokens=total_tokens,
                        llm_calls=llm_calls,
                    )

            # 3. Force conclusion if loop maxed out or broke unexpectedly
            logger.warning("Agentic loop finished without explicit conclusion. Forcing synthesis.")
            await on_status(STAGES_AGENTIC_RESEARCH[-1][1])  # "Формирую ответ..."

            try:
                # Build a fresh config for synthesis — do NOT mutate the loop's config
                synthesis_config = types.GenerateContentConfig(
                    system_instruction="Synthesize all the gathered information so far into a coherent final answer to the user's original query. Do not ask for tools. Format in Markdown.",
                    temperature=0.2,
                    tools=None,
                )
                # Propagate thinking_config if present on the original
                if hasattr(config, "thinking_config") and config.thinking_config:
                    synthesis_config.thinking_config = config.thinking_config
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(
                                text="Force conclusion: Provide your final answer based on the acquired context."
                            )
                        ],
                    )
                )
                response = await self.client.aio.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=synthesis_config,
                )
                # Track usage for the forced synthesis call
                llm_calls += 1
                total_tokens += self._extract_token_count(response)
                await self._notify_key_used()
                if response.text:
                    return AgenticResult(
                        answer=response.text,
                        total_tokens=total_tokens,
                        llm_calls=llm_calls,
                    )
            except Exception as e:
                logger.error("Forced synthesis failed: %s", e)

            return AgenticResult(
                answer="❌ К сожалению, агенту не удалось собрать достаточно информации для ответа в отведенное время.",
                total_tokens=total_tokens,
                llm_calls=llm_calls,
            )

        finally:
            # MEMORY LEAK FIX:
            # The `contents` list accumulates all model responses and tool results (up to 3 web pages x 15KB each).
            # The GenAI SDK's proto-plus objects create deep reference cycles with internal channels.
            # Python's refcount GC cannot collect these immediately when `contents` falls out of scope,
            # resulting in ~250MB RAM bloat after a deep dive until cyclic GC runs.
            # Explicitly clear the container and force cyclic GC collection.
            contents.clear()
            gc.collect()
