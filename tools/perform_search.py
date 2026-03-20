"""
perform_search tool — web search via SearXNG.
"""
from typing import Annotated
from pydantic import Field
import requests
import json


# ── Schema function ───────────────────────────────────────────────────────────
# This is what the ToolLoader passes to Ollama as the tool definition.
# The name must match the filename (perform_search).

def perform_search(
    query: Annotated[str, Field(description="The search query. Required.")],
    number: Annotated[int, Field(default=10, description="Number of results to return. Default is 10.")] = 10,
) -> str:
    """
    Perform a web search to research a topic or answer a question.
    Use when you need current information or have been asked to look something up.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────
# Called by core when the model invokes this tool.
# Signature must always be: execute(tool_args, session, core, tool_config)

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params = tool_args.get('parameters', {})
    query = params.get('query', '')
    num_responses = int(params.get('number', 10))

    core.send_whole_response(f"Performing Web Search on '{query}'.\n\r", session)
    core._log("perform_search start", session=session, extra=f"q={query} n={num_responses}")

    try:
        searxng_url = tool_config.get('searxng_url', 'http://localhost:8888')
        response = requests.get(
            searxng_url + "/search",
            params={
                "q": query,
                "format": "json",
                "language": "en-AU",
                "safesearch": 1,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for r in data.get("results", [])[:num_responses]:
            title = r.get("title", "")
            title = title.split("›")[-1].strip() if "›" in title else title
            results.append({
                "title": title[:100],
                "snippet": r.get("content", "")[:200],
                "link": r.get("url"),
            })

        if not results:
            results.append({"error": "No results found, try rephrasing the query"})

        core._log("perform_search end", session=session, extra=f"found={len(results)}")

    except requests.exceptions.ConnectionError:
        results = [{"error": "Search service unavailable"}]
    except Exception as e:
        results = [{"error": f"Search failed: {e}"}]

    return core._wrap_tool_result("perform_search", {
        "instruction": (
            "Use these results to answer the user's question directly if possible, "
            "or choose the most relevant URL to open with the open_website tool for further research. "
            "Respond in English only. Do not reproduce these results verbatim. "
            "If you don't have enough info, search again and increase the number of results."
        ),
        "results": results,
    })