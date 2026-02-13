import asyncio
import logging

from cachetools import TTLCache
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from config import Settings
from handler.router import Router
from handler.whatsapp_group_link_spam import WhatsappGroupLinkSpamHandler
from handler.kb_qa import KBQAHandler
from gowa_sdk.webhooks import WebhookEnvelope
from whatsapp import WhatsAppClient
from .base_handler import BaseHandler
from models import Message, OptOut
from tools.transcribe import transcribe_audio

logger = logging.getLogger(__name__)

# In-memory processing guard: 4 minutes TTL to prevent duplicate handling
_processing_cache = TTLCache(maxsize=1000, ttl=4 * 60)
_processing_lock = asyncio.Lock()

_AUDIO_EXTENSIONS = frozenset({".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac"})
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})


class MessageHandler(BaseHandler):
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
        settings: Settings,
    ):
        self.router = Router(session, whatsapp, embedding_client, settings)
        self.whatsapp_group_link_spam = WhatsappGroupLinkSpamHandler(
            session, whatsapp, embedding_client, settings
        )
        self.kb_qa_handler = KBQAHandler(session, whatsapp, embedding_client, settings)
        self.settings = settings
        super().__init__(session, whatsapp, embedding_client)

    async def __call__(self, payload: WebhookEnvelope):
        message = await self.store_message(payload)

        if not message:
            return

        # Ignore messages sent by the bot itself
        my_jid = await self.whatsapp.get_my_jid()
        if message.sender_jid == my_jid.normalize_str():
            return

        # Ignore messages without text AND without media
        if not message.text and not message.media_url:
            return

        if message.sender_jid.endswith("@lid"):
            logger.info(
                f"Received message from {message.sender_jid}: {payload.model_dump_json()}"
            )

        # direct message
        if message and not message.group:
            if message.text:
                command = message.text.strip().lower()
                if command == "opt-out":
                    await self.handle_opt_out(message)
                    return
                elif command == "opt-in":
                    await self.handle_opt_in(message)
                    return
                elif command == "status":
                    await self.handle_opt_status(message)
                    return
            # if autoreply is enabled, send autoreply
            if self.settings.dm_autoreply_enabled:
                await self.send_message(
                    message.sender_jid,
                    self.settings.dm_autoreply_message,
                    message.message_id,
                )
            return

        # In-memory dedupe: if this message is already being processed/recently processed, skip
        if message and message.message_id:
            async with _processing_lock:
                if message.message_id in _processing_cache:
                    logger.info(
                        f"Message {message.message_id} already in processing cache; skipping."
                    )
                    return
                _processing_cache[message.message_id] = True

        # Check for /kb_qa command (super admin only)
        # This does not have to be a managed group
        if message.group and message.text and message.text.startswith("/kb_qa "):
            if message.chat_jid not in self.settings.qa_test_groups:
                logger.warning(
                    f"QA command attempted from non-whitelisted group: {message.chat_jid}"
                )
                return  # Silent failure
            # Check if sender is a QA tester
            if message.sender_jid not in self.settings.qa_testers:
                logger.warning(f"Unauthorized /kb_qa attempt from {message.sender_jid}")
                return  # Silent failure

            await self.kb_qa_handler(message)
            return

        # ignore messages from unmanaged groups
        if message and message.group and not message.group.managed:
            return

        mentioned = message.has_mentioned(my_jid)

        # Reply-to-bot = implicit mention (swipe-reply to a bot message)
        if not mentioned and message.reply_to_id:
            replied_msg = await self.session.get(Message, message.reply_to_id)
            if replied_msg and replied_msg.sender_jid == my_jid.normalize_str():
                mentioned = True

        if mentioned:
            # Try to enrich with transcribed voice from replied-to message
            message = await self._enrich_with_replied_audio(message)

            # Check for image (current message or replied-to message)
            image_result = await self._try_get_image(message)
            if image_result:
                image_bytes, mime_type = image_result
                prompt = message.text or "מה יש בתמונה?"
                await self.router.analyze_image(message, image_bytes, prompt, mime_type)
                return

            # Try to transcribe voice in current message
            message = await self._try_transcribe_voice(message)

            if not message.text:
                return

            await self.router(message)
            return

        if (
            message.group
            and message.group.notify_on_spam
            and message.text
            and "https://chat.whatsapp.com/" in message.text
        ):
            await self.whatsapp_group_link_spam(message)
            return

    async def _try_transcribe_voice(self, message: Message) -> Message:
        """If the message is a voice/audio message without text, transcribe it."""
        if message.text or not message.media_url:
            return message

        if not self._is_audio_media(message.media_url):
            return message

        media_result = await self.download_media(message)
        if not media_result:
            return message

        audio_bytes, _ = media_result
        transcription = await transcribe_audio(
            audio_bytes, self.settings.whisper_host
        )
        if not transcription:
            return message

        return Message(
            **{
                **message.model_dump(exclude={"text", "sender", "group", "replies", "reactions", "kb_topics"}),
                "text": transcription,
            }
        )

    async def _enrich_with_replied_audio(self, message: Message) -> Message:
        """If replying to a voice message, transcribe it and prepend to text."""
        if not message.reply_to_id:
            return message

        replied_msg = await self.session.get(Message, message.reply_to_id)
        if not replied_msg or not replied_msg.media_url:
            return message

        if not self._is_audio_media(replied_msg.media_url):
            return message

        media_result = await self.download_media(replied_msg)
        if not media_result:
            return message

        audio_bytes, _ = media_result
        transcription = await transcribe_audio(
            audio_bytes, self.settings.whisper_host
        )
        if not transcription:
            return message

        enriched_text = f"[הודעה קולית מתומללת]: {transcription}\n\n{message.text or ''}"
        return Message(
            **{
                **message.model_dump(exclude={"text", "sender", "group", "replies", "reactions", "kb_topics"}),
                "text": enriched_text.strip(),
            }
        )

    async def _try_get_image(self, message: Message) -> tuple[bytes, str] | None:
        """Check current message and replied-to message for images, download if found."""
        # Check current message first
        if message.media_url and self._is_image_media(message.media_url):
            result = await self.download_media(message)
            if result:
                return result

        # Check replied-to message
        if message.reply_to_id:
            replied_msg = await self.session.get(Message, message.reply_to_id)
            if replied_msg and replied_msg.media_url and self._is_image_media(replied_msg.media_url):
                result = await self.download_media(replied_msg)
                if result:
                    return result

        return None

    @staticmethod
    def _is_audio_media(media_url: str) -> bool:
        """Check if media_url looks like audio content."""
        from urllib.parse import urlparse

        lower = media_url.lower()
        path = urlparse(lower).path
        if any(path.endswith(ext) for ext in _AUDIO_EXTENSIONS):
            return True
        # WhatsApp voice notes use "audio/" in MIME or "/ptt" in path
        return "audio/" in lower or "/ptt" in lower

    @staticmethod
    def _is_image_media(media_url: str) -> bool:
        """Check if media_url looks like image content."""
        from urllib.parse import urlparse

        lower = media_url.lower()
        path = urlparse(lower).path
        if any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS):
            return True
        return "image/" in lower

    async def handle_opt_out(self, message: Message):
        opt_out = await self.session.get(OptOut, message.sender_jid)
        if not opt_out:
            opt_out = OptOut(jid=message.sender_jid)
            await self.upsert(opt_out)
            await self.send_message(
                message.chat_jid,
                "You have been opted out. You will no longer be tagged in summaries and answers.",
            )
        else:
            await self.send_message(
                message.chat_jid,
                "You are already opted out.",
            )

    async def handle_opt_in(self, message: Message):
        opt_out = await self.session.get(OptOut, message.sender_jid)
        if opt_out:
            await self.session.delete(opt_out)
            await self.session.commit()
            await self.send_message(
                message.chat_jid,
                "You have been opted in. You will now be tagged in summaries and answers.",
            )
        else:
            await self.send_message(
                message.chat_jid,
                "You are already opted in.",
            )

    async def handle_opt_status(self, message: Message):
        opt_out = await self.session.get(OptOut, message.sender_jid)
        status = "opted out" if opt_out else "opted in"
        await self.send_message(
            message.chat_jid,
            f"You are currently {status}.",
        )
