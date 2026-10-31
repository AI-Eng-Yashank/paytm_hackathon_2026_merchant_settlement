"""
Offline scripted planner with the same interface as SarvamLLMService.

Used when SARVAM_API_KEY is not set: lets the dashboard, tools, guardrails and audit log be
tested end to end, and is a fallback if the venue Wi-Fi fails during the demo.
It follows the same playbook as the system prompt, deterministically.
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field


@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Function
    type: str = "function"


@dataclass
class _Message:
    content: str | None = None
    tool_calls: list = field(default_factory=list)
    reasoning_content: str | None = None


def _call(name: str, reason: str, **args) -> _Message:
    tc = _ToolCall(id=f"call_{uuid.uuid4().hex[:8]}",
                   function=_Function(name, json.dumps({**args, "reason": reason}, ensure_ascii=False)))
    return _Message(tool_calls=[tc], reasoning_content=reason)


def _history(messages: list) -> dict:
    """Map tool name -> (args, result) for every tool already called."""
    calls, done = {}, {}
    for m in messages:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                calls[tc["id"]] = (tc["function"]["name"], json.loads(tc["function"]["arguments"]))
        elif m["role"] == "tool" and m["tool_call_id"] in calls:
            name, args = calls[m["tool_call_id"]]
            done[name] = (args, json.loads(m["content"]))
    return done


class ScriptedLLMService:
    """Deterministic stand-in for the LLM."""

    def __init__(self, delay_s: float = 0.35):
        # Small pause per step so the dashboard animations are visible
        self.delay_s = delay_s

    async def chat(self, messages: list, tools: list) -> _Message:
        await asyncio.sleep(self.delay_s)
        # The latest merchant message (earlier turns and the facts note come before it)
        text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        done = _history(messages)
        res = {k: v[1] for k, v in done.items()}

        # 1. Identify the transaction
        m = re.search(r"TXN\d+", text, re.I)
        txn_id = m.group(0).upper() if m else None
        if not txn_id and "find_txn_by_amount_time" not in res:
            amount = re.search(r"₹\s?([\d,]+)", text)
            if not amount:
                return _Message(content="Kripya transaction id, ya payment ka amount aur samay bataiye, taaki main check kar sakoon.")
            days = 1 if re.search(r"\bkal\b", text, re.I) else 0
            d = re.search(r"(\d+)\s*din", text)
            days = int(d.group(1)) if d else days
            return _call("find_txn_by_amount_time", "Merchant ne txn id nahi diya, amount aur din se transaction dhoondh raha hoon.",
                         amount=float(amount.group(1).replace(",", "")), days_ago=days)
        if not txn_id:
            matches = res["find_txn_by_amount_time"].get("matches", [])
            if not matches:
                return _Message(content="Is amount ka koi payment nahi mila. Kripya transaction id bataiye.")
            txn_id = matches[0]["txn_id"]

        # 2. Check facts
        if "check_payment_status" not in res:
            return _call("check_payment_status", f"{txn_id} ka payment status aur duplicate debit check kar raha hoon.", txn_id=txn_id)
        pay = res["check_payment_status"]
        if pay.get("error"):
            return _Message(content=f"{txn_id} aapke account me nahi mila. Kripya sahi transaction id bataiye.")

        if pay.get("is_duplicate_debit") and "reverse_duplicate_debit" not in res:
            return _call("reverse_duplicate_debit", "Same payer se same amount do baar debit hua hai, duplicate wapas karna hai.", txn_id=txn_id)

        if not pay.get("is_duplicate_debit") and "get_settlement_status" not in res:
            return _call("get_settlement_status", "Settlement status, UTR aur RBI TAT check kar raha hoon.", txn_id=txn_id)
        st = res.get("get_settlement_status", {})

        # 3. Act
        if st.get("settlement_status") == "FAILED" and "retry_settlement" not in res:
            return _call("retry_settlement", f"Settlement fail hua tha ({st.get('failure_reason')}), dobara bhej raha hoon.", txn_id=txn_id)
        if st.get("settlement_status") == "PENDING" and st.get("utr") and "trigger_bank_reconciliation" not in res:
            return _call("trigger_bank_reconciliation", "Settlement pending hai, bank se UTR confirm kar raha hoon.", utr=st["utr"])
        bank = res.get("trigger_bank_reconciliation", {})
        if bank.get("bank_status") == "NOT_FOUND" and st.get("tat_breached") and "issue_instant_credit" not in res:
            return _call("issue_instant_credit", f"Bank me credit nahi mila aur RBI TAT {st['days_since_payment']} din se breach hai.",
                         txn_id=txn_id, amount=pay["amount"])
        credit = res.get("issue_instant_credit", {})
        blocked = any(r.get("status") == "BLOCKED_BY_GUARDRAIL" for r in res.values())
        if blocked and "escalate_to_human" not in res:
            why = next(r["why"] for r in res.values() if r.get("status") == "BLOCKED_BY_GUARDRAIL")
            return _call("escalate_to_human", "Guardrail ne action block kiya, specialist ko bhej raha hoon.",
                         summary=f"{txn_id} ₹{pay['amount']:,}: blocked - {'; '.join(why)}")

        # 4. Reply
        amt = f"₹{pay['amount']:,}"
        comp = st.get("compensation_due", 0)
        comp_line = f" RBI niyam ke hisaab se ₹{comp} compensation bhi milega." if comp else ""
        if blocked:
            reply = "Aapka ticket humare specialist ko bhej diya gaya hai. Woh 24 ghante me aapse sampark karenge."
        elif "reverse_duplicate_debit" in res:
            r = res["reverse_duplicate_debit"]
            reply = (f"Aapke customer se {amt} do baar kata gaya tha. Duplicate {amt} {r['refund_to']} par wapas bhej diya hai, "
                     f"refund ref {r['refund_ref']}. Paisa T+1 tak unke account me aa jayega.")
        elif "retry_settlement" in res:
            r = res["retry_settlement"]
            reply = f"Aapka {amt} ka settlement dobara bhej diya gaya hai, naya UTR {r['new_utr']}. Paisa 2 ghante me aapke account me aa jayega."
        elif credit.get("status") == "PENDING_HUMAN_APPROVAL":
            reply = (f"Aapka {amt} ka settlement {st['days_since_payment']} din se atka hai. Instant credit specialist approval me hai, "
                     f"kuch hi minute me confirm ho jayega.{comp_line}")
        elif credit.get("status") == "CREDITED":
            reply = f"Aapke {amt} ka instant credit kar diya gaya hai, credit id {credit['credit_id']}.{comp_line}"
        elif bank.get("bank_status") == "CREDITED":
            reply = (f"Aapka {amt} {bank['merchant_bank']} account me credit ho chuka hai, UTR {st['utr']}. "
                     f"Kripya apna bank statement check kar lijiye.")
        elif st.get("settlement_status") in ("SETTLED", "CREDITED_INSTANT"):
            reply = f"Aapka {amt} ka settlement ho chuka hai, UTR {st.get('utr')}. Kripya apna bank statement check kar lijiye."
        else:
            reply = f"Aapka {amt} ka settlement process me hai aur T+5 tak aa jayega. Hum isse track kar rahe hain."
        return _Message(content=reply)
