"""Conversation memory, transcripts, recordings, honest status, mood and the customer view."""

import asyncio
import time

import main
from agent.customers import health
from services.mood import heuristic_mood, rate_mood
from services.scripted_llm import _Message
from storage import db
from conftest import SEED, run_ticket


def say(client, text, conversation_id=None, merchant="M1001"):
    r = client.post("/api/tickets", json={"merchant_id": merchant, "text": text, "conversation_id": conversation_id})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- memory ----------

def test_turns_continue_one_conversation(client):
    first = say(client, "Kal ₹4,500 ka payment aaya tha, settlement nahi aaya")
    second = say(client, "मिले क्या?", first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"]
    transcript = client.get(f"/api/conversations/{first['conversation_id']}").json()
    assert [m["role"] for m in transcript["messages"]] == ["merchant", "agent", "merchant", "agent"]
    assert transcript["messages"][1]["facts"]                     # AI turn keeps what its tools found
    assert transcript["messages"][0]["facts"] is None


def test_agent_sees_earlier_turns_and_facts(client, monkeypatch):
    seen = []
    real_chat = main.resolver.llm.chat

    async def spy(messages, tools):
        seen.append(list(messages))  # copy: the agent keeps appending tool calls to the same list
        return await real_chat(messages, tools)

    monkeypatch.setattr(main.resolver.llm, "chat", spy)
    first = say(client, "Mera TXN240001 ka settlement nahi aaya")
    seen.clear()
    say(client, "मिले क्या?", first["conversation_id"])
    context = seen[0]
    roles = [m["role"] for m in context]
    assert roles[:2] == ["system", "system"]                      # prompt + facts note
    assert "TXN240001" in context[1]["content"] and "Facts your tools found" in context[1]["content"]
    assert "Mera TXN240001" in context[2]["content"] and context[3]["role"] == "assistant"
    assert context[-1]["content"].endswith("मिले क्या?")


def test_facts_note_never_reaches_the_merchant(client, monkeypatch):
    async def leaky(messages, tools):
        return _Message(content="Paisa 2 ghante me aa jayega.\n[Facts from tools in this turn: {\"utr\": \"X\"}]")
    monkeypatch.setattr(main.resolver.llm, "chat", leaky)
    assert say(client, "kab aayega?")["reply"] == "Paisa 2 ghante me aa jayega."


def test_other_merchants_conversation_is_not_reused(client):
    mine = say(client, "TXN240001 nahi aaya")
    other = say(client, "TXN240004 kab aayega?", mine["conversation_id"], merchant="M1003")
    assert other["conversation_id"] != mine["conversation_id"]


def test_idle_conversation_starts_fresh(client):
    first = say(client, "TXN240001 nahi aaya")
    with db.engine.connect() as c:
        c.execute(db.conversations.update().where(db.conversations.c.conversation_id == first["conversation_id"])
                  .values(updated_at=time.time() - 3600))
    assert say(client, "aur ek baat", first["conversation_id"])["conversation_id"] != first["conversation_id"]


# ---------- honest status ----------

def test_question_without_lookup_is_needs_info_not_resolved(client):
    r = say(client, "Hello, kuch problem hai")
    assert r["status"] == "NEEDS_INFO"
    m = client.get("/api/metrics").json()
    assert m["needs_info"] == 1 and m["auto_resolved"] == 0


def test_follow_up_answered_from_earlier_facts_is_resolved(client):
    first = say(client, "Mera TXN240001 ka settlement nahi aaya")
    assert say(client, "मिले क्या?", first["conversation_id"])["status"] == "RESOLVED"


def test_recent_transactions_tool(store):
    r = store.list_recent_transactions("M1002", 5)
    assert [t["txn_id"] for t in r["transactions"]] == ["TXN240002", "TXN240003"] or \
           {t["txn_id"] for t in r["transactions"]} == {"TXN240002", "TXN240003"}
    assert all(t["settlement_status"] for t in r["transactions"])
    assert store.list_recent_transactions("M1002", 500)["transactions"].__len__() <= 20


# ---------- recordings ----------

class FakeSTT:
    async def transcribe(self, audio_bytes, filename):
        return "Mera kal ka ₹4,500 ka settlement nahi aaya, TXN240001", "hi-IN"


class FakeTTS:
    async def generate_audio(self, text, language):
        return b"RIFF" + b"\0" * 2000


def test_voice_turn_records_both_sides(client, monkeypatch):
    monkeypatch.setattr(main, "stt", FakeSTT())
    monkeypatch.setattr(main, "tts", FakeTTS())
    r = client.post("/api/voice", data={"merchant_id": "M1001"},
                    files={"audio": ("clip.webm", b"\x1aE\xdf\xa3" + b"x" * 4000, "audio/webm")}).json()
    msgs = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"]
    assert [m["channel"] for m in msgs] == ["voice", "voice"]
    merchant_audio, agent_audio = msgs[0]["audio_url"], msgs[1]["audio_url"]
    assert merchant_audio.endswith("-merchant.webm") and agent_audio.endswith("-agent.wav")
    assert client.get(merchant_audio).content.startswith(b"\x1aE\xdf\xa3")
    assert client.get(agent_audio).content.startswith(b"RIFF")
    # Reset deletes recordings along with the transcripts
    client.post("/api/reset")
    assert client.get(merchant_audio).status_code == 404


def test_recordings_cannot_escape_their_folder(client):
    assert client.get("/recordings/../.env").status_code == 404


def test_recording_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(main.settings, "record_calls", False)
    monkeypatch.setattr(main, "stt", FakeSTT())
    monkeypatch.setattr(main, "tts", FakeTTS())
    r = client.post("/api/voice", data={"merchant_id": "M1001"},
                    files={"audio": ("clip.webm", b"x" * 4000, "audio/webm")}).json()
    msgs = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"]
    assert all(m["audio_url"] is None for m in msgs)


# ---------- mood ----------

def test_mood_keywords():
    assert heuristic_mood(["Paisa kab aayega? Bahut problem ho rahi hai"])[0] == "worried"
    assert heuristic_mood(["Ye fraud hai, consumer court jaunga"])[0] == "angry"
    assert heuristic_mood(["TXN240004 ka status batao"])[0] == "calm"
    assert heuristic_mood(["a", "b", "c", "d"])[0] == "angry"     # 4+ messages in one conversation


def test_mood_saved_on_conversation(client):
    r = say(client, "7 din ho gaye, bahut problem ho rahi hai, TXN240005", merchant="M1004")
    for _ in range(50):
        conv = client.get(f"/api/conversations/{r['conversation_id']}").json()
        if conv["mood"]:
            break
        time.sleep(0.05)
    assert conv["mood"] == "worried" and conv["mood_reason"]


def test_mood_without_messages():
    assert asyncio.run(rate_mood([])) == ("calm", "no messages")


# ---------- customer view ----------

def test_health_score_deductions_are_explained():
    h = health(escalated=1, pending_approvals=1, needs_info=0, contacts_7d=3, mood="angry",
               compensation_due=200, risk_score=0.9)
    assert h["score"] == 100 - 15 - 10 - 10 - 15 - 10 - 10 and h["label"] == "at_risk"
    assert len(h["factors"]) == 6 and all(f["points"] < 0 and f["why"] for f in h["factors"])
    assert health(escalated=0, pending_approvals=0, needs_info=0, contacts_7d=1, mood="calm",
                  compensation_due=0, risk_score=0.1) == {"score": 100, "label": "healthy", "factors": []}


def test_customer_overview_after_approval_ticket(client):
    run_ticket(client, "DEMO-4")
    o = client.get("/api/customers/M1004").json()
    assert o["money"]["pending_approval"] == 25000 and o["money"]["compensation_due"] == 200
    whys = " | ".join(f["why"] for f in o["health"]["factors"])
    assert "waiting for approval" in whys and "compensation due" in whys
    assert o["counts"]["awaiting"] == 1 and len(o["conversations"]) == 1


def test_one_conversation_is_one_contact(client):
    first = say(client, "TXN240001 nahi aaya")
    say(client, "मिले क्या?", first["conversation_id"])
    say(client, "ok thanks", first["conversation_id"])
    o = client.get("/api/customers/M1001").json()
    assert o["counts"]["tickets"] == 1 and o["counts"]["contacts_7d"] == 1
    assert len(o["tickets"]) == 3                                  # every turn still in the timeline


def test_customer_list_puts_contacted_and_unhealthy_first(client):
    run_ticket(client, "DEMO-1")   # healthy
    run_ticket(client, "DEMO-5")   # escalated, risky merchant
    for _ in range(100):           # mood is rated in the background after each turn
        rows = client.get("/api/customers").json()
        if all(r["mood"] for r in rows[:2]):
            break
        time.sleep(0.05)
    assert [r["merchant_id"] for r in rows[:2]] == ["M1005", "M1001"]
    # M1005: escalated (-15), risky (-10), RBI compensation due (-10), worried (-7) = 58
    assert rows[0]["health"] == 58 and rows[0]["label"] == "watch" and rows[0]["open_issues"] == 1
    assert rows[1]["health"] > rows[0]["health"]
    assert len(rows) == len(SEED["merchants"])


def test_batch_creates_a_conversation_per_ticket(client):
    client.post("/api/batch")
    deadline = time.time() + 90
    while client.get("/api/metrics").json()["tickets"] < 50 and time.time() < deadline:
        time.sleep(0.2)
    with db.engine.connect() as c:
        assert c.execute(db.conversations.select()).fetchall().__len__() == 50


# ---------- full call recording (both sides, one file) ----------

def _wav(seconds: float, freq: int = 440, rate: int = 16000) -> bytes:
    import io, math, struct, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(int(seconds * rate))))
    return buf.getvalue()


class WavTTS:
    async def generate_audio(self, text, language):
        return _wav(1.0, 660, 22050)


def _voice_turn(client, conversation_id=None):
    data = {"merchant_id": "M1001", **({"conversation_id": conversation_id} if conversation_id else {})}
    return client.post("/api/voice", data=data, files={"audio": ("clip.wav", _wav(0.8), "audio/wav")}).json()


def test_full_call_recording_joins_both_sides_in_order(client, monkeypatch):
    monkeypatch.setattr(main, "stt", FakeSTT())
    monkeypatch.setattr(main, "tts", WavTTS())
    first = _voice_turn(client)
    conv = client.get(f"/api/conversations/{first['conversation_id']}").json()
    assert conv["recorded_turns"] == 2 and conv["call_recording_url"].endswith("?n=2")

    r = client.get(conv["call_recording_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert r.content[:3] == b"ID3" or r.content[:2] == b"\xff\xfb"
    size_two_turns = len(r.content)

    _voice_turn(client, first["conversation_id"])          # another merchant + AI turn
    conv = client.get(f"/api/conversations/{first['conversation_id']}").json()
    assert conv["call_recording_url"].endswith("?n=4")
    assert len(client.get(conv["call_recording_url"]).content) > size_two_turns   # rebuilt, longer


def test_text_only_conversation_has_no_call_recording(client):
    r = say(client, "TXN240001 nahi aaya")
    conv = client.get(f"/api/conversations/{r['conversation_id']}").json()
    assert conv["call_recording_url"] is None
    assert client.get(f"/api/conversations/{r['conversation_id']}/call-recording.mp3").status_code == 404
