import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from google.genai import types

from app.config import settings
from app.prompt_registry import get_registry
from app.providers.gemini import get_cached_genai_client
from app.search_services import parallel_search
from app.utils.stage_indicators import STAGES_AGENTIC_RESEARCH
from app.web_reader import read_url

logger = logging.getLogger(__name__)


class AgenticSearch:
    def __init__(self, model_name: str, api_key: str):
        self.model_name = model_name
        self.api_key = api_key
        # Reuse cached genai.Client to avoid per-request TLS/TCP setup
        self.client = get_cached_genai_client(api_key)
        self.max_iterations = settings.AGENTIC_MAX_ITERATIONS
        self.max_pages = settings.AGENTIC_MAX_PAGES

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
                                "url": types.Schema(type=types.Type.STRING, description="The target URL to read.")
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
        self, call: types.FunctionCall, user_id: int | None = None, chat_id: int | None = None
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

    async def run(
        self,
        query: str,
        on_status: Callable[[str], Awaitable[None]],
        user_id: int | None = None,
        chat_id: int | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        """
        Execute the agentic research loop.

        Args:
            query: The user's question.
            on_status: Async callback for UI updates.
            user_id: Optional user ID for tracking/personalized facts.
            chat_id: Optional chat ID.
            history: Optional conversation history from prior turns.

        Returns:
            The final synthesized answer string.
        """
        await on_status(STAGES_AGENTIC_RESEARCH[0][1])  # "Планирую исследование..."

        # 1. Prepare configuration and context
        contents: list[Any] = []

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
            system_instruction=self._get_system_instruction(),
            tools=self._get_tools(),
            temperature=0.4,  # Lower temperature for more analytical research
        )

        pages_read = 0
        iterations = 0

        # 2. Main Agentic Loop
        while iterations < self.max_iterations:
            iterations += 1
            logger.info("Agentic loop iteration %d/%d", iterations, self.max_iterations)

            try:
                # We use the sync client wrapped in unblock to be safe, or just call async client.
                # The google-genai SDK provides an async client accessible via `client.aio`
                response = await self.client.aio.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
            except Exception as e:
                logger.error("Agentic model generation failed: %s", e, exc_info=True)
                if iterations == 1:
                    return f"❌ Ошибка при запуске агента: {str(e)}"
                break  # Break out and try to force conclusion

            if not response.candidates:
                logger.warning("Agent returned no candidates")
                break  # Break out and force conclusion

            candidate = response.candidates[0]
            message_content = candidate.content
            if not message_content:
                logger.warning("Agent returned empty message content")
                break

            # Append model's response to history
            contents.append(message_content)

            parts = message_content.parts or []
            # Collect function call parts in one pass (avoids double-iteration from any())
            fc_parts = [p for p in parts if p.function_call]

            if fc_parts:
                # Process all function calls in parallel or sequentially (Google SDK allows returning multiple results in one Content)
                function_responses = []
                concluding_answer = None

                for part in fc_parts:
                    call = part.function_call
                    assert call is not None  # guaranteed by list comprehension filter
                    call_args = call.args if isinstance(call.args, dict) else {}
                    if call.name == "conclude_research":
                        logger.info("Agent decided to conclude research.")
                        concluding_answer = call_args.get(
                            "answer", "❌ Агент завершил работу, но не предоставил ответ."
                        )
                        break  # Stop processing other calls

                    # Enforce page limits
                    if call.name == "read_page":
                        if pages_read >= self.max_pages:
                            logger.info("Agent hit max pages limit (%d). Denying read_page.", self.max_pages)
                            function_responses.append(
                                types.Part.from_function_response(
                                    name=call.name,
                                    response={
                                        "error": f"Max page limit ({self.max_pages}) reached. Analyze existing data or conclude."
                                    },
                                )
                            )
                            continue
                        pages_read += 1
                        await on_status(STAGES_AGENTIC_RESEARCH[2][1])  # "Читаю источник..."
                    elif call.name == "search_web":
                        await on_status(STAGES_AGENTIC_RESEARCH[1][1])  # "Ищу информацию..."

                    # Execute tool
                    result = await self._execute_tool(call, user_id, chat_id)

                    function_responses.append(types.Part.from_function_response(name=str(call.name), response=result))

                if concluding_answer:
                    return concluding_answer

                # Append all tool results to history for the model to see in the next turn
                if function_responses:
                    contents.append(
                        types.Content(
                            role="user",  # Function responses are sent back as 'user' or 'function' role depending on SDK wrapper, in genai it often goes as 'user' role with function_response parts
                            parts=function_responses,
                        )
                    )

                # Update status indicating we are processing/refining
                if iterations < self.max_iterations:
                    await on_status(STAGES_AGENTIC_RESEARCH[3][1])  # "Уточняю запрос..."

                continue  # Loop again with the new context
            else:
                # No function calls - this means the model decided to answer directly as text
                # We usually want it to use conclude_research, but if it doesn't, this is the fallback
                logger.info("Agent provided direct text answer without using conclude_research.")
                return str(parts[0].text) if parts and parts[0].text else "❌ Отсутствует текст в ответе."

        # 3. Force conclusion if loop maxed out or broke unexpectedly
        logger.warning("Agentic loop finished without explicit conclusion. Forcing synthesis.")
        await on_status(STAGES_AGENTIC_RESEARCH[-1][1])  # "Формирую ответ..."

        try:
            # Drop tools and ask it to synthesize everything it knows so far
            config.tools = None
            config.system_instruction = "Synthesize all the gathered information so far into a coherent final answer to the user's original query. Do not ask for tools. Format in Markdown."
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
                config=config,
            )
            if response.text:
                return response.text
        except Exception as e:
            logger.error("Forced synthesis failed: %s", e)

        return "❌ К сожалению, агенту не удалось собрать достаточно информации для ответа в отведенное время."
