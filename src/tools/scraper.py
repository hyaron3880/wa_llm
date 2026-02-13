import logging

from firecrawl import AsyncFirecrawl

logger = logging.getLogger(__name__)

_MAX_CONTENT_LENGTH = 3000  # characters — keep within LLM context budget


async def scrape_url(url: str, api_key: str) -> str:
    """Scrape a URL using Firecrawl and return markdown content (truncated)."""
    try:
        client = AsyncFirecrawl(api_key=api_key)
        result = await client.scrape(
            url=url,
            formats=["markdown"],
        )

        if isinstance(result, dict):
            markdown = result.get("markdown", "")
        else:
            markdown = getattr(result, "markdown", "") or ""
        if not markdown:
            return "לא הצלחתי לחלץ תוכן מהקישור."

        if len(markdown) > _MAX_CONTENT_LENGTH:
            markdown = markdown[:_MAX_CONTENT_LENGTH] + "\n\n[...תוכן נוסף קוצר...]"

        return markdown
    except Exception as e:
        logger.error(f"URL scraping failed: {e}")
        return "לא הצלחתי לקרוא את הקישור. נסו שוב מאוחר יותר."
