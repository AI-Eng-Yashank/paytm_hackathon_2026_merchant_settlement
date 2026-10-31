"""Mock systems (Payments, Ledger, Bank, Credits, CRM) backed by the database."""

from conftest import SEED


def test_seed_is_loaded(store):
    assert len(store.list_merchants()) == len(SEED["merchants"]) == 49
    assert len(store.list_seed_tickets()) == 50
    assert [t["ticket_id"] for t in store.list_seed_tickets("DEMO-")] == [f"DEMO-{i}" for i in range(1, 7)]


def test_payment_status_for_own_transaction(store):
    r = store.check_payment_status("M1001", "TXN240001")
    assert r["amount"] == 4500 and r["status"] == "SUCCESS"
    assert r["is_duplicate_debit"] is False


def test_merchant_cannot_see_another_merchants_transaction(store):
    # TXN240001 belongs to M1001
    assert store.check_payment_status("M1002", "TXN240001") == {"error": "TXN_NOT_FOUND", "txn_id": "TXN240001"}
    assert store.get_settlement_status("M1002", "TXN240001")["error"] == "TXN_NOT_FOUND"


def test_txn_id_is_case_and_space_insensitive(store):
    assert store.check_payment_status("M1001", "  txn240001 ")["txn_id"] == "TXN240001"


def test_duplicate_debit_is_detected_from_either_side(store):
    assert store.check_payment_status("M1002", "TXN240003")["duplicate_pair"] == "TXN240002"
    assert store.check_payment_status("M1002", "TXN240002")["duplicate_pair"] == "TXN240003"


def test_find_transaction_by_amount_and_day(store):
    assert [m["txn_id"] for m in store.find_txn_by_amount_time("M1001", 4500, 1)["matches"]] == ["TXN240001"]
    assert store.find_txn_by_amount_time("M1001", 4500, 3)["matches"] == []
    assert store.find_txn_by_amount_time("M1001", 999, None)["matches"] == []


def test_rbi_tat_breach_and_compensation(store):
    breached = store.get_settlement_status("M1004", "TXN240005")   # 7 days, merchant TAT is T+5
    assert breached["tat_breached"] is True and breached["compensation_due"] == 200
    fresh = store.get_settlement_status("M1003", "TXN240004")      # 2 days
    assert fresh["tat_breached"] is False and fresh["compensation_due"] == 0


def test_retry_settlement_settles_with_new_utr(store):
    r = store.retry_settlement("M1001", "TXN240001")
    assert r["settlement_status"] == "SETTLED" and r["new_utr"].startswith("UTR")
    assert store.get_settlement_status("M1001", "TXN240001")["settlement_status"] == "SETTLED"
    assert store.get_bank_record(r["new_utr"])["status"] == "CREDITED"


def test_bank_reconciliation_marks_credited_settlement_settled(store):
    r = store.trigger_bank_reconciliation("M1003", "UTR998877001")
    assert r["bank_status"] == "CREDITED" and r["merchant_bank"] == "ICICI Bank"
    assert store.get_settlement_status("M1003", "TXN240004")["settlement_status"] == "SETTLED"


def test_bank_reconciliation_unknown_or_foreign_utr(store):
    assert store.trigger_bank_reconciliation("M1003", "UTR000")["bank_status"] == "NOT_FOUND"
    assert store.trigger_bank_reconciliation("M1001", "UTR998877001")["bank_status"] == "NOT_FOUND"  # M1003's UTR


def test_reverse_duplicate_and_instant_credit_write_to_db(store):
    r = store.reverse_duplicate_debit("M1002", "TXN240002")       # given the original, reverses the duplicate
    assert r["reversed_txn_id"] == "TXN240003" and r["refund_to"] == "neha.s@oksbi"
    c = store.issue_instant_credit("M1004", "TXN240005", 25000.0)
    assert c["amount"] == 25000 and isinstance(c["amount"], int)
    assert store.prior_credit("TXN240005")["credit_id"] == c["credit_id"]
    assert store.credited_today("M1004") == 25000


def test_reset_restores_seed_state(store):
    store.retry_settlement("M1001", "TXN240001")
    store.reset()
    assert store.get_settlement_status("M1001", "TXN240001")["settlement_status"] == "FAILED"


def test_spoken_txn_ids_with_spaces_or_dashes_still_match(store):
    # Speech-to-text writes "TXN240004" as "TXN 240 004"
    for spoken in ["TXN 240 004", "txn-240004", " Txn 240004 "]:
        assert store.check_payment_status("M1003", spoken)["txn_id"] == "TXN240004"
