from __future__ import annotations

import asyncio
import dataclasses
import gc
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from google.genai import types

from app.config import settings
from app.prompt_registry import get_registry
from app.providers.base import _build_thinking_config
from app.providers.gemini import get_cached_genai_client
from app.search_services import parallel_search
from app.utils.stage_indicators import STAGES_AGENTIC_RESEARCH
from app.web_reader import read_url

logger = logging.getLogger(__name__)

# ── URL normalization for deduplication ──────────────────────────────────────
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "ref",
        "source",
        "share",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "yclid",
        "spm",
    }
)


def _normalize_url(url: str) -> str:
    """Normalize URL for deduplication.

    Strips tracking-only query parameters (utm_*, fbclid, etc.) while
    preserving semantically significant params (article IDs, page numbers).
    """
    try:
        parsed = urlparse(url)
        path = (parsed.path or "/").rstrip("/") or "/"
        hostname = (parsed.hostname or "").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        # Parse and filter query params
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: sorted(v) for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        canon_query = urlencode(filtered, doseq=True)
        scheme = parsed.scheme or "https"
        base = f"{scheme}://{hostname}{path}"
        return f"{base}?{canon_query}" if canon_query else base
    except Exception:
        return url


# ── Improvement 2: Page content cache (multi-layer) ──────────────────────────
_page_cache: dict[str, str] = {}
_page_cache_times: dict[str, float] = {}
_PAGE_CACHE_TTL = 1800  # 30 minutes


def _get_cached_page(url: str) -> str | None:
    """Return cached page content if TTL is still valid."""
    key = hashlib.sha256(url.encode()).hexdigest()
    ts = _page_cache_times.get(key)
    if ts is not None and (time.monotonic() - ts) < _PAGE_CACHE_TTL:
        return _page_cache.get(key)
    # Expired or missing — evict if present
    _page_cache.pop(key, None)
    _page_cache_times.pop(key, None)
    return None


def _set_cached_page(url: str, content: str) -> None:
    """Cache page content with monotonic timestamp."""
    key = hashlib.sha256(url.encode()).hexdigest()
    # Enforce global maxsize=500 to prevent unbounded growth
    if len(_page_cache) >= 500 and key not in _page_cache:
        # Evict oldest entry
        oldest_key = min(_page_cache_times, key=_page_cache_times.get)  # type: ignore[arg-type]
        _page_cache.pop(oldest_key, None)
        _page_cache_times.pop(oldest_key, None)
    _page_cache[key] = content
    _page_cache_times[key] = time.monotonic()


# ── Improvement 3: Source quality scoring ────────────────────────────────────
# Domain classification for search result enrichment
_DOMAIN_TIERS: dict[str, str] = {
    # Tier A: Official / Academic
    "github.com": "code_repository",
    "stackoverflow.com": "developer_qa",
    "arxiv.org": "academic",
    "scholar.google.com": "academic",
    "docs.python.org": "official_docs",
    "developer.mozilla.org": "official_docs",
    "developer.android.com": "official_docs",
    "developer.apple.com": "official_docs",
    "learn.microsoft.com": "official_docs",
    "cloud.google.com": "official_docs",
    "aws.amazon.com": "official_docs",
    "docs.aws.amazon.com": "official_docs",
    "wikipedia.org": "encyclopedia",
    "en.wikipedia.org": "encyclopedia",
    "ru.wikipedia.org": "encyclopedia",
    # Tier B: Trusted community
    "reddit.com": "community",
    "habr.com": "community",
    "medium.com": "blog_platform",
    "dev.to": "developer_blog",
    "news.ycombinator.com": "community",
}

# Trusted domain prefixes (matched with startswith)
_TRUSTED_PREFIXES = ("docs.", "documentation.", "wiki.", "api.")


def _classify_domain(url: str) -> tuple[str, str]:
    """Classify a URL into (domain_type, quality_tier).

    Returns:
        (domain_type, quality_tier) where quality_tier is "A", "B", or "C".
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
    except Exception:
        return ("unknown", "C")

    # Strip www. prefix
    if hostname.startswith("www."):
        hostname = hostname[4:]

    # Exact match
    if hostname in _DOMAIN_TIERS:
        dtype = _DOMAIN_TIERS[hostname]
        tier = "A" if dtype in ("official_docs", "academic", "code_repository", "developer_qa", "encyclopedia") else "B"
        return (dtype, tier)

    # Check parent domain (e.g. "old.reddit.com" → "reddit.com")
    parts = hostname.split(".")
    if len(parts) > 2:
        parent = ".".join(parts[-2:])
        if parent in _DOMAIN_TIERS:
            dtype = _DOMAIN_TIERS[parent]
            tier = (
                "A"
                if dtype in ("official_docs", "academic", "code_repository", "developer_qa", "encyclopedia")
                else "B"
            )
            return (dtype, tier)

    # Trusted prefix heuristic (docs.*, readthedocs.io, etc.)
    if any(hostname.startswith(p) for p in _TRUSTED_PREFIXES):
        return ("official_docs", "A")
    if "readthedocs" in hostname:
        return ("official_docs", "A")

    return ("unknown", "C")


def _classify_freshness(published_date: str) -> str:
    """Classify published_date into a freshness label."""
    if not published_date or published_date == "None":
        return "unknown"
    try:
        from datetime import UTC, datetime

        # Tavily returns ISO 8601 dates
        dt = datetime.fromisoformat(published_date.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = now - dt
        if delta.days <= 30:
            return "recent"
        elif delta.days <= 365:
            return "this_year"
        else:
            return "older"
    except (ValueError, TypeError):
        return "unknown"


def _enrich_search_results(results: list[dict]) -> list[dict]:
    """Add domain_type, quality_tier, and freshness to each search result."""
    for item in results:
        url = item.get("url", "")
        domain_type, quality_tier = _classify_domain(url)
        freshness = _classify_freshness(item.get("published_date", ""))
        item["domain_type"] = domain_type
        item["quality_tier"] = quality_tier
        item["freshness"] = freshness
    return results


# ── Improvement 4: Query deduplication ───────────────────────────────────────
def _jaccard_similarity(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two strings."""
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _find_duplicate_queries(
    new_queries: list[str],
    previous_queries: list[str],
    threshold: float = 0.85,
) -> list[tuple[str, str]]:
    """Find queries that are too similar to previously executed ones.

    Returns list of (new_query, matched_previous_query) pairs.
    """
    duplicates: list[tuple[str, str]] = []
    for nq in new_queries:
        for pq in previous_queries:
            if _jaccard_similarity(nq, pq) >= threshold:
                duplicates.append((nq, pq))
                break
    return duplicates


# ── Improvement 5: Citation validation ───────────────────────────────────────
import re

_URL_PATTERN = re.compile(r"\[([^\]]*)\]\((https?://[^\)]+)\)")


def _validate_citations(answer: str, known_urls: set[str]) -> list[str]:
    """Find URLs cited in the answer that weren't in search results.

    Returns list of unknown URLs (for observability logging only).
    """
    cited_urls = {m.group(2) for m in _URL_PATTERN.finditer(answer)}
    unknown = []
    for url in cited_urls:
        # Normalize: strip trailing slash and fragment
        normalized = url.rstrip("/").split("#")[0]
        if not any(normalized.startswith(k.rstrip("/").split("#")[0]) for k in known_urls):
            unknown.append(url)
    return unknown


@dataclasses.dataclass(slots=True)
class AgenticResult:
    """Return value of AgenticSearch.run()."""

    answer: str
    total_tokens: int = 0
    llm_calls: int = 0
    pages_deduplicated: int = 0


class AgenticSearch:
    def __init__(
        self,
        model_name: str,
        api_key: str,
        on_key_used: Callable[[], Awaitable[None]] | None = None,
        *,
        ltm_enabled: bool = False,
        ltm_api_key: str | None = None,
        ltm_expected_epoch: int | None = None,
    ):
        self.model_name = model_name
        self.api_key = api_key
        # Reuse cached genai.Client to avoid per-request TLS/TCP setup
        self.client = get_cached_genai_client(api_key)
        self.max_iterations = int(settings.AGENTIC_MAX_ITERATIONS)
        self.max_pages = int(settings.AGENTIC_MAX_PAGES)
        self.max_tokens = int(settings.AGENTIC_MAX_TOKENS)
        self.timeout_seconds = float(settings.AGENTIC_TIMEOUT_SECONDS)
        # Called after every generate_content call so the handler can
        # increment key usage (+1 per LLM invocation).
        self._on_key_used = on_key_used
        # Agentic RAG: when LTM is enabled, the agent can call recall_memory
        self._ltm_enabled = ltm_enabled
        self._ltm_api_key = ltm_api_key
        self._ltm_expected_epoch = ltm_expected_epoch

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
        declarations = [
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

        # Agentic RAG: add recall_memory tool when LTM is enabled
        if self._ltm_enabled:
            from app.repos.memory_tools import get_memory_tool_declaration

            declarations.append(get_memory_tool_declaration())

        return [types.Tool(function_declarations=declarations)]

    async def _execute_tool(
        self,
        call: types.FunctionCall,
        user_id: int | None = None,
        chat_id: int | None = None,
        *,
        session_page_cache: dict[str, str] | None = None,
        seen_urls: set[str] | None = None,
    ) -> dict:
        """Execute a tool requested by the model and return its result.

        Args:
            call: The function call from the model.
            user_id: Optional user ID for tracking.
            chat_id: Optional chat ID for tracking.
            session_page_cache: Per-session URL→content cache (Improvement 2).
            seen_urls: Mutable set of all URLs seen during this session (Improvement 3).
        """
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
                # Improvement 3: Enrich search results with quality metadata
                results = _enrich_search_results(results)
                # Deduplicate results by normalized URL
                if seen_urls is not None:
                    unique_results = []
                    for r in results:
                        url = r.get("url")
                        if not url:
                            unique_results.append(r)
                            continue
                        norm = _normalize_url(url)
                        if norm not in seen_urls:
                            seen_urls.add(norm)
                            unique_results.append(r)
                        else:
                            logger.debug("Dedup: skipping %s (normalized: %s)", url[:80], norm[:80])
                    dedup_count = len(results) - len(unique_results)
                    if dedup_count:
                        logger.info("Deduplicated %d/%d search results", dedup_count, len(results))
                    results = unique_results
                return {"results": results, "_dedup_count": dedup_count if seen_urls else 0}

            elif name == "read_page":
                safe_args = args if isinstance(args, dict) else {}
                url = safe_args.get("url")
                if not url:
                    return {"error": "No URL provided."}
                logger.info("Agent requested read_page: %s", url)

                # Improvement 2: Check session-level cache first, then global cache
                if session_page_cache is not None and url in session_page_cache:
                    logger.info("Session cache hit for URL: %s", url)
                    return {"content": session_page_cache[url]}

                cached = _get_cached_page(url)
                if cached is not None:
                    logger.info("Global cache hit for URL: %s", url)
                    if session_page_cache is not None:
                        session_page_cache[url] = cached
                    return {"content": cached}

                content = await read_url(url, timeout=12.0)

                # Truncate content to limit token usage
                limit = int(settings.AGENTIC_PAGE_CONTENT_LIMIT)
                if len(content) > limit:
                    content = content[:limit] + f"\n\n[...truncated at {limit} chars. Full content at {url}]"
                    logger.debug("Truncated page content from %s to %d chars", url[:60], limit)

                # Cache the result at both levels
                _set_cached_page(url, content)
                if session_page_cache is not None:
                    session_page_cache[url] = content

                return {"content": content}

            elif name == "conclude_research":
                # We handle conclude differently in the main loop, but just in case
                return {"status": "concluded"}

            elif name == "recall_memory":
                # Agentic RAG: search user's long-term memory
                safe_args = args if isinstance(args, dict) else {}
                query = safe_args.get("query", "")
                if not query:
                    return {"error": "No query provided for recall_memory."}
                if not user_id or not self._ltm_api_key:
                    return {"error": "Memory not available (no user context or API key)."}
                logger.info("Agent requested recall_memory: %s", query[:60])
                from app.repos.memory_tools import execute_memory_tool

                return await execute_memory_tool(
                    user_id,
                    query,
                    self._ltm_api_key,
                    expected_epoch=self._ltm_expected_epoch,
                )
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
            if meta is not None:
                raw = getattr(meta, "total_token_count", 0)
                return int(raw) if raw else 0
        except (TypeError, ValueError):
            pass
        return 0

    async def run(
        self,
        query: str,
        on_status: Callable[..., Awaitable[None]],
        user_id: int | None = None,
        chat_id: int | None = None,
        history: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
    ) -> AgenticResult:
        """
        Execute the agentic research loop.

        Args:
            query: The user's question.
            on_status: Async callback for UI updates. Signature:
                       on_status(stage_text: str, *, detail: str | None = None)
            user_id: Optional user ID for tracking/personalized facts.
            chat_id: Optional chat ID.
            history: Optional conversation history from prior turns.
            thinking_level: Optional thinking level for the model.

        Returns:
            AgenticResult with the final answer, total tokens, and LLM call count.
        """
        await on_status(STAGES_AGENTIC_RESEARCH[0][1])  # "Планирую исследование..."
        # 1. Prepare configuration and context
        contents: list[Any] = []
        concluding_answer: str | None = None
        total_tokens = 0
        llm_calls = 0

        # Improvement 2: Per-session URL→content cache
        session_page_cache: dict[str, str] = {}
        # Improvement 3: Track all URLs seen (for citation validation + dedup)
        seen_urls: set[str] = set()
        # Improvement 4: Track all previous search queries (for dedup)
        previous_queries: list[str] = []
        # Improvement 4: Wall-clock start time
        start_time = time.monotonic()
        # Track deduplicated pages across the session
        pages_deduplicated = 0

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
                system_instruction=self._get_system_instruction(),
                temperature=0.2,
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

                # Improvement 4: Time cutoff — check BEFORE starting the LLM call
                elapsed = time.monotonic() - start_time
                if elapsed > self.timeout_seconds:
                    logger.warning(
                        "Agentic loop time cutoff reached (%.1fs > %ds). Forcing synthesis.",
                        elapsed,
                        self.timeout_seconds,
                    )
                    break

                # Improvement 4: Token budget — check BEFORE starting the LLM call
                if total_tokens > self.max_tokens:
                    logger.warning(
                        "Agentic loop token budget exhausted (%d > %d). Forcing synthesis.",
                        total_tokens,
                        self.max_tokens,
                    )
                    break

                logger.info(
                    "Agent loop iteration %d/%d for query '%s' (tokens=%d, elapsed=%.1fs)",
                    iterations,
                    self.max_iterations,
                    query[:30],
                    total_tokens,
                    elapsed,
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
                        pages_deduplicated=pages_deduplicated,
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
                    # ── Phase 1: Parse all calls, extract args, check conclude ──
                    parsed_calls: list[tuple[str, dict, types.FunctionCall]] = []
                    for part in fc_parts:
                        call = part.function_call
                        assert call is not None
                        if hasattr(call, "args") and call.args:
                            try:
                                call_args = dict(call.args)
                            except (ValueError, TypeError):
                                call_args = {}
                        else:
                            call_args = {}
                        call_name = str(call.name) if call.name else "unknown"

                        # Short-circuit on conclude
                        if call_name == "conclude_research":
                            logger.info("Agent decided to conclude research.")
                            concluding_answer = str(
                                call_args.get(
                                    "answer",
                                    "❌ Агент завершил работу, но не предоставил ответ.",
                                )
                            )
                            break
                        parsed_calls.append((call_name, call_args, call))

                    if concluding_answer:
                        logger.info("Agent concluded research successfully.")
                        await on_status(STAGES_AGENTIC_RESEARCH[4][1])

                        # Improvement 3: Citation validation (log-only)
                        unknown_citations = _validate_citations(concluding_answer, seen_urls)
                        if unknown_citations:
                            logger.info(
                                "Citation check: %d URL(s) not found in search results: %s",
                                len(unknown_citations),
                                unknown_citations[:5],
                            )

                        return AgenticResult(
                            answer=concluding_answer,
                            total_tokens=total_tokens,
                            llm_calls=llm_calls,
                            pages_deduplicated=pages_deduplicated,
                        )

                    # ── Phase 2: Batch validate page limits & query dedup ──
                    # (Risk 1.1 mitigation: validate ALL read_page calls upfront)
                    executable_calls: list[tuple[str, dict, types.FunctionCall]] = []
                    denied_responses: list[types.Part] = []

                    for call_name, call_args, call_obj in parsed_calls:
                        if call_name == "read_page":
                            if pages_read >= self.max_pages:
                                logger.info(
                                    "Agent hit max pages limit (%d). Denying read_page.",
                                    self.max_pages,
                                )
                                denied_responses.append(
                                    types.Part.from_function_response(
                                        name=call_name,
                                        response={
                                            "error": f"Max page limit ({self.max_pages}) reached. Analyze existing data or conclude."
                                        },
                                    )
                                )
                                continue
                            pages_read += 1
                            executable_calls.append((call_name, call_args, call_obj))

                        elif call_name == "search_web":
                            # Improvement 4: Query deduplication guard
                            queries = call_args.get("queries", [])
                            dupes = _find_duplicate_queries(queries, previous_queries)
                            if dupes and len(dupes) == len(queries):
                                # ALL queries are duplicates — send advisory
                                logger.info(
                                    "All search queries are duplicates of previous: %s",
                                    [d[0] for d in dupes],
                                )
                                denied_responses.append(
                                    types.Part.from_function_response(
                                        name=call_name,
                                        response={
                                            "advisory": "All these queries are very similar to ones already executed. "
                                            "Analyze existing results or refine with different keywords, or conclude."
                                        },
                                    )
                                )
                                continue
                            # Track queries for future dedup
                            previous_queries.extend(queries)
                            executable_calls.append((call_name, call_args, call_obj))
                        else:
                            executable_calls.append((call_name, call_args, call_obj))

                    # ── Phase 3: Parallel execution (Improvement 1) ──
                    # Improvement 5: Build a status summary BEFORE executing
                    search_queries_str = ""
                    read_urls_str = ""
                    for cn, ca, _ in executable_calls:
                        if cn == "search_web":
                            qs = ca.get("queries", [])
                            search_queries_str = ", ".join(f"«{q}»" for q in qs[:3])
                        elif cn == "read_page":
                            url = ca.get("url", "")
                            try:
                                domain = urlparse(url).hostname or url[:40]
                            except Exception:
                                domain = url[:40]
                            read_urls_str += f"📖 {domain}\n"

                    # Improvement 5: Send rich status update
                    if search_queries_str:
                        detail = f"🔍 {search_queries_str}"
                        await on_status(
                            f"{STAGES_AGENTIC_RESEARCH[1][1]} (шаг {iterations} из {self.max_iterations})",
                            detail=detail,
                        )
                    elif read_urls_str:
                        await on_status(
                            f"{STAGES_AGENTIC_RESEARCH[2][1]} (шаг {iterations} из {self.max_iterations})",
                            detail=read_urls_str.strip(),
                        )

                    # Execute all validated tools in parallel with semaphore
                    _sem = asyncio.Semaphore(3)

                    async def _run_tool(call_obj: types.FunctionCall, _s: asyncio.Semaphore = _sem) -> dict:
                        async with _s:
                            return await self._execute_tool(
                                call_obj,
                                user_id,
                                chat_id,
                                session_page_cache=session_page_cache,
                                seen_urls=seen_urls,
                            )

                    if executable_calls:
                        tool_results = await asyncio.gather(
                            *[_run_tool(call_obj) for _, _, call_obj in executable_calls],
                            return_exceptions=True,
                        )
                    else:
                        tool_results = []

                    # Build function responses (maintain order matching executable_calls)
                    function_responses: list[types.Part] = list(denied_responses)
                    for i, (call_name, _call_args, _call_obj) in enumerate(executable_calls):
                        result = tool_results[i]
                        if isinstance(result, Exception):
                            result_dict: dict[str, Any] = {"error": f"Execution failed: {result}"}
                        else:
                            result_dict = dict(result)  # type: ignore[arg-type]  # _execute_tool returns dict
                        # Accumulate dedup metrics and strip metadata before sending to model
                        pages_deduplicated += result_dict.pop("_dedup_count", 0)
                        function_responses.append(
                            types.Part.from_function_response(name=call_name, response=result_dict)
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
                        await on_status(
                            STAGES_AGENTIC_RESEARCH[3][1],
                            detail=f"Итерация {iterations}/{self.max_iterations} • {pages_read} стр. прочитано • {len(seen_urls)} источников",
                        )

                    continue  # Loop again with the new context
                else:
                    # No function calls - this means the model decided to answer directly as text
                    logger.info("Agent provided direct text answer without using conclude_research.")
                    direct_text = str(parts[0].text) if parts and parts[0].text else "❌ Отсутствует текст в ответе."

                    # Improvement 3: Citation validation (log-only)
                    unknown_citations = _validate_citations(direct_text, seen_urls)
                    if unknown_citations:
                        logger.info(
                            "Citation check: %d URL(s) not found in search results: %s",
                            len(unknown_citations),
                            unknown_citations[:5],
                        )

                    return AgenticResult(
                        answer=direct_text,
                        total_tokens=total_tokens,
                        llm_calls=llm_calls,
                        pages_deduplicated=pages_deduplicated,
                    )

            # 3. Force conclusion if loop maxed out or broke unexpectedly
            logger.warning("Agentic loop finished without explicit conclusion. Forcing synthesis.")
            await on_status(STAGES_AGENTIC_RESEARCH[-1][1])

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
                        pages_deduplicated=pages_deduplicated,
                    )
            except Exception as e:
                logger.error("Forced synthesis failed: %s", e)

            return AgenticResult(
                answer="❌ К сожалению, агенту не удалось собрать достаточно информации для ответа в отведенное время.",
                total_tokens=total_tokens,
                llm_calls=llm_calls,
                pages_deduplicated=pages_deduplicated,
            )

        finally:
            # MEMORY LEAK FIX:
            # The `contents` list accumulates all model responses and tool results (up to 3 web pages x 15KB each).
            # The GenAI SDK's proto-plus objects create deep reference cycles with internal channels.
            # Python's refcount GC cannot collect these immediately when `contents` falls out of scope,
            # resulting in ~250MB RAM bloat after a deep dive until cyclic GC runs.
            # Explicitly clear the container and force cyclic GC collection.
            # NOTE: gc.collect() is synchronous and blocks the event loop ("stop-the-world").
            # Running it in a thread pool prevents freezing all concurrent I/O (streaming, polling).
            # generation=1 targets young objects (where proto-plus cycles live) for minimal pause.
            contents.clear()
            await asyncio.to_thread(gc.collect, 1)
