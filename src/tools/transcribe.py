import logging

import httpx

logger = logging.getLogger(__name__)


async def transcribe_audio(
    audio_bytes: bytes,
    whisper_host: str,
    language: str = "he",
) -> str | None:
    """Send audio bytes to the local Whisper ASR service and return transcription text."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{whisper_host}/asr",
                params={
                    "encode": "true",
                    "task": "transcribe",
                    "language": language,
                    "output": "txt",
                },
                files={"audio_file": ("audio.ogg", audio_bytes, "audio/ogg")},
            )
            response.raise_for_status()
            text = response.text.strip()
            if text:
                logger.info(f"Whisper transcription ({len(text)} chars): {text[:100]}...")
                return text
            return None
    except httpx.HTTPStatusError as e:
        logger.error(f"Whisper ASR returned error {e.response.status_code}: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Whisper transcription failed: {e}")
        return None
