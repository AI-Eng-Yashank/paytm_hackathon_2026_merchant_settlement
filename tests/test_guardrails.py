"""Guardrails: every money action is decided by rules in code, never by the model."""

from agent import guardrails
from conftest import SEED, first_of


def check(store, merchant, tool, **args):
    return guardrails.check(store, merchant, tool, args)


def test_read_only_tools_always_allowed(store):
    for tool in ["lookup_merchant", "check_payment_status", "get_settlement_status", "trigger_bank_reconciliation"]:
        assert check(store, "M1001", tool, txn_id="TXN999").decision == "ALLOW"


def test_action_on_unknown_or_foreign_txn_is_blocked(store):
    g = check(store, "M1002", "retry_settlement", txn_id="TXN240001")  # M1001's txn
    assert g.decision == "BLOCK" and "not found" in g.reasons[0]


def test_retry_only_failed_settlements(store):
    assert check(store, "M1001", "retry_settlement", txn_id="TXN240001").decision == "ALLOW"
    g = check(store, "M1003", "retry_settlement", txn_id="TXN240004")  # PENDING
    assert g.decision == "BLOCK" and "only FAILED" in g.reasons[0]


def test_duplicate_reversal_allowed_once(store):
    assert check(store, "M1002", "reverse_duplicate_debit", txn_id="TXN240003").decision == "ALLOW"
    store.reverse_duplicate_debit("M1002", "TXN240003")
    g = check(store, "M1002", "reverse_duplicate_debit", txn_id="TXN240003")
    assert g.decision == "BLOCK" and "already reversed" in g.reasons[0]


def test_reversal_needs_a_real_duplicate(store):
    g = check(store, "M1001", "reverse_duplicate_debit", txn_id="TXN240001")
    assert g.decision == "BLOCK" and "No duplicate" in g.reasons[0]


def test_credit_over_cap_needs_human_approval(store):
    g = check(store, "M1004", "issue_instant_credit", txn_id="TXN240005", amount=25000)
    assert g.decision == "NEEDS_APPROVAL"
    assert any("above the ₹10,000 auto-credit cap" in r for r in g.reasons)


def test_human_approval_waives_cap_only(store):
    g = guardrails.check(store, "M1004", "issue_instant_credit", {"txn_id": "TXN240005", "amount": 25000},
                         human_approved=True)
    assert g.decision == "ALLOW"
    # ...but hard rules still apply even with approval
    g = guardrails.check(store, "M1004", "issue_instant_credit", {"txn_id": "TXN240005", "amount": 20000},
                         human_approved=True)
    assert g.decision == "BLOCK" and "does not match" in g.reasons[0]


def test_small_breached_credit_is_automatic(store):
    # A generated merchant whose small settlement is past T+5 and not found at the bank
    merchant = first_of("tat_breach_small")["merchant_id"]
    txn = next(x for x in SEED["transactions"] if x["merchant_id"] == merchant)
    assert txn["amount"] <= 10_000
    g = check(store, merchant, "issue_instant_credit", txn_id=txn["txn_id"], amount=txn["amount"])
    assert g.decision == "ALLOW", g.reasons


def test_credit_blocked_before_tat_breach(store):
    # TXN240004 is only 2 days old and the bank already shows it credited
    g = check(store, "M1003", "issue_instant_credit", txn_id="TXN240004", amount=3250)
    assert g.decision == "BLOCK"
    assert any("TAT" in r for r in g.reasons)
    assert any("already credited" in r for r in g.reasons)


def test_repeat_claim_and_risky_merchant_blocked_with_all_reasons(store):
    g = check(store, "M1005", "issue_instant_credit", txn_id="TXN240006", amount=8000)
    assert g.decision == "BLOCK"
    text = " | ".join(g.reasons)
    for expected in ["repeat claim", "KYC is not FULL", "12 days old", "Risk score 0.85"]:
        assert expected in text


def test_wrong_amount_blocked(store):
    g = check(store, "M1004", "issue_instant_credit", txn_id="TXN240005", amount=99999)
    assert g.decision == "BLOCK" and "does not match" in g.reasons[0]


def test_daily_cap_counts_earlier_credits(store):
    store.issue_instant_credit("M1004", "TXN240005", 25000)
    # A fresh credit for the same txn is now a repeat claim, which is blocked outright
    assert check(store, "M1004", "issue_instant_credit", txn_id="TXN240005", amount=25000).decision == "BLOCK"
