# SettlementResolver — Essential Repos & References

Stack decision: **Python (reuse our repos) + Sarvam API + Tabler UI + anime.js**.

## 1. Reuse directly (copy code)

| Repo | License | What we take |
|---|---|---|
| [AI-Eng-Yashank/voice-ai-receptionist](https://github.com/AI-Eng-Yashank/voice-ai-receptionist) | ours | **Base app.** FastAPI + WebSocket, tool-call loop (`process_llm_response`), live monitor page (`static/visualization.html`, `broadcast_status`). |
| [sarvamai/sarvam-ai-cookbook](https://github.com/sarvamai/sarvam-ai-cookbook) → `examples/Indic Soundbox AI` | Apache-2.0 | `modules/asr.py`, `lid.py`, `llm.py`, `tts.py` — drop-in Sarvam replacements for the Groq/Deepgram services. Bump TTS to `bulbul:v4-flash`. Also `getting-started/chat completion` for tool calling. |
| [AI-Eng-Yashank/Logic_flow](https://github.com/AI-Eng-Yashank/Logic_flow) | ours | `evaluator.py` as a groundedness check. Fix: `schemas.py` / `schemas_dag.py` are missing (only `.pyc` committed); make parse failure = escalate, not pass. |
| [tabler/tabler](https://github.com/tabler/tabler) | MIT | UI via CDN, no build. Pages: `dashboard-crm` (KPIs + tickets), `chat`, `activity` (audit trail), `tasks` (approval queue). Pin `@tabler/core@1.6.1`. |
| [juliangarnier/anime](https://github.com/juliangarnier/anime) | MIT | Animations via CDN, pin `animejs@4.5.0`. v4 API (`animate`, `createTimeline`, `stagger`, `svg`) — v3 tutorials won't work. |

### Backend: borrow from agent-service-toolkit

[JoshuaC215/agent-service-toolkit](https://github.com/JoshuaC215/agent-service-toolkit) (MIT, ~4.5k stars, active) — FastAPI + LangGraph agent service. We keep voice-ai-receptionist as the base and lift these files instead of switching frameworks:

| File | Use |
|---|---|
| `src/agents/interrupt_agent.py` | LangGraph `interrupt()` example → our human-approval node before `issue_instant_credit`. |
| `src/service/service.py` (~L179–189) | Resume an interrupted run with `Command(resume=...)` → the "Approve" button endpoint. |
| `src/memory/sqlite.py` | `AsyncSqliteSaver` checkpointer → approvals survive a server restart. |
| `src/agents/safeguard.py` | Prompt-injection check on incoming tickets (swap its Groq model for Sarvam). |
| `src/core/llm.py` + `settings.py` | `COMPATIBLE_BASE_URL` / `COMPATIBLE_MODEL` pattern; for Sarvam prefer `langchain-sarvam`. |

Skip its Streamlit UI (we use Tabler) and Postgres/Mongo backends.

## 2. Python packages

```
pip install sarvamai langchain-sarvam langgraph langgraph-checkpoint-sqlite faker fastapi uvicorn python-dotenv
```

- `langchain-sarvam` (MIT) — LangChain chat model for Sarvam, so LangGraph nodes can call it directly. Verify tool binding works with a quick test.

- `sarvamai` — official SDK. Auth header is `api-subscription-key` (not Bearer). Models: `sarvam-105b`, `sarvam-105b-conversations` (`sarvam-30b` is deprecated). `content` can be `None` if reasoning eats `max_tokens`.
- `langgraph` — `interrupt()` for the human-approval queue (put the interrupt in its own node *before* the credit action).
- `faker` with `Faker("en_IN")` — seed merchants, txns, UTRs.

## 3. Dev aids (install once)

- [sarvamai/skills](https://github.com/sarvamai/skills) (Apache-2.0) — correct Sarvam SDK usage for coding agents: `npx skills add sarvamai/skills --skill chat` (+ `speech-to-text`, `text-to-speech`).
- [sarvamai/sarvam-mcp](https://github.com/sarvamai/sarvam-mcp) (MIT, optional) — try Sarvam calls from chat.

## 4. Read for ideas only (no license — don't copy code)

- [Bessiedelight/customer-service-agent](https://github.com/Bessiedelight/customer-service-agent) — extract → **gate** (pure Python rules) → correct → act. Our guardrail pattern.
- [Santhu-AIWeB/payment-reconciliation-system](https://github.com/Santhu-AIWeB/payment-reconciliation-system) — `gateway_server/app.py`, `bank_server/app.py`, `routers/audit.py`: failure scenarios for our mock APIs.
- [mansik-11/UPI-Payment-Reconciliation-Pipeline](https://github.com/mansik-11/UPI-Payment-Reconciliation-Pipeline) — `generator/generate_data.py` + `config.yaml`: mismatch types for seed data.
- [openai/openai-cs-agents-demo](https://github.com/openai/openai-cs-agents-demo) (MIT) — UI inspiration for showing agent orchestration. Too OpenAI-coupled to reuse.

## 5. Domain rules (use in guardrails + demo narration)

RBI circular RBI/2019-20/67 (20 Sep 2019) — TAT for failed transactions ([RBI](https://rbi.org.in/commonman/English/Scripts/Notification.aspx?Id=3074)):
- UPI P2P debited but not credited → auto-reversal by **T+1**, else **₹100/day** compensation.
- UPI merchant payment not confirmed to merchant → auto-reversal by **T+5**, else **₹100/day**.

## 6. Optional data

- [karanverma19/CodeMix_Query_Normalization_India](https://huggingface.co/datasets/karanverma19/CodeMix_Query_Normalization_India) (Apache-2.0, 100 samples) — real Hinglish phrasing incl. banking, to style test tickets.

## Skipped (and why)

- Pipecat / LiveKit Sarvam plugins — only needed for real phone calls.
- SettleFlow — GitHub repo not found, no license listed.
- Other UPI mock repos — 0 stars, no license; our mocks are ~150 lines.
- React/Tailwind dashboard templates — need a build step; Tabler via CDN is faster.
- [wassim249/fastapi-langgraph-agent-production-ready-template](https://github.com/wassim249/fastapi-langgraph-agent-production-ready-template) (MIT) — good, but Postgres + Valkey + Grafana stack is too heavy for a one-day build.
- Switching the whole base to agent-service-toolkit — more to learn than to gain; take its files instead.

## Still to write ourselves

Mock payment/ledger/bank APIs + seed data · 5 tools · UPI guardrail rules · audit log (SQLite) · approval queue · animations · 50-ticket batch runner.
