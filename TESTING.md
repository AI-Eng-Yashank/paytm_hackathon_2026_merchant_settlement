# Testing SettlementResolver

## Run the tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

**100 automated tests, about 20 seconds.** They run on a throwaway SQLite database and log folder with the offline planner, so they never touch Neon, Sarvam, your `.env` keys or `logs/`.

Before the demo, at the venue, also run the **live checks** (real Neon + Sarvam, read-only, a few small API calls):

```bash
RUN_LIVE=1 .venv/bin/python -m pytest -m live -v
```

## What the automated tests cover

### Mock systems (`tests/test_store.py`, 12 tests)
| Test | Checks |
|---|---|
| Seed is loaded | 49 merchants, 50 tickets, 6 demo scenarios in the database |
| Own transaction | Payment status returns amount and status |
| **Merchant isolation** | M1002 can't see M1001's transaction (TXN_NOT_FOUND) |
| Txn id format | `  txn240001 ` is treated as `TXN240001` |
| Duplicate detection | Found from either half of the pair |
| Find by amount + day | "₹4,500 yesterday" finds TXN240001; wrong day finds nothing |
| **RBI TAT** | 7-day-old settlement: breached, ₹200 compensation; 2-day-old: not breached |
| Retry settlement | Becomes SETTLED with a new UTR, bank shows CREDITED |
| Bank reconciliation | Credited UTR marks settlement SETTLED; unknown or other merchant's UTR is NOT_FOUND |
| Writes | Reversal and instant credit are stored; credit amounts are whole rupees |
| Reset | Restores the seed state |

### Guardrails (`tests/test_guardrails.py`, 12 tests)
| Test | Checks |
|---|---|
| Read-only tools | Always allowed |
| Other merchant's txn | Any action is blocked |
| Retry | Only FAILED settlements |
| **Double refund** | Second reversal of the same duplicate is blocked |
| No duplicate | Reversal without a duplicate is blocked |
| **Over-cap credit** | ₹25,000 needs human approval |
| **Human approval** | Waives the cap only; a wrong amount is still blocked |
| Small credit | Under ₹10,000 and past T+5 runs automatically |
| Before T+5 | Credit blocked (and blocked if bank already credited) |
| **Repeat claim / risky merchant** | Blocked, with every reason listed (repeat, KYC, account age, risk) |
| Wrong amount | Blocked |
| Daily cap | Earlier credits count |

### Agent end to end (`tests/test_agent.py`, 14 tests)
| Test | Checks |
|---|---|
| 6 demo scenarios | Each takes the right path, and the reply has the amount / reference |
| **Audit trail** | Every tool call logged in order, with reason and guardrail decision |
| Blocked credit | Never reaches the credits table |
| Approve | Credit issued, ticket RESOLVED, can't approve twice |
| Reject | No credit, ticket ESCALATED |
| **No duplicate approvals** | Repeat ticket for the same txn reuses the pending approval |
| Metrics | 6 demos: 4 auto, 1 escalated, 1 awaiting, ₹5,700 recovered |
| **50-ticket batch** | All 50 take the expected path; 84% auto, 5 approvals, 3 escalated |
| **Batch resets first** | Running the batch after other tickets still measures fresh data |

### API, voice and logs (`tests/test_api.py`, 10 tests)
| Test | Checks |
|---|---|
| Health | Mode, database, log switch reported |
| Validation | Unknown merchant is 404; voice without Sarvam is 400 |
| **Short recording** | Under 1 KB rejected with the Hinglish hint, Sarvam never called |
| **Speech-to-text failure** | Friendly message to the merchant; Sarvam's real error in `errors.log` |
| Voice happy path | Transcript runs through the agent; timing has every step |
| **Log switch** | Off: no timing in response or file. On: full timeline in both |
| Browser logs | Browser timing and errors land in the backend logs |
| **Unexpected error** | User sees a generic message; `errors.log` has the traceback |
| Reset | Clears tickets and audit |

### Infrastructure (`tests/test_infra.py`, 9 tests)
| Test | Checks |
|---|---|
| Neon URL | Converted for the psycopg driver |
| **Neon down** | Falls back to SQLite and logs why |
| New day | Mock data reseeded so "kal" and TAT ages stay right |
| Shared connection | One connection per tool step |
| **Audit writer** | 25 queued writes kept in order, none lost; reads see queued writes |
| **Sarvam timeout** | One retry after a timeout; gives up after that; bad requests aren't retried |

### Conversations, recordings, mood, customers (`tests/test_conversations.py`, 21 tests)
| Test | Checks |
|---|---|
| **Memory** | Turns continue one conversation; agent sees earlier turns + facts note; facts note never reaches the merchant |
| Isolation / idle | Another merchant's conversation id isn't reused; 30 idle minutes start a new one |
| **Honest status** | A question without a lookup is NEEDS_INFO; a follow-up answered from earlier facts is RESOLVED |
| Recent transactions | Newest first, settlement status included, capped at 20 |
| **Recordings** | Voice turn saves merchant + AI audio, served back; Reset deletes them; path traversal blocked; can be switched off |
| Mood | Keyword rating (calm / worried / angry), saved on the conversation |
| **Health score** | Every deduction listed; DEMO-4 shows approval + RBI compensation; one conversation = one contact |
| Customer list | Contacted and least healthy first |
| Batch | One conversation per ticket |
| **Full call recording** | One MP3 with both sides in order; rebuilt (longer) after another turn; none for text-only conversations |

### Fact-check (`tests/test_factcheck.py`, 12 tests)
| Test | Checks |
|---|---|
| Correct replies pass | Real UTR + amount; "credited" when the bank confirmed it; future tense ("aa jayega") |
| Merchant's own amounts | ₹500 / ₹1,500 the merchant mentioned can be repeated; RBI ₹100/day always allowed |
| **Overclaim** | "paisa aa gaya" / "has been credited" / "आ गया" without bank confirmation is rejected |
| **Invented data** | Unknown UTR or amount is rejected |
| **Action claims** | "wapas bhej diya" / "credit kar diya" / "retry kar diya" need that action to have run |
| Internal details | JSON, approval ids, risk score rejected |
| **In the agent** | Bad draft rewritten (audit: FAIL then PASS); still bad after rewrite: safe reply + escalated |
| Demo scenarios | All six pass the fact-check first time |

### MCP server (`tests/test_mcp.py`, 9 tests)
| Test | Checks |
|---|---|
| Tools registered | All 10 tools, approve/reject marked destructive, reads marked read-only |
| Lookups | find/get merchant, 404 for unknown |
| Transaction check | Read-only and scoped to the merchant |
| resolve_ticket | Runs the real agent; audit shows the retry |
| Approvals | List, approve (credit issued, no double approve), reject |
| Metrics / recent tickets | Match the API |
| API down | Clear "API is not running" message |

### Live checks (`tests/test_live.py`, 3 tests, `RUN_LIVE=1`)
| Test | Checks |
|---|---|
| Neon | URL is Singapore, median round trip under 200ms, data present |
| Sarvam tool calling | Hinglish ticket → calls the right tool with TXN240001 |
| Sarvam voice | Bulbul speaks, Saaras transcribes it back |

## Manual checks (things code can't test)

Do these in the browser at http://localhost:8000 before recording the demo video.

| # | Step | Expected |
|---|---|---|
| 1 | Open the dashboard | Badges: **Sarvam live** and **Neon Postgres** (green) |
| 2 | Click **Reset demo** | KPIs back to 0, chat and trace empty |
| 3 | Click each of the 6 scenario buttons | Same outcomes as the table above; system map animates; trace fills |
| 4 | DEMO-4, then **Approve** in the queue | Card slides out, Credits turns green, merchant gets credit id |
| 5 | Turn on **Speak replies**, send a ticket | Reply is spoken in Hinglish |
| 6 | **Tap** mic, speak 3–5s, **tap** again | "Listening 0:0x" timer, then "Sending…", transcript + reply |
| 7 | **Hold** mic while speaking, release | Same as above |
| 8 | Tap mic twice quickly | "Didn't catch that…", nothing sent |
| 9 | **Run 50-ticket batch** | Grid fills; ends at 84% auto-resolved, ~₹1,05,909 |
| 10 | Turn **Detailed log** on/off | Panel appears/disappears; `logs/timing.log` stops/starts |
| 11 | Unplug Wi-Fi, restart server | Badge shows **Local SQLite**, app still works (offline mode needs `SARVAM_API_KEY` empty) |
| 12 | Check `logs/errors.log` | No unexpected `[ERROR]` lines from the run |
