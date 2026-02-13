import logging

from pydantic_ai import Agent, BinaryContent

logger = logging.getLogger(__name__)


async def analyze_image(
    image_bytes: bytes,
    prompt: str,
    model_name: str,
    mime_type: str = "image/jpeg",
) -> str | None:
    """Analyze an image using the multimodal LLM and return a Hebrew description."""
    try:
        agent = Agent(
            model=model_name,
            system_prompt=(
                "You are a helpful assistant that analyzes images. "
                "You MUST respond in Hebrew (עברית). "
                "Describe the image clearly and concisely. "
                "If the user asks a specific question about the image, answer it directly."
            ),
        )

        result = await agent.run(
            [
                BinaryContent(data=image_bytes, media_type=mime_type),
                prompt,
            ]
        )
        return result.output
    except Exception as e:
        logger.error(f"Image analysis failed: {e}")
        return None
