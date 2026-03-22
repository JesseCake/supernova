"""
perform_search tool — web search via SearXNG.
"""
from typing import Annotated
from pydantic import Field
import requests

from core.tool_base import ToolBase

log = ToolBase.logger('perform_search')


# ── Schema function ───────────────────────────────────────────────────────────

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

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params        = ToolBase.params(tool_args)
    query         = params.get('query', '')
    num_responses = int(params.get('number', 10))

    ToolBase.speak(core, session, f"Searching for '{query}'.")
    log.info("Search started", extra={'data': f"q={query!r} n={num_responses}"})

    try:
        searxng_url = tool_config.get('searxng_url', 'http://localhost:8888')
        response    = requests.get(
            searxng_url + "/search",
            params={
                "q":          query,
                "format":     "json",
                "language":   "en-AU",
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
                "title":   title[:100],
                "snippet": r.get("content", "")[:200],
                "link":    r.get("url"),
            })

        if not results:
            results.append({"error": "No results found, try rephrasing the query"})

        log.info("Search complete", extra={'data': f"found={len(results)}"})

    except requests.exceptions.ConnectionError:
        log.warning("Search service unavailable")
        results = [{"error": "Search service unavailable"}]

    except Exception as e:
        log.error("Search failed", exc_info=True)
        results = [{"error": f"Search failed: {e}"}]

    return ToolBase.result(core, 'perform_search', {
        "instruction": (
            "Use these results to answer the user's question directly if possible, "
            "or choose the most relevant URL to open with the open_website tool for further research. "
            "Respond in English only. Do not reproduce these results verbatim. "
            "If you don't have enough info, search again and increase the number of results."
        ),
        "results": results,
    })