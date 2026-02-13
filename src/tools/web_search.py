import logging

from tavily import AsyncTavilyClient

logger = logging.getLogger(__name__)


async def web_search(query: str, api_key: str) -> str:
    """Search the web using Tavily and return formatted results."""
    try:
        client = AsyncTavilyClient(api_key=api_key)
        response = await client.search(query, max_results=5)
        results = response.get("results", [])
        if not results:
            return "לא נמצאו תוצאות חיפוש."

        formatted = []
        for r in results:
            title = r.get("title", "")
            content = r.get("content", "")
            url = r.get("url", "")
            formatted.append(f"- **{title}**: {content}\n  {url}")

        return "\n\n".join(formatted)
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return "חיפוש האינטרנט נכשל. נסו שוב מאוחר יותר."
