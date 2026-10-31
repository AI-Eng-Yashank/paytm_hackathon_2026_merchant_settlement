"""MCP server tools, called the way an MCP client would (through MCPServer.call_tool)."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import mcp_server
from conftest import run_ticket


@pytest.fixture(autouse=True)
def mcp_uses_test_api(client, monkeypatch):
    # TestClient is an httpx.Client, so the MCP tools talk to the in-process test app
    monkeypatch.setattr(mcp_server, "http", client)


def call(name: str, **args):
    result = asyncio.run(mcp_server.server.call_tool(name, args))
    assert not result.is_error, result.content[0].text
    # Lists come back as structured {"result": [...]}; dicts as JSON text
    if result.structured_content and "result" in result.structured_content:
        return result.structured_content["result"]
    return json.loads(result.content[0].text)


def call_error(name: str, **args) -> str:
    """In-process, a tool error is raised (MCP clients receive it as an is_error result)."""
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp_server.server.call_tool(name, args))
    return str(e.value)


def test_all_tools_registered_with_safety_hints():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert set(tools) == {"find_merchants", "get_merchant", "check_transaction", "resolve_ticket",
                          "list_pending_approvals", "approve_credit", "reject_credit", "get_ticket_audit",
                          "recent_tickets", "get_metrics", "get_customer_overview", "get_conversation_transcript"}
    assert tools["get_metrics"].annotations.read_only_hint is True
    assert tools["approve_credit"].annotations.destructive_hint is True
    assert tools["reject_credit"].annotations.destructive_hint is True


def test_find_and_get_merchant():
    found = call("find_merchants", query="sharma")
    assert [m["merchant_id"] for m in found] == ["M1001"]
    assert call("get_merchant", merchant_id="M1001")["settlement_bank"] == "HDFC Bank"
    assert "404" in call_error("get_merchant", merchant_id="M9999")


def test_check_transaction_is_read_only_and_scoped_to_merchant(store):
    r = call("check_transaction", merchant_id="M1004", txn_id="TXN240005")
    assert r["payment"]["amount"] == 25000 and r["settlement"]["tat_breached"] is True
    assert "404" in call_error("check_transaction", merchant_id="M1002", txn_id="TXN240005")
    assert store.get_settlement_status("M1004", "TXN240005")["settlement_status"] == "PENDING"  # unchanged


def test_resolve_ticket_runs_the_agent():
    r = call("resolve_ticket", merchant_id="M1001", message="Mera kal ka ₹4,500 ka settlement nahi aaya, TXN240001")
    assert r["status"] == "RESOLVED" and "₹4,500" in r["reply"]
    trail = call("get_ticket_audit", ticket_id=r["ticket_id"])
    assert [s["tool"] for s in trail if s["event"] == "tool_call"][-1] == "retry_settlement"


def test_approval_queue_through_mcp(client, store):
    run_ticket(client, "DEMO-4")
    pending = call("list_pending_approvals")
    assert len(pending) == 1 and pending[0]["amount"] == 25000
    assert any("auto-credit cap" in why for why in pending[0]["why_approval_needed"])

    r = call("approve_credit", approval_id=pending[0]["approval_id"], approved_by="ops@test")
    assert r["status"] == "APPROVED" and r["credit"]["amount"] == 25000
    assert store.credited_today("M1004") == 25000
    assert "409" in call_error("approve_credit", approval_id=pending[0]["approval_id"], approved_by="ops@test")


def test_reject_through_mcp(client, store):
    run_ticket(client, "DEMO-4")
    approval_id = call("list_pending_approvals")[0]["approval_id"]
    assert call("reject_credit", approval_id=approval_id, rejected_by="ops@test")["status"] == "REJECTED"
    assert store.credited_today("M1004") == 0


def test_metrics_and_recent_tickets(client):
    run_ticket(client, "DEMO-2")
    assert call("get_metrics")["tickets"] == 1
    assert call("recent_tickets", limit=5)[0]["ticket_id"] == "DEMO-2"


def test_customer_overview_and_transcript_through_mcp(client):
    r = call("resolve_ticket", merchant_id="M1004", message="7 din ho gaye, ₹25,000 ka settlement nahi aaya. TXN240005")
    overview = call("get_customer_overview", merchant_id="M1004")
    assert overview["pending_approvals"][0]["amount"] == 25000
    assert any("waiting for approval" in f["why"] for f in overview["health"]["factors"])
    conv_id = overview["conversations"][0]["conversation_id"]
    transcript = call("get_conversation_transcript", conversation_id=conv_id)
    assert [t["speaker"] for t in transcript["turns"]] == ["merchant", "AI"]
    assert "issue_instant_credit" not in transcript["turns"][1]["tools_used"]  # needed approval, so not executed
    assert transcript["turns"][1]["text"] == r["reply"]


def test_clear_message_when_api_is_down(monkeypatch):
    import httpx
    monkeypatch.setattr(mcp_server, "http", httpx.Client(base_url="http://127.0.0.1:1"))
    assert "API is not running" in call_error("get_metrics")
