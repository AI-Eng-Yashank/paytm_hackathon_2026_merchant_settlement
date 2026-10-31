"""
Fact-check: rules in code that every reply must pass before it reaches the merchant.

The guardrails stop wrong *actions*; this stops wrong *words*. A reply may only state what the
tools returned (in this turn or earlier in the conversation) or what the merchant said:

  1. IDs      every UTR, refund ref, credit id and txn id must appear in the evidence
  2. Amounts  every ₹ amount must appear in the evidence or the merchant's messages
  3. Arrival  "paisa aa gaya" / "credit ho chuka" only if the bank or an instant credit confirmed it
  4. Actions  "retry kar diya" / "refund bhej diya" / "credit kar diya" only if that action ran
  5. Leaks    no JSON, approval ids, risk scores or queue names

It is deliberately strict and cheap (milliseconds, no AI call). On failure the agent gets one
chance to rewrite; if that also fails, a safe reply goes out and a human reviews the ticket.
"""

import re
from dataclasses import dataclass, field

from config import settings


SAFE_REPLY = ("Maine aapki details check kar li hain. Ek specialist inhe confirm karke jaldi aapse "
              "sampark karega.")

ID_RE = re.compile(r"\b(UTR\s?\d{6,}|RFND\d{3,}|CR-\d{3,}|TXN\d{3,}|INV-\d{3,}|PAY-[A-Z0-9]{4,})\b", re.I)
# Amounts must start with a digit ("Rs," alone once matched an empty number and crashed the check)
AMOUNT_RE = re.compile(
    r"(?:₹|\brs\.?|\binr)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:rupaye|rupees|rupay|रुपये|रुपए|rs\b)", re.I)

PAST = r"(?:gaya|gayi|gaye|chuka|chuki|chuke|diya|diye|di\b|kar diya|ho gaya|ho chuka|done)"
# Money has reached the merchant's account
ARRIVAL_RE = re.compile(
    r"\b(?:aa|a|pahunch|pahuch|jama|credit|transfer)\s*(?:ho\s*)?(?:gaya|gayi|gaye|chuka|chuki|chuke)\b"
    r"|\b(?:has been|was|got)\s+(?:credited|received|deposited)\b|\breached your (?:bank )?account\b"
    r"|आ गया|आ गए|जमा हो गया|क्रेडिट हो गया|पहुंच गया|पहुँच गया", re.I)
ACTION_CLAIMS = {
    "retry": (re.compile(rf"\b(?:retry|dobara bhej|re-?push|resend)\w*\b.*?\b{PAST}", re.I), r'"new_utr"'),
    "refund": (re.compile(rf"\b(?:refund|wapas|reverse|lauta)\w*\b.*?\b{PAST}", re.I), r'"refund_ref"'),
    "credit": (re.compile(rf"\binstant credit\b.*?\b(?:kar diya|ho gaya|de diya|issued|done)\b", re.I), r'"credit_id"'),
}
# A payment link's amount, as the tool created it
LINK_AMOUNT_RE = re.compile(r'"link_id":\s*"(PAY-[A-Z0-9]+)",\s*"amount":\s*(\d+)')
LEAK_RE = re.compile(r"[{}]|approval[_ ]id|risk[_ ]score|settlements-l2|\btool\b|\bguardrail\b", re.I)
# Money confirmed: bank credited, an instant credit ran, or (collections) the invoice shows PAID
CREDITED_RE = re.compile(r'"bank_status":\s*"CREDITED"|"status":\s*"CREDITED"|"status":\s*"PAID"')


@dataclass
class FactCheck:
    ok: bool
    issues: list[str] = field(default_factory=list)


def _norm_id(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def _numbers(text: str) -> set[int]:
    return {int(float(n.replace(",", ""))) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)
            if n.replace(",", "").replace(".", "").isdigit()}


def check(reply: str, evidence: str, merchant_text: str) -> FactCheck:
    """
    Args:
        reply: the AI's reply to the merchant
        evidence: everything the tools returned (and were called with), this turn and earlier
        merchant_text: everything the merchant said in this conversation
    """
    issues = []
    known_ids = {_norm_id(m) for m in ID_RE.findall(evidence + " " + merchant_text)}
    # The prompt asks the agent to quote the RBI rate (Rs 100/day), so that figure is always known
    known_amounts = _numbers(evidence) | _numbers(merchant_text) | {settings.tat_compensation_per_day}

    for raw in ID_RE.findall(reply):
        if _norm_id(raw) not in known_ids:
            issues.append(f"'{raw}' does not appear in any tool result")

    for a, b in AMOUNT_RE.findall(reply):
        value = int(float((a or b).replace(",", "")))
        if value not in known_amounts:
            issues.append(f"amount ₹{value:,} does not appear in any tool result")

    for sentence in re.split(r"[.!?।\n]+", reply):
        if ARRIVAL_RE.search(sentence) and not CREDITED_RE.search(evidence):
            issues.append("says the money has arrived/been credited, but no bank or credit result confirms it "
                          "(only say when it is expected)")
        for name, (claim, proof) in ACTION_CLAIMS.items():
            if claim.search(sentence) and not re.search(proof, evidence):
                issues.append(f"says a {name} was done, but no {name} action ran")

    # A reply that names a payment link must state that link's own amount (not another figure it knows)
    link_amounts = {link_id.upper(): int(amount) for link_id, amount in LINK_AMOUNT_RE.findall(evidence)}
    stated = {int(float((a or b).replace(",", ""))) for a, b in AMOUNT_RE.findall(reply)}
    for raw in re.findall(r"PAY-[A-Z0-9]{4,}", reply, re.I):
        actual = link_amounts.get(raw.upper())
        if actual is not None and stated and actual not in stated:
            issues.append(f"says {raw} is for ₹{', ₹'.join(f'{s:,}' for s in sorted(stated))} "
                          f"but that link is for ₹{actual:,}")

    if LEAK_RE.search(reply):
        issues.append("contains internal details (JSON, ids, risk score, queue or tool names)")

    return FactCheck(ok=not issues, issues=list(dict.fromkeys(issues)))
