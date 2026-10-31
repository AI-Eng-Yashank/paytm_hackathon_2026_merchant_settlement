"""
Conversations: every merchant session as a sequence of turns (merchant and AI), with recordings,
the facts the agent found (for memory on the next turn) and the merchant's mood.
"""

import json
import secrets
import time

from sqlalchemy import func, insert, select, update

from storage import db


CONVERSATION_IDLE_S = 30 * 60   # a new message after 30 idle minutes starts a new conversation
HISTORY_TURNS = 10              # merchant+AI pairs the agent sees from earlier in the conversation

C, M = db.conversations, db.messages


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


class ConversationStore:
    """Synchronous; call from a worker thread (asyncio.to_thread)."""

    def get_or_start(self, merchant_id: str, conversation_id: str | None, channel: str,
                     topic: str | None = None) -> str:
        """Continue the given conversation if it is this merchant's, on the same topic and still active,
        else start one. topic separates e.g. a collections call from the merchant's own support chat."""
        now = time.time()
        with db.use() as c:
            if conversation_id:
                row = c.execute(select(C).where(C.c.conversation_id == conversation_id)).first()
                if (row and row.merchant_id == merchant_id and row.topic == topic
                        and now - row.updated_at < CONVERSATION_IDLE_S):
                    return conversation_id
            new_id = f"C-{secrets.token_hex(3).upper()}"
            c.execute(insert(C).values(conversation_id=new_id, merchant_id=merchant_id, channel=channel,
                                       status="OPEN", created_at=now, updated_at=now, topic=topic))
            return new_id

    def add_message(self, conversation_id: str, role: str, content: str, *, ticket_id: str | None = None,
                    channel: str = "text", facts: list | None = None) -> int:
        now = time.time()
        with db.use() as c:
            msg_id = c.execute(insert(M).values(
                conversation_id=conversation_id, role=role, content=content, ticket_id=ticket_id,
                created_at=now, channel=channel,
                facts=json.dumps(facts, ensure_ascii=False, default=str) if facts else None,
            ).returning(M.c.id)).scalar_one()
            c.execute(update(C).where(C.c.conversation_id == conversation_id).values(updated_at=now))
        return msg_id

    def set_audio(self, message_id: int, audio_path: str):
        with db.use() as c:
            c.execute(update(M).where(M.c.id == message_id).values(audio_path=audio_path))

    def set_status(self, conversation_id: str, status: str):
        with db.use() as c:
            c.execute(update(C).where(C.c.conversation_id == conversation_id).values(status=status))

    def set_mood(self, conversation_id: str, mood: str, reason: str):
        with db.use() as c:
            c.execute(update(C).where(C.c.conversation_id == conversation_id).values(mood=mood, mood_reason=reason))

    def messages(self, conversation_id: str) -> list[dict]:
        with db.use() as c:
            return _rows(c.execute(select(M).where(M.c.conversation_id == conversation_id).order_by(M.c.id)))

    def get(self, conversation_id: str) -> dict | None:
        with db.use() as c:
            row = c.execute(select(C).where(C.c.conversation_id == conversation_id)).first()
        if not row:
            return None
        return {**dict(row._mapping), "messages": self.messages(conversation_id)}

    def list_for_merchant(self, merchant_id: str) -> list[dict]:
        with db.use() as c:
            convs = _rows(c.execute(select(C).where(C.c.merchant_id == merchant_id).order_by(C.c.updated_at.desc())))
            counts = {r.conversation_id: r.n for r in c.execute(
                select(M.c.conversation_id, func.count().label("n"))
                .where(M.c.conversation_id.in_([x["conversation_id"] for x in convs] or [""]))
                .group_by(M.c.conversation_id))}
        for conv in convs:
            conv["turns"] = counts.get(conv["conversation_id"], 0)
        return convs

    def history_for_llm(self, conversation_id: str) -> list[dict]:
        """Earlier turns as chat messages, preceded by one note with the facts the agent's tools found,
        so a follow-up like "मिले क्या?" ("found them?") is answered from what was already looked up.

        The facts go in a separate system note, not inside the AI's past replies: when they were part
        of the replies, the model copied that format into its next answer to the merchant."""
        rows = self.messages(conversation_id)[-HISTORY_TURNS * 2:]
        facts = [f for m in rows if m["facts"] for f in json.loads(m["facts"])]
        out = []
        if facts:
            out.append({"role": "system", "content":
                        "Facts your tools found earlier in this conversation (internal; use them to answer, "
                        "never quote or show this note to the merchant):\n" + json.dumps(facts, ensure_ascii=False)})
        for m in rows:
            out.append({"role": "user" if m["role"] == "merchant" else "assistant", "content": m["content"]})
        return out
