import logging
from typing import List

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from sqlmodel.ext.asyncio.session import AsyncSession
from tenacity import (
    retry,
    wait_random_exponential,
    stop_after_attempt,
    before_sleep_log,
)
from voyageai.client_async import AsyncClient

from models import Message
from whatsapp import WhatsAppClient
from whatsapp.jid import parse_jid
from utils.chat_text import chat2text
from utils.context import build_context_window
from utils.conversation_digest import get_conversation_digest
from utils.opt_out import get_opt_out_map
from utils.voyage_embed_text import voyage_embed_text
from .base_handler import BaseHandler
from config import Settings
from services.prompt_manager import prompt_manager
from tools.web_search import web_search
from tools.weather import get_weather
from tools.scraper import scrape_url
from tools.datetime_tool import get_current_datetime


# Creating an object
logger = logging.getLogger(__name__)


class KnowledgeBaseAnswers(BaseHandler):
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
        settings: Settings,
    ):
        self.settings = settings
        super().__init__(session, whatsapp, embedding_client)

    async def __call__(self, message: Message):
        # Ensure message.text is not None before passing to generation_agent
        if message.text is None:
            logger.warning(f"Received message with no text from {message.sender_jid}")
            return

        my_jid = await self.whatsapp.get_my_jid()
        bot_jid_str = my_jid.normalize_str()

        # Build smart context: reply chain + token-budgeted recent messages
        history = await build_context_window(self.session, message)

        # Get opt-out map
        all_jids = {m.sender_jid for m in history}
        all_jids.add(message.sender_jid)
        opt_out_map = await get_opt_out_map(self.session, list(all_jids))

        rephrased_result = await self.rephrasing_agent(
            my_jid.user, message, history, opt_out_map, bot_jid_str
        )
        # Get query embedding
        embedded_question = (
            await voyage_embed_text(self.embedding_client, [rephrased_result.output])
        )[0]

        # Determine which groups to search
        group_jids = None
        if message.group:
            group_jids = [message.group.group_jid]
            if message.group.community_keys:
                related_groups = await message.group.get_related_community_groups(
                    self.session
                )
                group_jids.extend([g.group_jid for g in related_groups])

        # Use hybrid search to get topics with their source messages
        from search.hybrid_search import hybrid_search, format_search_results_for_prompt

        search_results = await hybrid_search(
            session=self.session,
            query=message.text,
            query_embedding=embedded_question,
            group_jids=group_jids,
            vector_limit=10,
            messages_per_topic=5,
        )

        # Format results for the generation agent
        formatted_topics = format_search_results_for_prompt(search_results, opt_out_map)

        # Also prepare distances for logging
        similar_topics_distances = [
            f"topic_distance: {r.vector_distance}" for r in search_results
        ]

        # Generate ambient conversation digest (cached, cheap model)
        context_ids = {m.message_id for m in history}
        digest = await get_conversation_digest(
            self.session, message, self.settings.model_name, context_ids, bot_jid_str
        )

        sender_number = parse_jid(message.sender_jid).user
        generation_result = await self.generation_agent(
            message.text, formatted_topics, message.sender_jid, history, opt_out_map,
            bot_jid_str, digest,
        )
        logger.info(
            "RAG Query Results:\n"
            f"Sender: {sender_number}\n"
            f"Question: {message.text}\n"
            f"Rephrased Question: {rephrased_result.output}\n"
            f"Chat JID: {message.chat_jid}\n"
            f"Retrieved Topics: {len(search_results)}\n"
            f"Total Messages: {sum(len(r.messages) for r in search_results)}\n"
            f"Similarity Scores: {similar_topics_distances}\n"
            f"Generated Response: {generation_result.output}"
        )

        await self.send_message(
            message.chat_jid,
            generation_result.output,
            in_reply_to=message.message_id,
        )

    def _register_tools(self, agent: Agent) -> None:
        """Register web tools on the agent so the LLM can call them autonomously."""
        settings = self.settings

        @agent.tool_plain
        async def search_web(query: str) -> str:
            """Search the web for current information. Use when the knowledge base doesn't have the answer, or for real-time questions about news, events, etc."""
            if not settings.tavily_api_key:
                return "חיפוש אינטרנט לא זמין כרגע."
            return await web_search(query, settings.tavily_api_key)

        @agent.tool_plain
        async def weather(location: str) -> str:
            """Get current weather for a location. Use when the user asks about weather conditions."""
            return await get_weather(location)

        @agent.tool_plain
        async def read_url(url: str) -> str:
            """Read and extract content from a URL. Use when the user shares a link and asks to summarize or read it."""
            if not settings.firecrawl_api_key:
                return "קריאת קישורים לא זמינה כרגע."
            return await scrape_url(url, settings.firecrawl_api_key)

        @agent.tool_plain
        def current_datetime() -> str:
            """Get the current date and time in Israel timezone. Use when the user asks about the current time or date."""
            return get_current_datetime()

    @retry(
        wait=wait_random_exponential(min=1, max=30),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def generation_agent(
        self,
        query: str,
        topics: str,  # receives pre-formatted topics
        sender: str,
        history: List[Message],
        opt_out_map: dict[str, str],
        bot_jid: str | None = None,
        digest: str = "",
    ) -> AgentRunResult[str]:
        agent = Agent(
            model=self.settings.generation_model_name,
            system_prompt=prompt_manager.render("rag.j2"),
        )
        self._register_tools(agent)

        sender_user = parse_jid(sender).user
        sender_display = opt_out_map.get(sender_user, f"@{sender_user}")

        digest_section = ""
        if digest:
            digest_section = (
                f"\n\n# Broader conversation context (what the group discussed recently):\n{digest}"
            )

        prompt_template = (
            f"{sender_display}: {query}\n\n"
            f"# Recent chat history:\n{chat2text(history, opt_out_map, bot_jid)}\n\n"
            f"# Related Topics:\n{topics}"
            f"{digest_section}"
        )

        return await agent.run(prompt_template)

    @retry(
        wait=wait_random_exponential(min=1, max=30),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def rephrasing_agent(
        self,
        my_jid: str,
        message: Message,
        history: List[Message],
        opt_out_map: dict[str, str],
        bot_jid: str | None = None,
    ) -> AgentRunResult[str]:
        rephrased_agent = Agent(
            model=self.settings.model_name,
            system_prompt=prompt_manager.render("rephrase.j2", my_jid=my_jid),
        )

        return await rephrased_agent.run(
            f"{message.text}\n\n## Recent chat history:\n{chat2text(history, opt_out_map, bot_jid)}"
        )
