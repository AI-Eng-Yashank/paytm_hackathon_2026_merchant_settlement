"""
Collections teammate: merchant A's AI calls merchant B about an invoice B owes A.

Same agent loop, guardrails, fact-check, recordings and transcripts as settlement support;
this module only supplies what is different: the instructions, five tools, the guardrails for
them, the call's opening line, reminder drafting, and an offline planner.

Rules it follows: says it is an AI calling on A's behalf and that the call is recorded; confirms
it is speaking to B before mentioning amounts; never threatens or pressures; offers only what A
allows (full payment, or instalments if A set them); disputes go back to A, not argued.
"""

import json
import re
from datetime import date, timedelta

from config import settings
from agent import factcheck
from agent.guardrails import GateResult
from agent.profile import AgentProfile
from agent.tools import _tool
from mock_systems.collections_store import CollectionsStore
from services.scripted_llm import _Message, _call, _history


TOOLS = [
    _tool("get_invoice", "Invoice status: amount, paid, outstanding, due date, instalments allowed, payment links.", {}, []),
    _tool("send_payment_link",
          "Send merchant B a UPI payment link, only after they agreed to pay now. amount = the full outstanding "
          "amount, or the first instalment they agreed to if max_instalments > 1 (at least min_instalment).",
          {"amount": {"type": "integer", "description": "Rupees, exactly what merchant B agreed to pay now"}},
          ["amount"]),
    _tool("record_promise_to_pay", "Record the date (and amount) merchant B promises to pay by.",
          {"date": {"type": "string", "description": "YYYY-MM-DD"}, "amount": {"type": "integer"}}, ["date"]),
    _tool("record_dispute", "Merchant B disputes the invoice (already paid, wrong amount, goods issue). "
          "Records it and hands it to merchant A. Don't argue.", {"dispute": {"type": "string"}}, ["dispute"]),
    _tool("notify_creditor", "Pass a request you can't handle (discount, waiver, callback) to merchant A.",
          {"summary": {"type": "string"}}, ["summary"]),
]
TOOL_SYSTEM = {"get_invoice": "ledger", "send_payment_link": "payments", "record_promise_to_pay": "crm",
               "record_dispute": "crm", "notify_creditor": "crm"}
ACTION_TOOLS = {"send_payment_link", "record_promise_to_pay"}


def greeting(view: dict) -> str:
    """The call's first line: who is calling, that it's recorded, and a check that it's B. No amounts yet."""
    return (f"Namaste! Main {view['creditor']} ki taraf se unki AI assistant bol rahi hoon. "
            f"Yeh call record ho rahi hai. Kya meri baat {view['debtor']} se ho rahi hai?")


def system_prompt(view: dict) -> str:
    due = (f"{view['days_overdue']} din overdue (due {view['due_on']})" if view["days_overdue"]
           else f"due on {view['due_on']} (in {view['due_in_days']} days)")
    instalments = (f"A allows up to {view['max_instalments']} instalments; the first must be at least "
                   f"Rs {view['min_instalment']}." if view["max_instalments"] > 1 else "A does not allow instalments.")
    return f"""You are an AI assistant calling on behalf of {view['creditor']} (merchant A) to {view['debtor']}
(merchant B) about an unpaid invoice. You already said who you are, that the call is recorded, and asked if
you are speaking to {view['debtor']}.

Today is {date.today().isoformat()} ({date.today():%A}). Convert relative days ("kal", "parso", "3 din baad",
"next Monday") to a YYYY-MM-DD date from today.

Invoice: {view['invoice_id']}, {view['description']}, Rs {view['outstanding']} outstanding of Rs {view['amount']},
{due}. {instalments} Promises to pay must be within {settings.collections_max_promise_days} days
(by {(date.today() + timedelta(days=settings.collections_max_promise_days)).isoformat()}).

Steps:
1. If the person is not {view['debtor']} (wrong number, someone else), apologise and end politely
   WITHOUT mentioning the amount or invoice.
2. Once confirmed, briefly state the invoice and amount and ASK if they can pay now by UPI link.
   Only after they agree, call send_payment_link with the amount they agreed to (full, or the first
   instalment if allowed). Then tell them the exact amount of THAT link, as the tool returned it.
3. If they can't pay now, ask when they can and record_promise_to_pay with that date.
4. If they dispute it (already paid, wrong amount, goods damaged), record_dispute with their reason and say
   {view['creditor']} will look into it. Don't argue.
5. Anything else (discount, waiver, more time than allowed): notify_creditor and say {view['creditor']} will decide.
6. If they say they have paid via the link, call get_invoice to check before thanking them.

Rules:
- Always polite and respectful. Never threaten, pressure, shame, or mention legal action.
- Never offer a discount. Only offer instalments if allowed above.
- Only state amounts, dates and links your tools returned. Don't say a payment was received unless
  get_invoice shows it.
- Reply in the caller's language (Hinglish in Roman script if they speak Hinglish), 1-3 short sentences,
  suitable to be spoken. No markdown, no emojis.
"""


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def collections_profile(cstore: CollectionsStore, invoice_id: str, llm=None) -> AgentProfile:
    """A profile bound to one invoice: the model can't switch to another invoice or merchant."""
    view = cstore.view(cstore.invoice(invoice_id))

    def check(actor_id: str, name: str, args: dict) -> GateResult:
        inv = cstore.invoice(invoice_id)
        if not inv or inv["creditor_id"] != actor_id:
            return GateResult("BLOCK", ["Invoice not found for this merchant"])
        v = cstore.view(inv)
        if name == "send_payment_link":
            if v["status"] == "PAID":
                return GateResult("BLOCK", ["Invoice is already paid"])
            if v["status"] == "DISPUTED":
                return GateResult("BLOCK", ["Invoice is disputed; merchant A decides what happens next"])
            amount = int(args.get("amount") or v["outstanding"])
            ok_full = amount == v["outstanding"]
            ok_instalment = v["max_instalments"] > 1 and v["min_instalment"] <= amount < v["outstanding"]
            if not (ok_full or ok_instalment):
                allowed = f"the full Rs {v['outstanding']}" + (
                    f" or an instalment of at least Rs {v['min_instalment']}" if v["max_instalments"] > 1 else "")
                return GateResult("BLOCK", [f"Rs {amount} is not allowed: only {allowed} (no discounts)"])
            if cstore.active_links(invoice_id) >= 3:
                return GateResult("BLOCK", ["3 unpaid payment links already sent for this invoice"])
        if name == "record_promise_to_pay":
            when = _parse_date(args.get("date", ""))
            first, last = cstore.promise_window(settings.collections_max_promise_days)
            if v["status"] == "PAID":
                return GateResult("BLOCK", ["Invoice is already paid"])
            if not when:
                return GateResult("BLOCK", ["Give the date as YYYY-MM-DD"])
            if not first <= when <= last:
                return GateResult("BLOCK", [f"Promise date must be between {first} and {last} "
                                            f"({settings.collections_max_promise_days} days); "
                                            f"for more time, notify_creditor"])
            amount = int(args.get("amount") or v["outstanding"])
            if not 0 < amount <= v["outstanding"]:
                return GateResult("BLOCK", [f"Promise amount must be between Rs 1 and Rs {v['outstanding']}"])
        return GateResult("ALLOW")

    def execute(actor_id: str, name: str, args: dict) -> dict:
        inv = cstore.invoice(invoice_id)
        v = cstore.view(inv)
        if name == "get_invoice":
            links = [l for i in cstore.list_invoices(actor_id) if i["invoice_id"] == invoice_id for l in i["links"]]
            return {**v, "payment_links": [{"link_id": l["link_id"], "amount": l["amount"], "status": l["status"]}
                                           for l in links]}
        if name == "send_payment_link":
            link = cstore.create_link(invoice_id, int(args.get("amount") or v["outstanding"]))
            return {"link_id": link["link_id"], "amount": link["amount"],
                    "payment_link": settings.public_base_url + link["url_path"],
                    "instalment": link["amount"] < v["outstanding"]}
        if name == "record_promise_to_pay":
            when, amount = _parse_date(args["date"]), int(args.get("amount") or v["outstanding"])
            cstore.record_promise(invoice_id, when, amount)
            cstore.notify_creditor(inv, f"Promise to pay: {v['debtor']}",
                                   f"{v['debtor']} promised to pay Rs {amount} for {invoice_id} by {when}.")
            return {"status": "PROMISED", "promise_date": when.isoformat(), "amount": amount}
        if name == "record_dispute":
            cstore.record_dispute(invoice_id, args.get("dispute", ""))
            cstore.notify_creditor(inv, f"Dispute: {v['debtor']} on {invoice_id}",
                                   f"{v['debtor']} disputes {invoice_id} (Rs {v['outstanding']}): {args.get('dispute')}")
            return {"status": "DISPUTED", "handed_to": v["creditor"]}
        if name == "notify_creditor":
            cstore.notify_creditor(inv, f"Request from {v['debtor']}", args.get("summary", ""))
            return {"status": "NOTIFIED", "notified": v["creditor"]}
        return {"error": f"UNKNOWN_TOOL {name}"}

    return AgentProfile(
        name="collections", system_prompt=system_prompt(view), tools=TOOLS, tool_system=TOOL_SYSTEM,
        check=check, execute=execute,
        user_prefix=lambda actor_id: f"[{view['debtor']} on the call about {invoice_id}]",
        found=lambda tool, result: isinstance(result, dict) and not result.get("error"),
        action_tools=ACTION_TOOLS, recovery_tools=set(),
        escalate_tools={"record_dispute", "notify_creditor"},
        rewrite_hint="Only mention amounts, dates and payment links your tools returned; say a payment was "
                     "received only if get_invoice shows it.",
        fallback_reply=f"Dhanyavaad. Main yeh baat {view['creditor']} ko bata rahi hoon, woh aapse sampark karenge.",
        error_reply="Maaf kijiye, abhi system me dikkat hai. Hum thodi der baad aapko dobara call karenge.",
        llm=llm,
        facts=json.dumps(view, ensure_ascii=False, default=str),
    )


# ---------- reminder message (WhatsApp/email) in merchant A's name ----------

def reminder_template(view: dict, link_url: str) -> str:
    due = (f"{view['due_on']} ko due tha" if view["days_overdue"] else f"{view['due_on']} ko due hai")
    return (f"Namaste {view['debtor']} ji, {view['creditor']} ki taraf se ek yaad: invoice {view['invoice_id']} "
            f"({view['description']}) ka Rs {view['outstanding']} {due}. Aap is link se UPI se turant pay kar "
            f"sakte hain: {link_url} Dhanyavaad!")


def _right_way_round(draft: str, view: dict) -> bool:
    """Addressed to the debtor and sent by the creditor. (Seen live: a draft greeted the creditor,
    "Hello Sharma ji", about "Mehta Electronics ke invoice": right facts, wrong direction.)"""
    text = draft.lower()
    greets_creditor = re.search(rf"(namaste|hello|hi|dear)\s+{re.escape(view['creditor'].split()[0].lower())}\b", text)
    return (view["debtor"].lower() in text and view["creditor"].lower() in text and not greets_creditor
            and text.find(view["debtor"].lower()) < 60)       # the debtor is named in the greeting


async def draft_reminder(view: dict, link: dict, link_url: str) -> tuple[str, str]:
    """Returns (draft, source). Sarvam writes it when available; the fact-check (or offline mode)
    falls back to the template, so a wrong amount or link can never go out in A's name."""
    template = reminder_template(view, link_url)
    if settings.offline:
        return template, "template"
    try:
        from sarvamai import AsyncSarvamAI
        client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)
        r = await client.chat.completions(
            messages=[{"role": "system", "content":
                       f"Write a short, polite WhatsApp payment reminder in Hinglish (Roman script).\n"
                       f"FROM: {view['creditor']} (the shop that is owed the money; sign off as them).\n"
                       f"TO: {view['debtor']} (the shop that owes it). Start with: Namaste {view['debtor']} ji,\n"
                       "Max 3 sentences, warm and respectful, no pressure or threats, no emojis. "
                       "Use exactly these facts and include the link as given:\n" +
                       json.dumps({**view, "payment_link": link_url}, ensure_ascii=False)},
                      {"role": "user", "content": "Write the message."}],
            model=settings.llm_model, temperature=0.3, max_tokens=400, reasoning_effort=None,
            request_options={"timeout_in_seconds": settings.llm_timeout_s})
        draft = (r.choices[0].message.content or "").strip()
        evidence = json.dumps({**view, **link, "payment_link": link_url}, ensure_ascii=False, default=str)
        if draft and link_url in draft and _right_way_round(draft, view) and factcheck.check(draft, evidence, "").ok:
            return draft, "sarvam"
    except Exception:
        pass
    return template, "template"


# ---------- offline planner (no Sarvam key) ----------

class ScriptedCollectionsPlanner:
    """Deterministic stand-in for the LLM on collections calls, same playbook as the prompt."""

    def __init__(self, cstore: CollectionsStore, invoice_id: str):
        self.cstore, self.invoice_id = cstore, invoice_id

    async def chat(self, messages: list, tools: list) -> _Message:
        text = next(m["content"] for m in reversed(messages) if m["role"] == "user").split("\n", 1)[-1].lower()
        done = {k: v[1] for k, v in _history(messages).items()}
        v = self.cstore.view(self.cstore.invoice(self.invoice_id))

        if "record_dispute" in done:
            return _Message(content=f"Samajh gayi. Maine yeh baat {v['creditor']} ko bata di hai, woh khud aapse baat karenge.")
        if "record_promise_to_pay" in done:
            p = done["record_promise_to_pay"]
            if p.get("status") == "PROMISED":
                return _Message(content=f"Theek hai, maine {p['promise_date']} tak Rs {p['amount']} ka promise note "
                                        f"kar liya hai aur {v['creditor']} ko bata diya hai. Dhanyavaad!")
            return _Message(content=f"Yeh date {settings.collections_max_promise_days} din se zyada hai. "
                                    f"Kya aap thoda jaldi pay kar sakte hain?")
        if "send_payment_link" in done:
            l = done["send_payment_link"]
            if l.get("payment_link"):
                return _Message(content=f"Maine Rs {l['amount']} ka payment link bhej diya hai: {l['payment_link']} "
                                        f"Isse UPI se turant pay kar sakte hain.")
            return _Message(content=f"Yeh amount allowed nahi hai. Poora Rs {v['outstanding']} ka link bhej doon?")
        if "get_invoice" in done:
            g = done["get_invoice"]
            if g["status"] == "PAID":
                return _Message(content=f"Dhanyavaad! Rs {g['paid_amount']} mil gaya, invoice {g['invoice_id']} clear ho gaya hai.")
            return _Message(content=f"Abhi payment dikh nahi raha, Rs {g['outstanding']} baaki hai. Link se pay karke ek baar bata dijiye.")

        if re.search(r"pehle hi|already|kharab|galat|damaged|wrong amount|de diya tha", text):
            return _call("record_dispute", "Merchant B disputes the invoice.", dispute=text[:200])
        if re.search(r"pay kar diya|payment kar di|paid|ho gaya", text):
            return _call("get_invoice", "Merchant B says they paid; checking before thanking them.")
        if re.search(r"\bkal\b|parso|\d+\s*din|next week|tomorrow|baad me|tak de", text):
            days = 1 if re.search(r"\bkal\b|tomorrow", text) else 2 if "parso" in text else 3
            d = re.search(r"(\d+)\s*din", text)
            days = int(d.group(1)) if d else days
            return _call("record_promise_to_pay", "Merchant B can't pay now and gave a date.",
                         date=(date.today() + timedelta(days=days)).isoformat(), amount=v["outstanding"])
        if re.search(r"link|bhej|pay|aadha|half|kist|instal", text):
            half = re.search(r"aadha|half|kist|instal", text) and v["max_instalments"] > 1
            return _call("send_payment_link", "Merchant B agreed to pay now.",
                         amount=v["min_instalment"] if half else v["outstanding"])
        if re.search(r"\bhaan\b|\bji\b|\byes\b|bol raha|bol rahi|speaking|\bhan\b", text):
            due = f"{v['days_overdue']} din pehle due tha" if v["days_overdue"] else f"{v['due_on']} ko due hai"
            return _Message(content=f"Dhanyavaad. {v['creditor']} ka invoice {v['invoice_id']} ({v['description']}) "
                                    f"Rs {v['outstanding']} ka hai, jo {due}. Kya main abhi aapko UPI payment link bhej doon?")
        return _Message(content=f"Maaf kijiye, kya aap {v['debtor']} se bol rahe hain?")
