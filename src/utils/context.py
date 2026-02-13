import logging
from typing import List

from sqlmodel import select, desc
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Message

logger = logging.getLogger(__name__)

# Rough estimate: ~4 chars per token for multilingual (Hebrew + English mix)
_CHARS_PER_TOKEN = 4
_DEFAULT_TOKEN_BUDGET = 2000
_MAX_MESSAGES = 25
_MAX_REPLY_CHAIN_DEPTH = 10


async def resolve_reply_chain(
    session: AsyncSession,
    message: Message,
    max_depth: int = _MAX_REPLY_CHAIN_DEPTH,
) -> List[Message]:
    """Walk the reply chain backwards from a message, collecting ancestors.

    Returns messages in chronological order (oldest first).
    """
    chain: list[Message] = []
    current = message

    try:
        while current.reply_to_id and len(chain) < max_depth:
            parent = await session.get(Message, current.reply_to_id)
            if not parent:
                break
            chain.append(parent)
            current = parent
    except Exception:
        logger.exception("Failed to resolve reply chain for message %s", message.message_id)

    chain.reverse()
    return chain


def _estimate_tokens(messages: List[Message]) -> int:
    """Rough token estimate for a list of messages."""
    total_chars = sum(len(m.text or "") for m in messages)
    return total_chars // _CHARS_PER_TOKEN


async def build_context_window(
    session: AsyncSession,
    message: Message,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    max_messages: int = _MAX_MESSAGES,
) -> List[Message]:
    """Build a token-budgeted context window for a message.

    Priority order:
    1. Reply chain (always included first — enables follow-up questions)
    2. Recent messages to fill remaining budget

    Returns messages in chronological order (oldest first), deduplicated.
    """
    # 1. Resolve reply chain (highest priority)
    reply_chain = await resolve_reply_chain(session, message)
    chain_tokens = _estimate_tokens(reply_chain)

    # Track seen message IDs for deduplication
    seen_ids = {m.message_id for m in reply_chain}
    seen_ids.add(message.message_id)  # Exclude the triggering message itself

    remaining_budget = max(0, token_budget - chain_tokens)
    remaining_slots = max(0, max_messages - len(reply_chain))

    # 2. Fetch recent messages to fill remaining budget
    recent: list[Message] = []
    if remaining_budget > 0 and remaining_slots > 0:
        # Fetch more than needed so we can filter and still fill the budget
        fetch_limit = min(remaining_slots + 10, 50)
        stmt = (
            select(Message)
            .where(Message.chat_jid == message.chat_jid)
            .order_by(desc(Message.timestamp))
            .limit(fetch_limit)
        )
        res = await session.exec(stmt)
        candidates = list(res.all())

        used_tokens = 0
        for msg in candidates:
            if msg.message_id in seen_ids:
                continue
            msg_tokens = len(msg.text or "") // _CHARS_PER_TOKEN
            if used_tokens + msg_tokens > remaining_budget:
                break
            if len(recent) >= remaining_slots:
                break
            recent.append(msg)
            seen_ids.add(msg.message_id)
            used_tokens += msg_tokens

    # 3. Merge and sort chronologically (sorted returns new list)
    combined = sorted([*reply_chain, *recent], key=lambda m: m.timestamp)

    logger.debug(
        "Context window: %d reply-chain + %d recent = %d messages (~%d tokens)",
        len(reply_chain),
        len(recent),
        len(combined),
        _estimate_tokens(combined),
    )

    return combined
