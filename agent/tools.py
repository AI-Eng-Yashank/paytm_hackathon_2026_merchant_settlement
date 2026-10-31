"""
Tool definitions the agent can call, and the dispatcher that routes them to the mock systems.

Every tool takes a `reason` argument so each step lands in the audit log with its why.
`merchant_id` is never taken from the model: it is injected from the ticket, so the agent
can only touch the ticket owner's data.
"""

from mock_systems.store import MockStore


REASON = {"type": "string", "description": "One short sentence: why you are calling this tool now."}
TXN_ID = {"type": "string", "description": "Transaction id, e.g. TXN240001"}


def _tool(name: str, description: str, properties: dict, required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {**properties, "reason": REASON},
                "required": required + ["reason"],
            },
        },
    }


TOOL_SCHEMAS = [
    _tool("lookup_merchant", "CRM: get the merchant's profile, KYC tier, settlement bank and recent ticket count.",
          {}, []),
    _tool("check_payment_status", "Payments: status of a payment, and whether it was debited twice.",
          {"txn_id": TXN_ID}, ["txn_id"]),
    _tool("find_txn_by_amount_time", "Payments: find the merchant's transaction by amount (and days ago) when no txn id is given.",
          {"amount": {"type": "number"}, "days_ago": {"type": "integer", "description": "0 = today, 1 = yesterday"}},
          ["amount"]),
    _tool("list_recent_transactions",
          "Payments: the merchant's most recent transactions with amount, date, time and settlement status. "
          "Use when the merchant asks about recent payments or gives several amounts.",
          {"limit": {"type": "integer", "description": "How many, newest first (default 10, max 20)"}}, []),
    _tool("get_settlement_status", "Ledger: settlement status, UTR, failure reason and RBI TAT breach for a transaction.",
          {"txn_id": TXN_ID}, ["txn_id"]),
    _tool("retry_settlement", "Ledger: re-push a FAILED settlement to the merchant's bank.",
          {"txn_id": TXN_ID}, ["txn_id"]),
    _tool("trigger_bank_reconciliation", "Bank/NPCI: check with the bank whether a settlement UTR was credited.",
          {"utr": {"type": "string"}}, ["utr"]),
    _tool("reverse_duplicate_debit", "Credits: refund the second debit of a duplicate payment to the customer.",
          {"txn_id": TXN_ID}, ["txn_id"]),
    _tool("issue_instant_credit",
          "Credits: credit the merchant instantly for a settlement that is overdue (RBI TAT breached) and not found at the bank. Subject to caps and eligibility rules.",
          {"txn_id": TXN_ID, "amount": {"type": "number"}}, ["txn_id", "amount"]),
    _tool("escalate_to_human", "CRM: hand the ticket to a human agent with a summary of what you found.",
          {"summary": {"type": "string"}}, ["summary"]),
]

# Which mock system each tool talks to (drives the dashboard's system map)
TOOL_SYSTEM = {
    "lookup_merchant": "crm",
    "check_payment_status": "payments",
    "find_txn_by_amount_time": "payments",
    "list_recent_transactions": "payments",
    "get_settlement_status": "ledger",
    "retry_settlement": "ledger",
    "trigger_bank_reconciliation": "bank",
    "reverse_duplicate_debit": "credits",
    "issue_instant_credit": "credits",
    "escalate_to_human": "crm",
}

ACTION_TOOLS = {"retry_settlement", "reverse_duplicate_debit", "issue_instant_credit"}


def execute_tool(store: MockStore, merchant_id: str, name: str, args: dict) -> dict:
    """Run a tool against the mock systems. Guardrails must already have allowed it."""
    if name == "lookup_merchant":
        return store.lookup_merchant(merchant_id)
    if name == "check_payment_status":
        return store.check_payment_status(merchant_id, args.get("txn_id", ""))
    if name == "find_txn_by_amount_time":
        return store.find_txn_by_amount_time(merchant_id, args.get("amount", 0), args.get("days_ago"))
    if name == "list_recent_transactions":
        return store.list_recent_transactions(merchant_id, int(args.get("limit") or 10))
    if name == "get_settlement_status":
        return store.get_settlement_status(merchant_id, args.get("txn_id", ""))
    if name == "retry_settlement":
        return store.retry_settlement(merchant_id, args["txn_id"])
    if name == "trigger_bank_reconciliation":
        return store.trigger_bank_reconciliation(merchant_id, args.get("utr", ""))
    if name == "reverse_duplicate_debit":
        return store.reverse_duplicate_debit(merchant_id, args["txn_id"])
    if name == "issue_instant_credit":
        return store.issue_instant_credit(merchant_id, args["txn_id"], float(args["amount"]))
    if name == "escalate_to_human":
        return {"status": "ESCALATED", "queue": "settlements-l2"}
    return {"error": f"UNKNOWN_TOOL {name}"}
