"""
SettlementResolver MCP server: ops tools for AI assistants (Claude Desktop, Claude Code, Cursor).

Every tool calls the running SettlementResolver API, so the same guardrails, audit log and live
dashboard apply as for merchant tickets. Start the app first (uvicorn main:app), then point your
MCP client at this file:

    .venv/bin/python mcp_server.py            # stdio transport

SR_API_URL overrides the API address (default http://127.0.0.1:8000).
"""

import json
import logging
import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


API_URL = os.environ.get("SR_API_URL", "http://127.0.0.1:8000")
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep per-request lines out of the client's MCP log

server = MCPServer(
    name="settlementresolver",
    title="SettlementResolver",
    instructions=(
        "Ops tools for SettlementResolver, an AI teammate that resolves Paytm merchant settlement disputes. "
        "Use find_merchants to get a merchant_id, then check_transaction or resolve_ticket. "
        "resolve_ticket runs the full agent (Hinglish in, Hinglish reply out); money actions in it are "
        "decided by code guardrails, not by you. Credits over the cap wait in list_pending_approvals; "
        "approve_credit / reject_credit are human decisions, so confirm with the user before calling them."
    ),
)

# Swapped for a test client in tests/test_mcp.py
http: httpx.Client = httpx.Client(base_url=API_URL, timeout=90)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _call(method: str, path: str, **kwargs):
    # ToolError messages reach the assistant; any other exception is reported as a crash
    try:
        r = http.request(method, path, **kwargs)
    except httpx.ConnectError:
        raise ToolError(f"SettlementResolver API is not running at {API_URL}. Start it with: "
                        f".venv/bin/python -m uvicorn main:app --port 8000")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise ToolError(f"{r.status_code}: {detail}")
    return r.json()


@server.tool(annotations=READ_ONLY)
def find_merchants(query: str = "", limit: int = 20) -> list[dict]:
    """Find merchants by id, shop name, business type or city (case-insensitive). Empty query lists all."""
    q = query.strip().lower()
    rows = [m for m in _call("GET", "/api/merchants")
            if not q or any(q in str(m.get(k, "")).lower() for k in ("merchant_id", "name", "business", "city"))]
    return [{k: m[k] for k in ("merchant_id", "name", "business", "city", "kyc_tier", "settlement_bank")}
            for m in rows[:limit]]


@server.tool(annotations=READ_ONLY)
def get_merchant(merchant_id: str) -> dict:
    """Merchant profile: KYC tier, account age, risk score, settlement bank and cycle, tickets in last 30 days."""
    return _call("GET", f"/api/merchants/{merchant_id}")


@server.tool(annotations=READ_ONLY)
def check_transaction(merchant_id: str, txn_id: str) -> dict:
    """Payment status, duplicate-debit check, settlement status, UTR, RBI TAT breach and compensation due."""
    return _call("GET", f"/api/transactions/{txn_id}", params={"merchant_id": merchant_id})


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
def resolve_ticket(merchant_id: str, message: str) -> dict:
    """Run the SettlementResolver agent on a merchant's complaint (Hinglish, Hindi or English).

    The agent checks payments, ledger and bank, and may retry a settlement, reverse a duplicate
    debit or issue an instant credit. Guardrails decide every money action; credits over the cap
    are queued for human approval instead of being paid. Returns status, reply to merchant and steps.
    """
    r = _call("POST", "/api/tickets", json={"merchant_id": merchant_id, "text": message})
    return {k: r[k] for k in ("ticket_id", "status", "reply", "steps", "duration_ms")}


@server.tool(annotations=READ_ONLY)
def list_pending_approvals() -> list[dict]:
    """Credits waiting for a human decision, with the agent's reason and the guardrail's reasons."""
    out = []
    for a in _call("GET", "/api/approvals", params={"status": "PENDING"}):
        args = json.loads(a["args"])
        out.append({"approval_id": a["id"], "ticket_id": a["ticket_id"], "merchant_id": a["merchant_id"],
                    "txn_id": args.get("txn_id"), "amount": args.get("amount"),
                    "agent_reason": a["agent_reason"], "why_approval_needed": json.loads(a["reasons"])})
    return out


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
def approve_credit(approval_id: int, approved_by: str) -> dict:
    """Approve a pending instant credit (moves money). Hard guardrails are re-checked before paying.
    Only call this after the user has explicitly said to approve this approval_id."""
    r = _call("POST", f"/api/approvals/{approval_id}/approve", json={"decided_by": approved_by})
    return {"status": r["status"], "credit": r["result"], "reply_to_merchant": r["reply"]}


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
def reject_credit(approval_id: int, rejected_by: str) -> dict:
    """Reject a pending instant credit; the ticket is escalated. Only call after the user says to reject it."""
    return _call("POST", f"/api/approvals/{approval_id}/reject", json={"decided_by": rejected_by})


@server.tool(annotations=READ_ONLY)
def get_ticket_audit(ticket_id: str) -> list[dict]:
    """Audit trail for one ticket: every step, tool, guardrail decision and reason, in order."""
    return [{"actor": a["actor"], "event": a["event"], "tool": a["tool"], "decision": a["decision"],
             "reason": a["reason"], "result": json.loads(a["result"] or "{}")}
            for a in _call("GET", "/api/audit", params={"ticket_id": ticket_id})]


@server.tool(annotations=READ_ONLY)
def recent_tickets(limit: int = 10) -> list[dict]:
    """Most recent tickets with status, reply, steps and whether a human was involved."""
    return [{k: t[k] for k in ("ticket_id", "merchant_id", "status", "text", "reply", "steps", "human_involved")}
            for t in _call("GET", "/api/tickets")[:limit]]


@server.tool(annotations=READ_ONLY)
def get_customer_overview(merchant_id: str) -> dict:
    """How a merchant is doing: health score with the reasons for each deduction, mood, money recovered /
    credited / waiting / RBI compensation due, ticket counts and their conversations."""
    d = _call("GET", f"/api/customers/{merchant_id}")
    return {"merchant": {k: d["merchant"][k] for k in ("merchant_id", "name", "city", "kyc_tier", "risk_score")},
            "health": d["health"], "mood": d["mood"], "money": d["money"], "counts": d["counts"],
            "pending_approvals": d["pending_approvals"],
            "conversations": [{k: c[k] for k in ("conversation_id", "status", "mood", "turns", "channel")}
                              for c in d["conversations"]]}


@server.tool(annotations=READ_ONLY)
def get_conversation_transcript(conversation_id: str) -> dict:
    """Full transcript of a conversation: what the merchant and the AI said, in order, with the tools the AI
    used on each turn and links to call recordings."""
    c = _call("GET", f"/api/conversations/{conversation_id}")
    return {"conversation_id": c["conversation_id"], "merchant_id": c["merchant_id"], "mood": c["mood"],
            "mood_reason": c["mood_reason"],
            "turns": [{"speaker": "merchant" if m["role"] == "merchant" else "AI", "text": m["content"],
                       "channel": m["channel"], "ticket_id": m["ticket_id"],
                       "recording": f"{API_URL}{m['audio_url']}" if m["audio_url"] else None,
                       "tools_used": [f["tool"] for f in (m["facts"] or [])]} for m in c["messages"]]}


@server.tool(annotations=READ_ONLY)
def get_metrics() -> dict:
    """Headline numbers: tickets handled, auto-resolution rate, escalations, pending approvals, money recovered."""
    return _call("GET", "/api/metrics")


if __name__ == "__main__":
    server.run("stdio")
