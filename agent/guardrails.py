"""
Guardrails: deterministic business rules checked in code before any action tool runs.

The LLM proposes an action; these gates decide. Pattern: extract -> gate -> act
(adapted from the ShopBot "business logic gates" design).

Decisions:
  ALLOW           run the tool now
  NEEDS_APPROVAL  valid, but over the auto-credit cap -> human approval queue
  BLOCK           invalid, ineligible or risky -> never runs, ticket escalated
"""

from dataclasses import dataclass, field

from config import settings
from mock_systems.store import MockStore
from agent.tools import ACTION_TOOLS


@dataclass
class GateResult:
    decision: str  # ALLOW | NEEDS_APPROVAL | BLOCK
    reasons: list[str] = field(default_factory=list)


def check(store: MockStore, merchant_id: str, name: str, args: dict, human_approved: bool = False) -> GateResult:
    """Check a proposed tool call. Read-only tools are always allowed."""
    if name not in ACTION_TOOLS:
        return GateResult("ALLOW")

    if name == "issue_instant_credit":
        # One query for everything this gate needs (txn, settlement, bank, prior and today's credits)
        txn = store.credit_context(merchant_id, args.get("txn_id", ""))
    else:
        txn = store._own_txn(args.get("txn_id", ""), merchant_id)
    if not txn:
        return GateResult("BLOCK", [f"Transaction {args.get('txn_id')} not found for this merchant"])

    if name == "retry_settlement":
        if txn["settlement_status"] != "FAILED":
            return GateResult("BLOCK", [f"Settlement is {txn['settlement_status']}, only FAILED settlements can be retried"])
        return GateResult("ALLOW")

    if name == "reverse_duplicate_debit":
        twin = store.twin_of(txn)
        if not twin:
            return GateResult("BLOCK", ["No duplicate debit found for this transaction"])
        dup = txn if txn["duplicate_of"] else twin
        if dup["status"] == "REVERSED":
            return GateResult("BLOCK", ["Duplicate debit was already reversed"])
        return GateResult("ALLOW")

    # issue_instant_credit
    blocks, approvals = [], []
    merchant = store.get_merchant(merchant_id)
    amount = float(args.get("amount", 0))
    status = store._settlement_view(txn)

    if txn["prior_credits"]:
        blocks.append("A credit was already issued for this transaction (repeat claim)")
    if txn["status"] != "SUCCESS":
        blocks.append(f"Payment status is {txn['status']}, not SUCCESS")
    if txn["settlement_status"] not in ("PENDING", "FAILED"):
        blocks.append(f"Settlement is already {txn['settlement_status']}")
    if abs(amount - txn["amount"]) >= 1:
        blocks.append(f"Credit amount ₹{amount:,.0f} does not match transaction amount ₹{txn['amount']:,}")
    if not status["tat_breached"]:
        blocks.append(f"RBI TAT (T+{settings.merchant_tat_days}) not yet breached; wait for normal settlement")
    if txn["bank_status"] == "CREDITED":
        blocks.append("Bank shows this UTR as already credited")
    if merchant["kyc_tier"] != "FULL":
        blocks.append("Merchant KYC is not FULL")
    if merchant["account_age_days"] < settings.min_account_age_days:
        blocks.append(f"Account is {merchant['account_age_days']} days old (min {settings.min_account_age_days})")
    if merchant["risk_score"] > settings.max_risk_score:
        blocks.append(f"Risk score {merchant['risk_score']} above {settings.max_risk_score}")

    if amount > settings.per_txn_credit_cap:
        approvals.append(f"₹{amount:,.0f} is above the ₹{settings.per_txn_credit_cap:,} auto-credit cap")
    if (txn["credited_today"] or 0) + amount > settings.daily_credit_cap:
        approvals.append(f"Would exceed the ₹{settings.daily_credit_cap:,} daily credit cap for this merchant")

    if blocks:
        return GateResult("BLOCK", blocks)
    if approvals and not human_approved:
        return GateResult("NEEDS_APPROVAL", approvals)
    return GateResult("ALLOW", approvals)
