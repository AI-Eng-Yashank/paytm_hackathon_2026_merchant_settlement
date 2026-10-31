# SettlementResolver

An autonomous AI teammate that resolves merchant settlement disputes in Hinglish, with guardrails, a human approval queue and a full audit trail. Paytm Build for India AI Hackathon, Track 3.

Built on [voice-ai-receptionist](https://github.com/AI-Eng-Yashank/voice-ai-receptionist) (FastAPI server, event broadcast, tool loop), with Sarvam replacing Groq/Deepgram. See [RESOURCES.md](RESOURCES.md) for every repo used.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add SARVAM_API_KEY, or leave empty for offline mode
.venv/bin/python -m uvicorn main:app --port 8000
```

Open http://localhost:8000.

- **With `SARVAM_API_KEY`:** Sarvam-105B makes the decisions, Saaras transcribes voice, Bulbul speaks replies.
- **Without it:** a deterministic scripted planner runs the same playbook. Use it to rehearse, or as a fallback if the venue Wi-Fi fails.

Regenerate the mock data with `.venv/bin/python -m mock_systems.seed`.

## Database (Neon Postgres, SQLite fallback)

Set `DATABASE_URL` in `.env` to your Neon connection string (Neon console → **Connect**). Leave it empty to use local SQLite (`data/local.db`). If Neon is set but unreachable at startup, the app falls back to SQLite and logs why in `logs/errors.log`. The header badge shows which one is live.

| Tables | What |
|---|---|
| `merchants`, `transactions`, `settlements`, `bank_records`, `credits`, `seed_tickets` | The five mock systems and the 50 test tickets, seeded from `data/seed.json` |
| `tickets`, `audit_log`, `approvals` | What the agent did, why, and what humans approved |
| `conversations`, `messages` | Multi-turn memory (tables ready; wiring is the next fix) |
| `meta` | The day the mock data was seeded |

Tables are created on startup. Mock data is reseeded on **Reset demo** and automatically on a new day, so "kal" and RBI TAT ages in the demo scenarios stay correct.

**Speed:** every query is one network round trip, so the Neon region matters. Use **AWS Singapore** (`aws-ap-southeast-1`); Neon has no India region. The code keeps round trips low: autocommit (no BEGIN/ROLLBACK), one connection per tool step, merchants cached in memory, the credit guardrail reads everything in one query, and audit writes go through a background writer in batches, so the merchant's reply never waits for them.

## How it works

```
ticket (text or voice) -> Sarvam-105B proposes a tool call
   -> guardrails.check()  ALLOW -> run on mock system
                          NEEDS_APPROVAL -> human approval queue
                          BLOCK -> never runs, ticket escalated
   -> result back to the model -> ... -> Hinglish reply (+ Bulbul audio)
every step -> audit log (SQLite) + live dashboard (WebSocket)
```

| Path | What |
|---|---|
| `main.py` | FastAPI app: tickets, voice, approvals, batch run, reset, `/ws/events` |
| `agent/resolver.py` | Agent loop: LLM → gate → execute / queue / block → reply |
| `agent/tools.py` | 9 tools across 5 systems; `merchant_id` injected from the ticket, never from the model |
| `agent/guardrails.py` | Credit caps, KYC, account age, risk, repeat-claim and RBI TAT rules |
| `agent/audit.py` | SQLite: tickets, audit log, approvals, metrics |
| `agent/prompts.py` | System prompt |
| `mock_systems/` | Payments, ledger, bank/NPCI, credits, CRM + seed generator (50 tickets) |
| `services/` | Sarvam STT / LLM / TTS, and the offline scripted planner |
| `static/` | Dashboard: Tabler (CDN) + anime.js 4.5.0 |

## Demo scenarios (quick-pick buttons)

| Ticket | Path |
|---|---|
| Failed settlement | `retry_settlement` → resolved |
| Duplicate debit | `reverse_duplicate_debit` → resolved |
| Pending but credited | bank reconciliation shows credited → resolved |
| ₹25,000 stuck 7 days | TAT breached, over ₹10,000 cap → human approval |
| Repeat credit claim | already credited, new account, high risk → blocked, escalated |
| No txn id given | `find_txn_by_amount_time` first |

**Run 50-ticket batch** fills the grid and the KPI tiles.

### Measured with live Sarvam-105B (31 Oct 2026, reasoning off)

| Metric | Result |
|---|---|
| Correct path chosen | **50 / 50 tickets** |
| Resolved with no human | **84%** (42/50), which is every routine ticket in the set |
| Sent to human approval | 5 (all over the ₹10,000 cap) |
| Blocked and escalated | 3 (all repeat claims) |
| Money recovered | ₹1,05,909 |
| Time per ticket | median 3.3s, slowest 7.5s |
| Steps per ticket | 3.3 tool calls |
| AI errors | 0 |

With reasoning set to `low`, the same tickets took 11–36s, so it is off by default.

Re-run on **Neon Postgres (AWS Singapore)** the same day: again **50/50 correct path**, 84% auto-resolved, ₹1,05,909 recovered, median **3.9s** per ticket (slowest 8.6s), full batch in 3.6 minutes. Neon adds about 0.5s per ticket over local SQLite (65ms per round trip).

## Guardrail rules (config.py)

- Auto-credit cap ₹10,000 per transaction and ₹20,000 per merchant per day. Above the cap, a human approves.
- Instant credit only if: payment SUCCESS, settlement still open, bank shows NOT_FOUND, RBI TAT (T+5 for merchant payments) breached, and the amount equals the transaction amount.
- Blocked: repeat credit for the same transaction, KYC not FULL, account under 30 days old, risk score over 0.7.
- RBI circular RBI/2019-20/67: ₹100/day compensation beyond TAT is shown to the merchant.

## Conversations, recordings and customer health

- **Memory:** each merchant session is one conversation (badge in the chat header; **New** starts another, and 30 idle minutes start a fresh one). The agent sees earlier turns plus a note of what its tools found, so "मिले क्या?" is answered from what was already looked up.
- **No false promises:** the agent must do the lookup now or ask one question; it never says "ek minute rukiye".
- **Honest status:** a turn that only asks a question is `NEEDS_INFO`, not `RESOLVED`.
- **Recording + transcript:** every turn stores what the merchant and the AI said. Voice turns also save both audio clips under `recordings/<conversation>/` (served at `/recordings/...`, cleared on Reset, `RECORD_CALLS=false` to turn off). The dashboard shows a recording notice.
- **Mood:** after each turn, Sarvam rates the conversation calm / worried / angry with a reason (keyword fallback offline and for the batch).
- **Customers page** (`/customers`): every merchant sorted by who needs attention, and for each one a 0–100 health score with every deduction explained, money (recovered, credited, waiting for approval, RBI compensation due), tickets timeline, and conversations with full transcript and audio playback.

## MCP server (ops tools for AI assistants)

`mcp_server.py` lets Claude Desktop, Claude Code or Cursor work with SettlementResolver. Every tool calls the running app, so the same guardrails, audit log and live dashboard apply. Start the app first.

| Tool | What it does |
|---|---|
| `find_merchants`, `get_merchant` | Look up a shop by name, city or id |
| `check_transaction` | Payment, duplicate check, settlement, UTR, RBI TAT (read-only) |
| `resolve_ticket` | Run the full agent on a complaint (Hinglish/Hindi/English) |
| `list_pending_approvals` | Credits waiting for a human, with the guardrail's reasons |
| `approve_credit`, `reject_credit` | Human decision (marked destructive, so clients ask before running) |
| `get_ticket_audit`, `recent_tickets`, `get_metrics` | Audit trail and headline numbers |

**Claude Code:** `.mcp.json` in this folder registers it; approve it when Claude Code asks.

**Claude Desktop:** add this to `~/Library/Application Support/Claude/claude_desktop_config.json` and restart Claude:

```json
{
  "mcpServers": {
    "settlementresolver": {
      "command": "/Users/mhatreay/paytm_project/.venv/bin/python",
      "args": ["/Users/mhatreay/paytm_project/mcp_server.py"]
    }
  }
}
```

Try: *"Which credits are waiting for approval, and why?"* or *"Mehta Electronics says their ₹25,000 settlement is stuck. Check it."*

## Detailed log

Turn on **Detailed log** in the top bar (default on; `DETAILED_LOG=false` in `.env` to start off). Every request then gets a timeline:

- **Browser:** button pressed, mic permission, mic ready, recording started, released, upload, response, audio playing, audio finished.
- **Server:** upload read, speech-to-text, each AI call (with what it decided), each tool (with its result), guardrails, text-to-speech.

It shows in the dashboard panel (slow steps highlighted, **Copy** button) and in the server console as `[TIMING]` lines. Switching it off stops both.

### Log files (for debugging)

| File | What | When |
|---|---|---|
| `logs/timing.log` | Every step of every request, browser and server, with full tool results | While the detailed log is on |
| `logs/errors.log` | Server exceptions (with traceback and Sarvam's error message), browser JS errors, failed requests, mic errors (with the browser timeline attached) | Always |

Each server start writes a header line with the mode, models and settings. Files rotate at 2 MB (5 kept) and are git-ignored. When something goes wrong, check `logs/errors.log` first, then find the same trace id (e.g. `V-C6E0A3`) in `logs/timing.log`.

## Known limits

- Spoken transaction ids transcribe badly ("TXN240001" became "टीएक्सएन24001"). The prompt tells the agent to fall back to amount and time, which is how merchants talk anyway.
- Mic: **tap to start, tap again to send** (holding for over 0.5s and releasing also sends). Recordings under 0.5s or 1 KB are not sent. Tested with real Bulbul speech fed in as a fake mic; still worth one try with your own voice.
- The approval queue is a SQLite table, not LangGraph `interrupt()`; this is simpler and enough for the demo.
