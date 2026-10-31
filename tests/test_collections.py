"""Collections teammate: merchant A's AI calls merchant B about an unpaid invoice (offline planner)."""

from datetime import date, timedelta

import pytest

import main
from agent.collections import collections_profile, greeting
from conftest import SEED


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    # Calls are only allowed in the calling window; tests shouldn't depend on the clock
    monkeypatch.setattr(main, "_within_call_hours", lambda: True)


def start(client, invoice_id):
    r = client.post(f"/api/collections/{invoice_id}/call")
    assert r.status_code == 200, r.text
    return r.json()


def turn(client, invoice_id, conv, text):
    r = client.post(f"/api/collections/{invoice_id}/turn", json={"text": text, "conversation_id": conv})
    assert r.status_code == 200, r.text
    return r.json()


def invoice(client, invoice_id):
    return next(i for i in client.get("/api/collections/invoices").json() if i["invoice_id"] == invoice_id)


def test_invoices_seeded():
    assert [i["invoice_id"] for i in SEED["invoices"]] == ["INV-1001", "INV-1002", "INV-1003", "INV-1004"]


def test_opening_line_discloses_and_checks_identity_before_amounts(client):
    call = start(client, "INV-1001")
    g = call["greeting"]
    assert "Sharma General Store" in g and "AI" in g and "record" in g and "Gupta Medicals" in g
    assert "12000" not in g and "12,000" not in g and "INV-1001" not in g     # no details before identity check
    msgs = client.get(f"/api/conversations/{call['conversation_id']}").json()["messages"]
    assert [m["role"] for m in msgs] == ["agent"]


def test_wrong_person_gets_no_details(client):
    call = start(client, "INV-1001")
    r = turn(client, "INV-1001", call["conversation_id"], "Nahi, yeh Sharma Medical hai, wrong number")
    assert "12000" not in r["reply"] and "INV-1001" not in r["reply"]


def test_happy_path_link_then_payment(client):
    conv = start(client, "INV-1001")["conversation_id"]
    r = turn(client, "INV-1001", conv, "Haan ji, main Gupta Medicals se bol raha hoon")
    assert "12000" in r["reply"] and "INV-1001" in r["reply"]
    r = turn(client, "INV-1001", conv, "Theek hai, link bhej do")
    link_id = r["reply"].split("/pay/")[1].split()[0]
    assert invoice(client, "INV-1001")["links"][0]["link_id"] == link_id

    # Merchant B pays on the payment page
    assert client.get(f"/api/pay/{link_id}").json()["link"]["amount"] == 12000
    paid = client.post(f"/api/pay/{link_id}").json()
    assert paid["invoice"]["status"] == "PAID"
    assert client.post(f"/api/pay/{link_id}").json()["already_paid"] is True        # paying twice is a no-op

    r = turn(client, "INV-1001", conv, "Maine pay kar diya")
    assert "mil gaya" in r["reply"]                      # only after get_invoice showed PAID (fact-checked)
    summary = client.get("/api/collections/summary").json()
    assert summary["collected"] == 12000 and summary["paid"] == 1
    notes = [m for m in client.get("/api/outbox").json() if m["to_id"] == "M1001"]
    assert any("paid Rs 12000" in m["body"] for m in notes)


def test_instalment_only_when_allowed(client):
    conv = start(client, "INV-1001")["conversation_id"]       # A allows 2 instalments
    turn(client, "INV-1001", conv, "haan ji bol raha hoon")
    turn(client, "INV-1001", conv, "abhi aadha de sakta hoon, link bhej do")
    assert invoice(client, "INV-1001")["links"][0]["amount"] == 6000


def test_promise_to_pay(client):
    conv = start(client, "INV-1002")["conversation_id"]
    turn(client, "INV-1002", conv, "haan ji Balaji Tiffin Centre")
    r = turn(client, "INV-1002", conv, "Abhi nahi ho payega, kal tak de dunga")
    inv = invoice(client, "INV-1002")
    assert inv["status"] == "PROMISED" and inv["promise_date"] == (date.today() + timedelta(days=1)).isoformat()
    assert inv["promise_date"] in r["reply"]


def test_dispute_goes_back_to_merchant_a(client):
    conv = start(client, "INV-1003")["conversation_id"]
    turn(client, "INV-1003", conv, "haan ji New Fashion Hub")
    r = turn(client, "INV-1003", conv, "Maal kharab tha, main yeh payment nahi karunga")
    assert invoice(client, "INV-1003")["status"] == "DISPUTED" and r["status"] == "ESCALATED"
    assert any(m["subject"].startswith("Dispute") for m in client.get("/api/outbox").json())


# ---------- guardrails (in code) ----------

def gate(name, args, invoice_id="INV-1001", actor="M1001"):
    return collections_profile(main.cstore, invoice_id).check(actor, name, args)


def test_no_discounts_and_instalment_floor():
    assert gate("send_payment_link", {"amount": 12000}).decision == "ALLOW"
    assert gate("send_payment_link", {"amount": 6000}).decision == "ALLOW"            # 2 instalments allowed
    assert gate("send_payment_link", {"amount": 5000}).decision == "BLOCK"            # below the instalment
    assert gate("send_payment_link", {"amount": 1750}, "INV-1002").decision == "BLOCK"  # no instalments on INV-1002


def test_promise_date_window():
    ok = (date.today() + timedelta(days=3)).isoformat()
    too_late = (date.today() + timedelta(days=30)).isoformat()
    assert gate("record_promise_to_pay", {"date": ok}).decision == "ALLOW"
    assert gate("record_promise_to_pay", {"date": too_late}).decision == "BLOCK"
    assert gate("record_promise_to_pay", {"date": "next month"}).decision == "BLOCK"


def test_other_merchants_cannot_use_the_invoice():
    assert gate("send_payment_link", {"amount": 12000}, actor="M1002").decision == "BLOCK"


def test_link_limit_and_paid_invoice(client):
    for _ in range(3):
        main.cstore.create_link("INV-1001", 12000)
    assert gate("send_payment_link", {"amount": 12000}).decision == "BLOCK"
    link = main.cstore.create_link("INV-1002", 3500)
    client.post(f"/api/pay/{link['link_id']}")
    assert gate("send_payment_link", {"amount": 3500}, "INV-1002").decision == "BLOCK"


def test_no_calls_outside_hours_or_on_paid_invoices(client, monkeypatch):
    monkeypatch.setattr(main, "_within_call_hours", lambda: False)
    assert client.post("/api/collections/INV-1001/call").status_code == 409


# ---------- reminder message in A's name ----------

def test_reminder_draft_needs_approval_then_sends(client):
    draft = client.post("/api/collections/INV-1004/reminder").json()
    assert draft["status"] == "DRAFT" and draft["channel"] == "whatsapp"
    assert "7200" in draft["body"] and draft["payment_link"] in draft["body"]
    sent = client.post(f"/api/outbox/{draft['id']}/send", json={"body": draft["body"] + " (edited)",
                                                                 "approved_by": "Sharma ji"}).json()
    assert sent["status"] == "SENT" and sent["body"].endswith("(edited)") and sent["approved_by"] == "Sharma ji"
    assert client.post(f"/api/outbox/{draft['id']}/send", json={"body": "x"}).status_code == 409


def test_collections_calls_stay_out_of_settlement_metrics(client):
    conv = start(client, "INV-1002")["conversation_id"]
    turn(client, "INV-1002", conv, "haan ji")
    assert client.get("/api/metrics").json()["tickets"] == 0
    assert client.get("/api/customers/M1001").json()["counts"]["tickets"] == 0


def test_reminder_drafts_must_go_from_creditor_to_debtor():
    from agent.collections import _right_way_round
    view = main.cstore.view(main.cstore.invoice("INV-1004"))
    # Seen live from the AI: right facts, wrong direction
    assert not _right_way_round("Hello Sharma ji, Mehta Electronics ke invoice INV-1004 ke liye 7200 rupaye ka "
                                "payment due hai. Link: http://x/pay/PAY-1. Dhanyawad.", view)
    assert _right_way_round("Namaste Mehta Electronics ji, Sharma General Store ki taraf se yaad dilana tha ki "
                            "INV-1004 ka Rs 7200 due hai. Link: http://x/pay/PAY-1", view)
