"""End-to-end agent behaviour through the API, with the offline scripted planner."""

import time

import pytest

from conftest import EXPECTED_STATUS, SEED, run_ticket, seed_ticket


@pytest.mark.parametrize("ticket_id,status,in_reply", [
    ("DEMO-1", "RESOLVED", "₹4,500"),            # failed settlement -> retried
    ("DEMO-2", "RESOLVED", "refund ref RFND"),    # duplicate debit -> reversed
    ("DEMO-3", "RESOLVED", "UTR998877001"),       # pending but bank shows credited
    ("DEMO-4", "AWAITING_APPROVAL", "₹200"),      # ₹25,000 over cap, RBI compensation mentioned
    ("DEMO-5", "ESCALATED", "specialist"),        # repeat claim blocked
    ("DEMO-6", "RESOLVED", "₹4,500"),             # no txn id -> found by amount + day
])
def test_demo_scenarios(client, ticket_id, status, in_reply):
    r = run_ticket(client, ticket_id)
    assert r["status"] == status
    assert in_reply in r["reply"]
    assert r["steps"] >= 2


def test_every_tool_call_is_audited_with_reason_and_decision(client):
    run_ticket(client, "DEMO-5")
    rows = client.get("/api/audit", params={"ticket_id": "DEMO-5"}).json()
    tools = [r for r in rows if r["event"] == "tool_call"]
    assert [r["tool"] for r in tools] == ["check_payment_status", "get_settlement_status",
                                          "trigger_bank_reconciliation", "issue_instant_credit", "escalate_to_human"]
    assert all(r["reason"] for r in tools)
    assert next(r for r in tools if r["tool"] == "issue_instant_credit")["decision"] == "BLOCK"
    assert rows[0]["event"] == "ticket_received" and rows[-1]["event"] == "reply_sent"


def test_blocked_credit_never_reaches_credits_table(client, store):
    run_ticket(client, "DEMO-5")
    assert store.credited_today("M1005") == 0


def test_approval_flow_approve(client, store):
    run_ticket(client, "DEMO-4")
    pending = client.get("/api/approvals", params={"status": "PENDING"}).json()
    assert len(pending) == 1 and pending[0]["tool"] == "issue_instant_credit"

    r = client.post(f"/api/approvals/{pending[0]['id']}/approve", json={}).json()
    assert r["status"] == "APPROVED" and r["result"]["amount"] == 25000
    assert r["result"]["credit_id"] in r["reply"]
    assert store.credited_today("M1004") == 25000

    ticket = next(t for t in client.get("/api/tickets").json() if t["ticket_id"] == "DEMO-4")
    assert ticket["status"] == "RESOLVED" and ticket["human_involved"] == 1
    # Can't approve twice
    assert client.post(f"/api/approvals/{pending[0]['id']}/approve", json={}).status_code == 409


def test_approval_flow_reject(client, store):
    run_ticket(client, "DEMO-4")
    a = client.get("/api/approvals", params={"status": "PENDING"}).json()[0]
    assert client.post(f"/api/approvals/{a['id']}/reject", json={}).json()["status"] == "REJECTED"
    ticket = next(t for t in client.get("/api/tickets").json() if t["ticket_id"] == "DEMO-4")
    assert ticket["status"] == "ESCALATED"
    assert store.credited_today("M1004") == 0


def test_repeat_ticket_reuses_pending_approval(client):
    run_ticket(client, "DEMO-4")
    t = seed_ticket("DEMO-4")
    client.post("/api/tickets", json={"merchant_id": t["merchant_id"], "text": t["text"], "ticket_id": "DEMO-4-again"})
    assert len(client.get("/api/approvals", params={"status": "PENDING"}).json()) == 1


def test_metrics_after_demo_scenarios(client):
    for i in range(1, 7):
        run_ticket(client, f"DEMO-{i}")
    m = client.get("/api/metrics").json()
    assert m["tickets"] == 6
    assert m["auto_resolved"] == 4 and m["escalated"] == 1 and m["awaiting_approval"] == 1
    assert m["recovered_amount"] == 4500 + 1200   # retried settlement + reversed duplicate


def _wait_for_batch(client, n=50, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/metrics").json()["tickets"] >= n:  # metrics only count finished tickets
            return
        time.sleep(0.2)
    raise AssertionError("batch did not finish")


def test_batch_of_50_takes_the_expected_path(client):
    assert client.post("/api/batch").json()["status"] == "started"
    _wait_for_batch(client)
    scenario = {t["ticket_id"]: t["scenario"] for t in SEED["tickets"]}
    rows = client.get("/api/tickets").json()
    assert len(rows) == 50
    wrong = [(r["ticket_id"], scenario[r["ticket_id"]], r["status"]) for r in rows
             if r["status"] != EXPECTED_STATUS[scenario[r["ticket_id"]]]]
    assert wrong == []
    m = client.get("/api/metrics").json()
    assert m["auto_resolution_rate"] == 84 and m["awaiting_approval"] == 5 and m["escalated"] == 3


def test_batch_resets_data_first(client):
    # Fix one duplicate by hand; the batch must still see fresh data and resolve it itself
    run_ticket(client, "DEMO-2")
    client.post("/api/batch")
    _wait_for_batch(client)
    demo2 = next(t for t in client.get("/api/tickets").json() if t["ticket_id"] == "DEMO-2")
    assert demo2["status"] == "RESOLVED"
    assert client.get("/api/metrics").json()["auto_resolution_rate"] == 84
