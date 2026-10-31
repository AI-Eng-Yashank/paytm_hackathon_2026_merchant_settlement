"""
SettlementResolver - Main Application.
FastAPI server for the merchant dispute agent and its live dashboard.

Ties together:
- Speech-to-Text (Sarvam Saaras), LLM with tool calling (Sarvam-105B), Text-to-Speech (Sarvam Bulbul)
- Mock Payments / Ledger / Bank / Credits / CRM systems
- Guardrails, human approval queue and audit log
- Live event stream to the dashboard (adapted from the receptionist's monitor WebSocket)
"""

import asyncio
import base64
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from services import SarvamLLMService, SarvamSTTService, SarvamTTSService, ScriptedLLMService
from storage import db
from mock_systems.store import MockStore
from mock_systems.collections_store import CollectionsStore
from agent import guardrails
from agent.audit import AuditStore
from agent.collections import ScriptedCollectionsPlanner, collections_profile, draft_reminder, greeting
from agent.conversations import ConversationStore
from agent.customers import CustomerView
from agent.resolver import Resolver
from services import call_recording as call_recording_service
from services.mood import rate_mood
from agent.timing import Timeline, debug, error_log, log_error, log_timing_line, timing_log
from agent.tools import TOOL_SYSTEM, execute_tool


app = FastAPI(
    title="SettlementResolver",
    description="Autonomous AI teammate that resolves merchant settlement disputes",
    version="0.1.0",
)

STATIC_PATH = Path(__file__).parent / "static"

db.init_db()
store = MockStore()
audit = AuditStore()
conversations = ConversationStore()
customers = CustomerView(store, conversations, audit)
cstore = CollectionsStore(store)
RECORDINGS_DIR = Path(settings.recordings_dir) if settings.recordings_dir else Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
llm = ScriptedLLMService(settings.scripted_delay_s) if settings.offline else SarvamLLMService()
stt = None if settings.offline else SarvamSTTService()
tts = None if settings.offline else SarvamTTSService()

# Dashboard WebSocket connections
monitor_connections: Set[WebSocket] = set()
batch_task: Optional[asyncio.Task] = None


async def broadcast(event: dict):
    """Broadcast an agent event to every connected dashboard."""
    if not monitor_connections:
        return
    message = json.dumps(event, ensure_ascii=False, default=str)
    disconnected = set()
    for connection in monitor_connections:
        try:
            await connection.send_text(message)
        except Exception as e:
            print(f"[Monitor] Error broadcasting to connection: {e}")
            disconnected.add(connection)
    monitor_connections.difference_update(disconnected)


resolver = Resolver(llm, store, audit, broadcast)

_session = (f"=== server start | mode={'offline-scripted' if settings.offline else 'sarvam'} "
            f"llm={settings.llm_model} reasoning={settings.reasoning_effort} stt={settings.stt_model}/{settings.stt_mode} "
            f"tts={settings.tts_model}/{settings.tts_speaker} db={db.backend}:{db.backend_host} "
            f"detailed_log={debug.enabled} ===")
timing_log.info(_session)
error_log.info(_session)


@app.middleware("http")
async def no_stale_frontend(request: Request, call_next):
    """Make browsers re-check the dashboard's HTML/JS/CSS on every load (cheap 304 if unchanged),
    so a fix to app.js reaches the page without a hard refresh."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/customers") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def log_unhandled_errors(request: Request, call_next):
    """Any unexpected exception in an endpoint is written to logs/errors.log with its traceback."""
    try:
        return await call_next(request)
    except Exception as e:
        log_error(f"{request.method} {request.url.path}", e)
        # Full details are in logs/errors.log; never show raw internals to the merchant
        return JSONResponse({"detail": "Kuch gadbad ho gayi. Kripya dobara koshish karein."}, status_code=500)


# ---------- pages ----------

@app.get("/")
async def serve_index():
    """Serve the dashboard."""
    return FileResponse(str(STATIC_PATH / "index.html"), media_type="text/html")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "mode": "offline-scripted" if settings.offline else "sarvam",
            "llm_model": None if settings.offline else settings.llm_model, "detailed_log": debug.enabled,
            "db": db.describe()}


@app.get("/customers")
async def serve_customers():
    """Customer (merchant) health dashboard."""
    return FileResponse(str(STATIC_PATH / "customers.html"), media_type="text/html")


app.mount("/static", StaticFiles(directory=str(STATIC_PATH)), name="static")
# Call recordings (merchant .webm, AI .wav); StaticFiles refuses paths outside the folder
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")


@app.websocket("/ws/events")
async def events_websocket(websocket: WebSocket):
    """Live agent events for the dashboard."""
    await websocket.accept()
    monitor_connections.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "connected", "metrics": audit.metrics()}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        monitor_connections.discard(websocket)


# ---------- reference data ----------

@app.get("/api/merchants")
def list_merchants():
    return store.list_merchants()


@app.get("/api/merchants/{merchant_id}")
def get_merchant(merchant_id: str):
    merchant = store.get_merchant(merchant_id)
    if not merchant:
        raise HTTPException(404, "Unknown merchant")
    return merchant


@app.get("/api/transactions/{txn_id}")
def get_transaction(txn_id: str, merchant_id: str):
    """Read-only: payment + settlement status for one of the merchant's transactions."""
    with db.session():
        payment = store.check_payment_status(merchant_id, txn_id)
        if payment.get("error"):
            raise HTTPException(404, "Transaction not found for this merchant")
        return {"payment": payment, "settlement": store.get_settlement_status(merchant_id, txn_id)}


@app.get("/api/scenarios")
def list_scenarios():
    """Demo tickets (DEMO-*) for the quick-pick buttons."""
    return store.list_seed_tickets("DEMO-")


# ---------- tickets ----------

class TicketIn(BaseModel):
    merchant_id: str
    text: str
    ticket_id: Optional[str] = None
    speak: bool = False
    conversation_id: Optional[str] = None


async def _tts(text: str, language: str, tl: Timeline, speaker: Optional[str] = None) -> Optional[bytes]:
    if not tts:
        return None
    try:
        with tl.span("text-to-speech (Bulbul)", f"{len(text)} chars, {language}") as span:
            audio = await tts.generate_audio(text, language, **({"speaker": speaker} if speaker else {}))
            span.detail = f"{len(text)} chars → {len(audio) // 1024} KB audio"
        return audio
    except Exception as e:
        # The span already wrote the traceback to logs/errors.log
        await broadcast({"type": "error", "detail": f"TTS error: {e}"})
        return None


def _finish(result: dict, tl: Timeline) -> dict:
    tl.mark("response sent", f"server total {tl.total_ms()} ms")
    if debug.enabled:
        result["timing"] = tl.events
    return result


def _save_recording(conversation_id: str, filename: str, data: bytes) -> str:
    """Write audio under recordings/<conversation>/ and return its path relative to recordings/."""
    folder = RECORDINGS_DIR / conversation_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)
    return f"{conversation_id}/{filename}"


def _compact_facts(facts: list[dict]) -> list[dict]:
    """What the agent's tools found this turn, trimmed for the next turn's context."""
    out = []
    for f in facts:
        result = json.dumps(f["result"], ensure_ascii=False, default=str)
        out.append({"tool": f["tool"], "args": f["args"],
                    "result": json.loads(result) if len(result) <= 600 else result[:600] + "…"})
    return out


async def _update_mood(conversation_id: str, merchant_id: str, allow_llm: bool = True):
    msgs = await asyncio.to_thread(conversations.messages, conversation_id)
    texts = [m["content"] for m in msgs if m["role"] == "merchant"]
    mood, reason = await rate_mood(texts, allow_llm=allow_llm)
    await asyncio.to_thread(conversations.set_mood, conversation_id, mood, reason)
    await broadcast({"type": "mood", "conversation_id": conversation_id, "merchant_id": merchant_id,
                     "mood": mood, "reason": reason})


async def _handle_turn(*, merchant_id: str, text: str, ticket_id: str, channel: str, tl: Timeline,
                       conversation_id: Optional[str] = None, language: str = "hi-IN", speak: bool = False,
                       merchant_audio: Optional[bytes] = None, audio_ext: str = "webm",
                       mood_llm: bool = True, profile=None, topic: Optional[str] = None,
                       speaker: Optional[str] = None) -> dict:
    """One merchant turn: continue (or start) the conversation, run the agent with memory of earlier
    turns, save both sides' transcript and recordings, and rate the merchant's mood in the background."""
    merchant_audio_url = agent_audio_url = None
    conv_id = await asyncio.to_thread(conversations.get_or_start, merchant_id, conversation_id, channel, topic)
    history = await asyncio.to_thread(conversations.history_for_llm, conv_id)
    merchant_msg = await asyncio.to_thread(conversations.add_message, conv_id, "merchant", text,
                                           ticket_id=ticket_id, channel=channel)
    if merchant_audio and settings.record_calls:
        path = _save_recording(conv_id, f"{merchant_msg}-merchant.{audio_ext}", merchant_audio)
        await asyncio.to_thread(conversations.set_audio, merchant_msg, path)
        merchant_audio_url = f"/recordings/{path}"
        tl.mark("recording saved", f"merchant audio → recordings/{path}")

    result = await resolver.resolve(ticket_id, merchant_id, text, language, timeline=tl,
                                    history=history, conversation_id=conv_id, profile=profile)
    facts = _compact_facts(result.pop("facts"))
    agent_msg = await asyncio.to_thread(conversations.add_message, conv_id, "agent", result["reply"],
                                        ticket_id=ticket_id, channel=channel, facts=facts)
    await asyncio.to_thread(conversations.set_status, conv_id, result["status"])

    if speak:
        audio = await _tts(result["reply"], language, tl, speaker)
        if audio:
            result["audio_b64"] = base64.b64encode(audio).decode()
            if settings.record_calls:
                path = _save_recording(conv_id, f"{agent_msg}-agent.wav", audio)
                await asyncio.to_thread(conversations.set_audio, agent_msg, path)
                agent_audio_url = f"/recordings/{path}"

    asyncio.create_task(_update_mood(conv_id, merchant_id, allow_llm=mood_llm))
    result.update(conversation_id=conv_id, merchant_audio_url=merchant_audio_url, agent_audio_url=agent_audio_url)
    return result


@app.post("/api/tickets")
async def create_ticket(ticket: TicketIn):
    """Resolve a text turn end to end (continues ticket.conversation_id if given)."""
    if not store.get_merchant(ticket.merchant_id):
        raise HTTPException(404, "Unknown merchant")
    ticket_id = ticket.ticket_id or f"T-{uuid.uuid4().hex[:6].upper()}"
    tl = Timeline(ticket_id, "text", broadcast)
    tl.mark("request received", f"text ticket, {len(ticket.text)} chars")
    result = await _handle_turn(merchant_id=ticket.merchant_id, text=ticket.text, ticket_id=ticket_id,
                                channel="text", tl=tl, conversation_id=ticket.conversation_id, speak=ticket.speak)
    return _finish(result, tl)


@app.post("/api/voice")
async def voice_ticket(merchant_id: str = Form(...), audio: UploadFile = File(...),
                       conversation_id: Optional[str] = Form(None), invoice_id: Optional[str] = Form(None)):
    """Voice turn: Sarvam STT -> agent -> Sarvam TTS; both sides recorded.
    With invoice_id, the speaker is merchant B on a collections call (merchant_id is ignored)."""
    if not stt:
        raise HTTPException(400, "Voice needs SARVAM_API_KEY (running in offline mode)")
    ticket_id = f"{'COL' if invoice_id else 'V'}-{uuid.uuid4().hex[:6].upper()}"
    tl = Timeline(ticket_id, "voice", broadcast)
    tl.mark("request received", "voice upload started")
    with tl.span("read upload") as span:
        audio_bytes = await audio.read()
        span.detail = f"{len(audio_bytes) // 1024} KB, {audio.content_type}"
    retry_hint = "Aawaz samajh nahi aayi. Mic dabaiye, boliye, phir dobara dabakar bhejiye."
    if len(audio_bytes) < 1000:
        tl.mark("rejected: recording too short", f"{len(audio_bytes)} bytes")
        log_error("voice: recording too short", detail=f"{len(audio_bytes)} bytes uploaded", trace_id=ticket_id)
        raise HTTPException(422, retry_hint)
    await broadcast({"type": "stt", "detail": f"Transcribing {len(audio_bytes)} bytes with Saaras"})
    try:
        with tl.span("speech-to-text (Saaras)") as span:
            transcript, language = await stt.transcribe(audio_bytes, audio.filename or "audio.webm")
            span.detail = f"{language}: {transcript}"
    except Exception:
        # The span already wrote the Sarvam error and traceback to logs/errors.log
        raise HTTPException(422, retry_hint)
    if not transcript:
        tl.mark("empty transcript", "nothing understood")
        log_error("voice: empty transcript", detail=f"{len(audio_bytes)} bytes uploaded", trace_id=ticket_id)
        raise HTTPException(422, retry_hint)
    audio_ext = Path(audio.filename or "a.webm").suffix.lstrip(".").lower() or "webm"
    if invoice_id:
        result = await _collections_turn(invoice_id, transcript, conversation_id, tl, ticket_id,
                                         language=language, merchant_audio=audio_bytes, audio_ext=audio_ext)
    else:
        result = await _handle_turn(merchant_id=merchant_id, text=transcript, ticket_id=ticket_id, channel="voice",
                                    tl=tl, conversation_id=conversation_id, language=language, speak=True,
                                    merchant_audio=audio_bytes, audio_ext=audio_ext)
    result["transcript"] = transcript
    return _finish(result, tl)


# ---------- conversations & customers ----------

@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    """Full transcript (merchant and AI), with recording URLs and the tools used on each AI turn."""
    with db.session():
        conv = conversations.get(conversation_id)
    if not conv:
        raise HTTPException(404, "Unknown conversation")
    for m in conv["messages"]:
        m["audio_url"] = f"/recordings/{m['audio_path']}" if m["audio_path"] else None
        m["facts"] = json.loads(m["facts"]) if m["facts"] else None
    recorded = sum(1 for m in conv["messages"] if m["audio_path"])
    # ?n= changes whenever a turn is added, so browsers don't replay an older, shorter version
    conv["call_recording_url"] = (f"/api/conversations/{conversation_id}/call-recording.mp3?n={recorded}"
                                  if recorded else None)
    conv["recorded_turns"] = recorded
    return conv


@app.get("/api/conversations/{conversation_id}/call-recording.mp3")
def call_recording(conversation_id: str):
    """The whole call as one MP3: every recorded turn (merchant and AI) in order, with short pauses."""
    with db.session():
        conv = conversations.get(conversation_id)
    if not conv:
        raise HTTPException(404, "Unknown conversation")
    clips = [RECORDINGS_DIR / m["audio_path"] for m in conv["messages"]
             if m["audio_path"] and (RECORDINGS_DIR / m["audio_path"]).exists()]
    if not clips:
        raise HTTPException(404, "No recorded turns in this conversation")
    try:
        path = call_recording_service.build(clips, RECORDINGS_DIR / conversation_id)
    except Exception as e:
        log_error("call recording", e, trace_id=conversation_id)
        raise HTTPException(500, "Could not build the call recording")
    return FileResponse(str(path), media_type="audio/mpeg",
                        filename=f"call-{conversation_id}.mp3", content_disposition_type="inline")


# ---------- collections: merchant A's AI calls merchant B about an unpaid invoice ----------

def _collections_profile(invoice_id: str):
    planner = ScriptedCollectionsPlanner(cstore, invoice_id) if settings.offline else None
    return collections_profile(cstore, invoice_id, llm=planner)


def _within_call_hours() -> bool:
    start, end = (int(x) for x in settings.collections_call_hours.split("-"))
    return start <= datetime.now().hour < end


async def _collections_turn(invoice_id: str, text: str, conversation_id: Optional[str], tl: Timeline,
                            ticket_id: str, language: str = "hi-IN", merchant_audio: Optional[bytes] = None,
                            audio_ext: str = "webm") -> dict:
    inv = await asyncio.to_thread(cstore.invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Unknown invoice")
    profile = await asyncio.to_thread(_collections_profile, invoice_id)
    result = await _handle_turn(merchant_id=inv["creditor_id"], text=text, ticket_id=ticket_id, channel="call",
                                tl=tl, conversation_id=conversation_id, language=language, speak=True,
                                merchant_audio=merchant_audio, audio_ext=audio_ext, profile=profile,
                                topic=f"collections:{invoice_id}", speaker=settings.collections_tts_speaker)
    result["invoice"] = await asyncio.to_thread(lambda: cstore.view(cstore.invoice(invoice_id)))
    await broadcast({"type": "invoice_updated", "invoice": result["invoice"]})
    return result


@app.get("/collections")
async def serve_collections():
    return FileResponse(str(STATIC_PATH / "collections.html"), media_type="text/html")


@app.get("/pay/{link_id}")
async def serve_pay_page(link_id: str):
    return FileResponse(str(STATIC_PATH / "pay.html"), media_type="text/html")


@app.get("/api/collections/invoices")
def list_invoices(creditor_id: Optional[str] = None):
    return cstore.list_invoices(creditor_id)


@app.get("/api/collections/summary")
def collections_summary(creditor_id: Optional[str] = None):
    return cstore.summary(creditor_id)


@app.post("/api/collections/{invoice_id}/call")
async def start_collections_call(invoice_id: str):
    """Start an AI call to merchant B: opening line (who is calling, recorded, is this B?), spoken and recorded."""
    inv = await asyncio.to_thread(cstore.invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Unknown invoice")
    if inv["status"] == "PAID":
        raise HTTPException(409, "This invoice is already paid")
    if not _within_call_hours():
        raise HTTPException(409, f"Outside calling hours ({settings.collections_call_hours}h). "
                                 "The AI only calls merchants during the day.")
    view = await asyncio.to_thread(cstore.view, inv)
    tl = Timeline(f"CALL-{invoice_id}", "call", broadcast)
    conv_id = await asyncio.to_thread(conversations.get_or_start, inv["creditor_id"], None, "call",
                                      f"collections:{invoice_id}")
    opening = greeting(view)
    msg_id = await asyncio.to_thread(conversations.add_message, conv_id, "agent", opening, channel="call")
    result = {"conversation_id": conv_id, "greeting": opening, "invoice": view, "agent_audio_url": None}
    audio = await _tts(opening, "hi-IN", tl, settings.collections_tts_speaker)
    if audio:
        result["audio_b64"] = base64.b64encode(audio).decode()
        if settings.record_calls:
            path = _save_recording(conv_id, f"{msg_id}-agent.wav", audio)
            await asyncio.to_thread(conversations.set_audio, msg_id, path)
            result["agent_audio_url"] = f"/recordings/{path}"
    await broadcast({"type": "call_started", "invoice": view, "conversation_id": conv_id})
    return result


class CallTurnIn(BaseModel):
    text: str
    conversation_id: str


@app.post("/api/collections/{invoice_id}/turn")
async def collections_turn(invoice_id: str, body: CallTurnIn):
    """Merchant B speaks (typed here; voice goes through /api/voice with invoice_id)."""
    ticket_id = f"COL-{uuid.uuid4().hex[:6].upper()}"
    tl = Timeline(ticket_id, "call", broadcast)
    tl.mark("request received", f"collections call turn, {len(body.text)} chars")
    result = await _collections_turn(invoice_id, body.text, body.conversation_id, tl, ticket_id)
    return _finish(result, tl)


@app.post("/api/collections/{invoice_id}/reminder")
async def draft_collections_reminder(invoice_id: str):
    """Draft a WhatsApp reminder in merchant A's name, with a fresh payment link. A approves before it's sent."""
    inv = await asyncio.to_thread(cstore.invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Unknown invoice")
    view = await asyncio.to_thread(cstore.view, inv)
    if view["status"] in ("PAID", "DISPUTED"):
        raise HTTPException(409, f"Invoice is {view['status'].lower()}; no reminder needed")
    link = await asyncio.to_thread(cstore.create_link, invoice_id, view["outstanding"])
    url = settings.public_base_url + link["url_path"]
    body, source = await draft_reminder(view, link, url)
    msg_id = await asyncio.to_thread(lambda: cstore.add_message(
        channel="whatsapp", to_id=view["debtor_id"], to_name=view["debtor"], to_contact=view["debtor_phone"],
        subject=f"Payment reminder {invoice_id}", body=body, invoice_id=invoice_id, status="DRAFT"))
    message = await asyncio.to_thread(cstore.message, msg_id)
    await broadcast({"type": "outbox", "message": message})
    return {**message, "drafted_by": source, "payment_link": url}


class SendIn(BaseModel):
    body: str
    approved_by: str = "merchant A"


@app.post("/api/outbox/{message_id}/send")
async def send_outbox_message(message_id: int, body: SendIn):
    """Merchant A approved the draft (possibly edited): send it. Simulated: marked SENT in the outbox."""
    msg = await asyncio.to_thread(cstore.message, message_id)
    if not msg or msg["status"] != "DRAFT":
        raise HTTPException(409, "Not a draft")
    await asyncio.to_thread(cstore.send_message, message_id, body.body, body.approved_by)
    message = await asyncio.to_thread(cstore.message, message_id)
    await broadcast({"type": "outbox", "message": message})
    return message


@app.get("/api/outbox")
def list_outbox():
    return cstore.outbox()


@app.get("/api/pay/{link_id}")
def get_payment_link(link_id: str):
    link = cstore.link(link_id)
    if not link:
        raise HTTPException(404, "Unknown payment link")
    return {"link": link, "invoice": cstore.view(cstore.invoice(link["invoice_id"]))}


@app.post("/api/pay/{link_id}")
async def pay_link(link_id: str):
    """Demo UPI payment by merchant B: applies the amount to the invoice and tells merchant A."""
    result = await asyncio.to_thread(cstore.pay, link_id)
    if not result["link"]:
        raise HTTPException(404, "Unknown payment link")
    if not result["already_paid"]:
        inv = result["invoice"]
        await asyncio.to_thread(lambda: cstore.notify_creditor(
            cstore.invoice(inv["invoice_id"]), f"Payment received: {inv['debtor']}",
            f"{inv['debtor']} paid Rs {result['link']['amount']} for {inv['invoice_id']} via UPI link {link_id}."))
        await broadcast({"type": "invoice_paid", "invoice": inv, "amount": result["link"]["amount"], "link_id": link_id})
    return result


@app.get("/api/customers")
def list_customers():
    return customers.summary()


@app.get("/api/customers/{merchant_id}")
def customer_overview(merchant_id: str):
    data = customers.overview(merchant_id)
    if not data:
        raise HTTPException(404, "Unknown merchant")
    return data


# ---------- detailed log switch ----------

class DebugIn(BaseModel):
    enabled: bool


@app.post("/api/debug")
async def set_debug(body: DebugIn):
    """Turn the detailed timing log on or off (server console + dashboard)."""
    debug.enabled = body.enabled
    line = f"detailed log {'ENABLED' if body.enabled else 'DISABLED'} from dashboard"
    print(f"[TIMING] {line}", flush=True)
    timing_log.info(line)
    return {"detailed_log": debug.enabled}


class ClientTiming(BaseModel):
    trace_id: str
    events: list[dict]


@app.post("/api/debug/client-timing")
async def client_timing(body: ClientTiming):
    """Browser-side steps (mic, upload, playback), printed next to the server steps."""
    for e in body.events:
        dur = f"{e['ms']:>6}ms" if e.get("ms") is not None else " " * 8
        log_timing_line(f"{body.trace_id} +{e['at_ms']:>6}ms {dur}  browser: {e['step']:<25} {e.get('detail', '')}")
    return {"ok": True}


class ClientError(BaseModel):
    where: str
    message: str
    stack: str = ""
    trace_id: str = ""


@app.post("/api/debug/client-error")
async def client_error(body: ClientError):
    """Browser errors (JS exceptions, failed requests, mic errors) land in logs/errors.log."""
    log_error(f"browser: {body.where}", detail=f"{body.message}\n{body.stack}".strip(), trace_id=body.trace_id)
    return {"ok": True}


@app.get("/api/tickets")
def list_tickets():
    return audit.list_tickets()


@app.get("/api/audit")
def list_audit(ticket_id: Optional[str] = None):
    return audit.list_audit(ticket_id)


@app.get("/api/metrics")
def metrics():
    return audit.metrics()


# ---------- human approval queue ----------

class Decision(BaseModel):
    decided_by: str = "ops@settlementresolver"


@app.get("/api/approvals")
def list_approvals(status: Optional[str] = None):
    return audit.list_approvals(status)


@app.post("/api/approvals/{approval_id}/approve")
async def approve(approval_id: int, decision: Decision):
    """Human approves an over-cap action; it is re-checked (cap waived, hard rules still apply) and run."""
    def run():
        with db.session():
            a = audit.get_approval(approval_id)
            if not a or a["status"] != "PENDING":
                return None, None, None
            args = json.loads(a["args"])
            gate = guardrails.check(store, a["merchant_id"], a["tool"], args, human_approved=True)
            if gate.decision != "ALLOW":
                audit.decide_approval(approval_id, "BLOCKED", decision.decided_by)
                audit.log(a["ticket_id"], "approval_blocked", tool=a["tool"], args=args,
                          reason="; ".join(gate.reasons), decision="BLOCK", actor=decision.decided_by)
                return a, gate, None
            result = execute_tool(store, a["merchant_id"], a["tool"], args)
            audit.decide_approval(approval_id, "APPROVED", decision.decided_by)
            return a, gate, result

    a, gate, result = await asyncio.to_thread(run)
    if not a:
        raise HTTPException(409, "Approval not pending")
    if result is None:
        raise HTTPException(409, f"Blocked on re-check: {gate.reasons}")
    args = json.loads(a["args"])
    audit.log(a["ticket_id"], "human_approved", tool=a["tool"], args=args, result=result,
              reason=a["agent_reason"], decision="ALLOW", actor=decision.decided_by)
    reply = (f"Aapke ₹{result['amount']:,.0f} ka instant credit approve ho gaya hai, credit id {result['credit_id']}. "
             f"Paisa {result['to_bank']} account me aa gaya hai.")
    audit.close_ticket(a["ticket_id"], "RESOLVED", reply=reply, recovered=result.get("amount", 0), human=True)
    await broadcast({"type": "approval_decided", "approval_id": approval_id, "status": "APPROVED",
                     "ticket_id": a["ticket_id"], "tool": a["tool"], "system": TOOL_SYSTEM[a["tool"]],
                     "result": result, "reply": reply})
    await broadcast({"type": "metrics", "metrics": await asyncio.to_thread(audit.metrics)})
    return {"status": "APPROVED", "result": result, "reply": reply}


@app.post("/api/approvals/{approval_id}/reject")
async def reject(approval_id: int, decision: Decision):
    a = await asyncio.to_thread(audit.get_approval, approval_id)
    if not a or a["status"] != "PENDING":
        raise HTTPException(409, "Approval not pending")
    await asyncio.to_thread(audit.decide_approval, approval_id, "REJECTED", decision.decided_by)
    audit.log(a["ticket_id"], "human_rejected", tool=a["tool"], args=json.loads(a["args"]),
              decision="BLOCK", actor=decision.decided_by)
    audit.close_ticket(a["ticket_id"], "ESCALATED", human=True)
    await broadcast({"type": "approval_decided", "approval_id": approval_id, "status": "REJECTED",
                     "ticket_id": a["ticket_id"]})
    await broadcast({"type": "metrics", "metrics": await asyncio.to_thread(audit.metrics)})
    return {"status": "REJECTED"}


# ---------- batch run & reset ----------

async def _run_batch():
    # Always measure on fresh data: re-running already-fixed tickets skews every number
    await asyncio.to_thread(store.reset)
    await asyncio.to_thread(audit.reset)
    await broadcast({"type": "reset", "metrics": await asyncio.to_thread(audit.metrics)})
    batch = await asyncio.to_thread(store.list_seed_tickets)  # 6 demo scenarios + 44 generated tickets
    await broadcast({"type": "batch_start", "tickets": [t["ticket_id"] for t in batch]})
    for t in batch:
        tl = Timeline(t["ticket_id"], "batch", broadcast)
        # Each batch ticket is its own conversation; mood uses the keyword rating to save 50 AI calls
        await _handle_turn(merchant_id=t["merchant_id"], text=t["text"], ticket_id=t["ticket_id"],
                           channel="text", tl=tl, mood_llm=False)
    await broadcast({"type": "batch_done", "metrics": await asyncio.to_thread(audit.metrics)})


@app.post("/api/batch")
async def run_batch():
    """Run the generated tickets through the agent in the background."""
    global batch_task
    if batch_task and not batch_task.done():
        raise HTTPException(409, "Batch already running")
    batch_task = asyncio.create_task(_run_batch())
    return {"status": "started"}


@app.post("/api/reset")
async def reset():
    """Reset mock systems and the audit DB for a fresh demo run."""
    if batch_task and not batch_task.done():
        batch_task.cancel()
    await asyncio.to_thread(store.reset)
    await asyncio.to_thread(audit.reset)
    await asyncio.to_thread(_clear_recordings)
    await broadcast({"type": "reset", "metrics": await asyncio.to_thread(audit.metrics)})
    return {"status": "reset"}


def _clear_recordings():
    for folder in RECORDINGS_DIR.iterdir():
        if folder.is_dir():
            shutil.rmtree(folder)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
