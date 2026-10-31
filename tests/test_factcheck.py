"""Fact-check: every reply must be backed by tool results before it reaches the merchant."""

import json

import main
from agent import factcheck
from services.scripted_llm import _Message


RETRY = json.dumps({"tool": "retry_settlement", "args": {"txn_id": "TXN240001"},
                    "result": {"txn_id": "TXN240001", "settlement_status": "SETTLED", "new_utr": "UTR518389021",
                               "amount": 4500, "expected_in_account": "within 2 hours"}})
BANK_CREDITED = json.dumps({"tool": "trigger_bank_reconciliation", "args": {"utr": "UTR998877001"},
                            "result": {"utr": "UTR998877001", "bank_status": "CREDITED", "credited_amount": 3250,
                                       "merchant_bank": "ICICI Bank"}})


def check(reply, evidence=RETRY, merchant="Mera ₹4,500 nahi aaya"):
    return factcheck.check(reply, evidence, merchant)


# ---------- passes ----------

def test_correct_replies_pass():
    assert check("Aapka ₹4,500 ka settlement dobara bhej diya gaya hai, naya UTR UTR518389021. "
                 "Paisa 2 ghante me aapke account me aa jayega.").ok
    assert check("Aapka Rs 3250 ICICI Bank account me credit ho chuka hai, UTR 998877001.",
                 evidence=BANK_CREDITED, merchant="TXN240004 kab aayega?").ok


def test_future_tense_is_fine_without_confirmation():
    assert check("Paisa 2 ghante me aa jayega, credit ho jayega.").ok


def test_amount_the_merchant_mentioned_can_be_repeated():
    assert check("₹500 aur ₹1,500 ke transactions abhi nahi mile.", evidence="{}",
                 merchant="kal 500 aur 1500 ke transaction nahi aaye").ok


def test_rbi_rate_is_always_allowed():
    assert check("RBI niyam ke hisaab se Rs 100 per day compensation milega.").ok


# ---------- catches ----------

def test_overclaim_money_arrived_is_caught():
    # The real slip seen live: only "expected within 2 hours" was known
    r = check("Sharma ji, paisa aapke account mein aa gaya hai. UTR UTR518389021.")
    assert not r.ok and "arrived" in r.issues[0]
    assert not check("Your amount has been credited.").ok
    assert not check("पैसा आपके खाते में आ गया है।").ok


def test_invented_utr_is_caught():
    r = check("Naya UTR UTR123456789 hai.")
    assert not r.ok and "UTR123456789" in r.issues[0]


def test_invented_amount_is_caught():
    r = check("Aapka ₹45,000 ka settlement bhej diya gaya hai.")
    assert not r.ok and "45,000" in " ".join(r.issues)


def test_action_claims_need_the_action():
    assert not check("Duplicate amount wapas bhej diya gaya hai.").ok                  # no reversal ran
    assert not check("Aapke liye instant credit kar diya hai.").ok                     # no credit ran
    assert not check("Settlement retry kar diya gaya hai.", evidence="{}").ok          # no retry ran
    assert check("Settlement retry kar diya gaya hai.").ok


def test_internal_details_are_caught():
    assert not check('Status {"approval_id": 3}').ok
    assert not check("Aapka risk score high hai.").ok


# ---------- inside the agent ----------

def replies(*texts):
    queue = list(texts)

    async def fake_chat(messages, tools):
        return _Message(content=queue.pop(0))
    return fake_chat


def test_bad_reply_is_rewritten(client, monkeypatch):
    monkeypatch.setattr(main.resolver.llm, "chat",
                        replies("Paisa aa gaya hai, UTR UTR999999999.", "Paisa jaldi aa jayega, hum track kar rahe hain."))
    r = client.post("/api/tickets", json={"merchant_id": "M1001", "text": "kab aayega?"}).json()
    assert r["reply"] == "Paisa jaldi aa jayega, hum track kar rahe hain."
    checks = [a for a in client.get("/api/audit", params={"ticket_id": r["ticket_id"]}).json() if a["event"] == "fact_check"]
    assert [c["decision"] for c in checks] == ["FAIL", "PASS"]
    assert "UTR999999999" in checks[0]["reason"]


def test_still_bad_after_rewrite_sends_safe_reply_and_escalates(client, monkeypatch):
    monkeypatch.setattr(main.resolver.llm, "chat", replies("Paisa aa gaya hai.", "Haan paisa credit ho gaya hai."))
    r = client.post("/api/tickets", json={"merchant_id": "M1001", "text": "kab aayega?"}).json()
    assert r["reply"] == factcheck.SAFE_REPLY
    assert r["status"] == "ESCALATED"


def test_demo_scenarios_pass_fact_check_first_time(client):
    for i in range(1, 7):
        t = next(x for x in __import__("conftest").SEED["tickets"] if x["ticket_id"] == f"DEMO-{i}")
        r = client.post("/api/tickets", json={"merchant_id": t["merchant_id"], "text": t["text"],
                                              "ticket_id": t["ticket_id"]}).json()
        checks = [a["decision"] for a in client.get("/api/audit", params={"ticket_id": r["ticket_id"]}).json()
                  if a["event"] == "fact_check"]
        assert checks == ["PASS"], (t["ticket_id"], r["reply"])
