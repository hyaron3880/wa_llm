import logging
from typing import Sequence
from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from handler.knowledge_base_answers import KnowledgeBaseAnswers
from models import Message
from whatsapp.jid import parse_jid
from utils.chat_text import chat2text
from utils.opt_out import get_opt_out_map
from whatsapp import WhatsAppClient
from config import Settings
from .base_handler import BaseHandler
from services.prompt_manager import prompt_manager
from tools.image_analysis import analyze_image


# Creating an object
logger = logging.getLogger(__name__)


class IntentEnum(str, Enum):
    summarize = "summarize"
    ask_question = "ask_question"
    about = "about"
    other = "other"


class Intent(BaseModel):
    intent: IntentEnum = Field(
        description="""The intent of the message.
- summarize: Summarize TODAY's chat messages, or catch up on the chat messages FROM TODAY ONLY. This will trigger the summarization of the chat messages. This is only relevant for queries about TODAY's chat. A query across a broader timespan is classified as ask_question
- ask_question: Ask a question or learn from the collective knowledge of the group. This also handles real-time questions like weather, news, URLs, time/date - the agent has tools for these.
- about: Learn about me(bot) and my capabilities. This will trigger the about section.
- other: Completely unintelligible or empty input. Almost never used."""
    )


class Router(BaseHandler):
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
        settings: Settings,
    ):
        self.settings = settings
        self.ask_knowledge_base = KnowledgeBaseAnswers(
            session, whatsapp, embedding_client, settings
        )
        super().__init__(session, whatsapp, embedding_client)

    async def __call__(self, message: Message):
        if not message.text:
            return

        try:
            route = await self._route(message.text)
            match route:
                case IntentEnum.summarize:
                    await self.summarize(message)
                case IntentEnum.ask_question:
                    await self.ask_knowledge_base(message)
                case IntentEnum.about:
                    await self.about(message)
                case IntentEnum.other:
                    await self.ask_knowledge_base(message)
        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            await self.send_message(
                message.chat_jid,
                "סליחה, משהו השתבש. נסו שוב 🙏",
            )

    async def _route(self, message: str) -> IntentEnum:
        agent = Agent(
            model=self.settings.model_name,
            system_prompt=prompt_manager.render("intent.j2"),
            output_type=Intent,
        )

        result = await agent.run(message)
        return result.output.intent

    async def analyze_image(
        self,
        message: Message,
        image_bytes: bytes,
        prompt: str,
        mime_type: str = "image/jpeg",
    ):
        """Analyze an image and send the result back to the chat."""
        try:
            result = await analyze_image(
                image_bytes, prompt, self.settings.generation_model_name, mime_type
            )
            if result:
                await self.send_message(message.chat_jid, result)
            else:
                await self.send_message(
                    message.chat_jid,
                    "סליחה, לא הצלחתי לנתח את התמונה. נסו שוב 🙏",
                )
        except Exception as e:
            logger.error(f"Image analysis failed: {e}", exc_info=True)
            await self.send_message(
                message.chat_jid,
                "סליחה, משהו השתבש בניתוח התמונה. נסו שוב 🙏",
            )

    async def summarize(self, message: Message):
        time_24_hours_ago = datetime.now() - timedelta(hours=24)
        stmt = (
            select(Message)
            .where(Message.chat_jid == message.chat_jid)
            .where(Message.timestamp >= time_24_hours_ago)
            .order_by(desc(Message.timestamp))
            .limit(30)
        )
        res = await self.session.exec(stmt)
        messages: Sequence[Message] = res.all()

        # Get opt-out map for all senders in the history + current sender
        all_jids = {m.sender_jid for m in messages}
        all_jids.add(message.sender_jid)
        opt_out_map = await get_opt_out_map(self.session, list(all_jids))

        agent = Agent(
            model=self.settings.generation_model_name,
            system_prompt=prompt_manager.render("summarize.j2"),
            output_type=str,
        )

        sender_user = parse_jid(message.sender_jid).user
        sender_display = opt_out_map.get(sender_user, f"@{sender_user}")

        response = await agent.run(
            f"{sender_display}: {message.text}\n\n # History:\n {chat2text(list(messages), opt_out_map)}"
        )
        await self.send_message(
            message.chat_jid,
            response.output,
            in_reply_to=message.message_id,
        )

    async def about(self, message):
        await self.send_message(
            message.chat_jid,
            (
                "היי! אני הבוט של עומר 👋\n"
                "הנה מה שאני יודע לעשות:\n"
                "📝 *סיכום שיחות* - סיכום הודעות הצ'אט מהיום\n"
                "❓ *מענה על שאלות* - מבסיס הידע של הקבוצה\n"
                "🔍 *חיפוש באינטרנט* - חדשות ומידע עדכני\n"
                "🌤️ *מזג אוויר* - בכל מיקום בעולם\n"
                "🎤 *תמלול הודעות קוליות* - תגיבו להודעה קולית ותתייגו אותי\n"
                "🖼️ *ניתוח תמונות* - שלחו תמונה ותתייגו אותי\n"
                "🔗 *סיכום קישורים* - שלחו לינק ותבקשו סיכום\n"
                "⏰ *שעה ותאריך* - מה השעה עכשיו\n\n"
                "תייגו אותי עם שאלה ואנסה לעזור!"
            ),
        )

