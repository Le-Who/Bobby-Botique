# /app/repos/memory_tools.py
"""Agentic RAG — expose memory search as a Tool Declaration for Gemini function calling.

When the agentic research loop is active and LTM is enabled, the agent can call
`recall_memory(query)` to retrieve relevant long-term memories and knowledge graph
triples.  This replaces blank guessing with grounded personal context.

The tool declaration is registered via `get_memory_tool_declaration()` and the
execution handler is `execute_memory_tool()`.

Architecture:
    1. AgenticSearch._get_tools() includes the memory tool declaration when ltm_enabled.
    2. AgenticSearch._execute_tool() routes `recall_memory` calls here.
    3. We return memories + graph triples as structured JSON for the model.
"""

import logging
from typing import Any

from google.genai import types


def get_memory_tool_declaration() -> types.FunctionDeclaration:
    """Return the `recall_memory` tool declaration for the agentic research loop.

    This lets the agentic model proactively search the user's personal
    knowledge graph during research, grounding answers in user-specific context.
    """
    return types.FunctionDeclaration(
        name="recall_memory",
        description=(
            "Search the user's personal long-term memory and knowledge graph. "
            "Use this to recall personal facts, preferences, past conversations, "
            "work context, projects, and relationships. Returns relevant memories "
            "and knowledge graph triples."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Search query to find relevant memories (e.g. 'Python project', 'work at company X')",
                ),
            },
            required=["query"],
        ),
    )


async def execute_memory_tool(
    user_id: int,
    query: str,
    api_key: str,
) -> dict[str, Any]:
    """Execute the `recall_memory` tool call and return results.

    Returns:
        Dict with 'memories' (list of content strings) and 'graph_triples' (list of relation strings).
    """
    try:
        from app.repos.memory import search_memories_with_graph

        memories, graph_triples = await search_memories_with_graph(
            user_id,
            query,
            api_key,
            limit=5,
            min_similarity=0.55,
        )

        memory_texts = [
            {
                "content": m.get("content", ""),
                "source": m.get("source_type", "unknown"),
                "date": str(m.get("created_at", ""))[:10],
            }
            for m in memories
        ]

        return {
            "memories": memory_texts,
            "graph_triples": graph_triples,
            "total_found": len(memories),
        }

    except Exception as e:
        logging.warning("recall_memory tool execution failed: %s", e)
        return {"error": f"Memory search failed: {e}", "memories": [], "graph_triples": []}
