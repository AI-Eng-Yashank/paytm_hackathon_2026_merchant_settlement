"""
The SettlementResolver agent loop.

ticket -> LLM proposes a tool call -> guardrail gate -> execute / queue for approval / block
       -> result back to LLM -> ... -> final reply to merchant

Every step is broadcast to the dashboard and written to the audit log.
"""

import asyncio
import json
import re
import time
from typing import Awaitable, Callable

from config import settings
from storage import db
from mock_systems.store import MockStore
from agent import factcheck, guardrails
from agent.audit import AuditStore
from agent.prompts import SYSTEM_PROMPT
from agent.timing import Timeline
from agent.tools import TOOL_SCHEMAS, TOOL_SYSTEM, execute_tool


Emit = Callable[[dict], Awaitable[None]]

RECOVERY_TOOLS = {"retry_settlement", "reverse_duplicate_debit", "issue_instant_credit"}


def _found_something(tool: str, result: dict) -> bool:
    """A lookup that actually found the merchant's transaction(s), or an action that ran."""
    if not isinstance(result, dict) or result.get("error"):
        return False
    if tool == "find_txn_by_amount_time":
        return bool(result.get("matches"))
    if tool == "list_recent_transactions":
        return bool(result.get("transactions"))
    if tool == "trigger_bank_reconciliation":
        return result.get("bank_status") != "NOT_FOUND" or "merchant_bank" in result
    return tool in {"check_payment_status", "get_settlement_status"} | RECOVERY_TOOLS


class Resolver:
    """Runs one ticket through the agent loop."""

    def __init__(self, llm, store: MockStore, audit: AuditStore, emit: Emit):
        self.llm = llm
        self.store = store
        self.audit = audit
        self.emit = emit

    async def resolve(self, ticket_id: str, merchant_id: str, text: str, language: str = "hi-IN",
                      timeline: Timeline | None = None, history: list[dict] | None = None,
                      conversation_id: str | None = None) -> dict:
        """
        Resolve one merchant turn.

        Args:
            history: earlier turns of this conversation as chat messages (memory)

        Returns:
            {"ticket_id", "status", "reply", "steps", "duration_ms", "facts"}
            facts: tools used and key results, stored with the reply for the next turn's memory
        """
        started = time.time()
        tl = timeline or Timeline(ticket_id, "ticket", self.emit)
        tl.mark("agent start", f"{merchant_id}: {text}" + (f" ({len(history)} earlier messages)" if history else ""))
        self.audit.open_ticket(ticket_id, merchant_id, text, language, conversation_id)
        self.audit.log(ticket_id, "ticket_received", reason=text, actor="merchant")
        await self.emit({"type": "ticket_start", "ticket_id": ticket_id, "merchant_id": merchant_id, "text": text,
                         "conversation_id": conversation_id})

        merchant = self.store.lookup_merchant(merchant_id)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *(history or []),
            {"role": "user", "content": f"[Merchant {merchant_id}: {merchant.get('name')}, {merchant.get('city')}]\n{text}"},
        ]

        steps, recovered = 0, 0.0
        awaiting, escalated = False, False
        # Did a tool find the merchant's transaction(s), now or earlier in this conversation?
        # (a follow-up answered from earlier facts is resolved, not "needs info")
        found_facts = bool(history) and any(m["role"] == "system" and "Facts your tools found" in m["content"]
                                            for m in history)
        facts: list[dict] = []
        reply = None
        # Fact-check inputs: everything tools returned (this turn and earlier), and what the merchant said
        evidence = [m["content"] for m in history or [] if m["role"] == "system"]
        merchant_said = " ".join([m["content"] for m in history or [] if m["role"] == "user"] + [text])
        rewrites_left = 1

        for llm_round in range(1, settings.max_agent_steps + 1):
            await self.emit({"type": "thinking", "ticket_id": ticket_id})
            try:
                with tl.span(f"AI call #{llm_round}", f"{len(messages)} messages in context") as span:
                    msg = await self.llm.chat(messages, TOOL_SCHEMAS)
                    span.detail = (f"→ tools: {', '.join(tc.function.name for tc in msg.tool_calls)}"
                                   if msg.tool_calls else f"→ reply ({len(msg.content or '')} chars)")
            except Exception as e:
                # The span already wrote the traceback to logs/errors.log
                await self.emit({"type": "error", "ticket_id": ticket_id, "detail": f"LLM error: {e}"})
                self.audit.log(ticket_id, "llm_error", reason=str(e))
                escalated = True
                reply = "Maaf kijiye, abhi system me dikkat hai. Aapka ticket specialist ko bhej diya gaya hai, woh jaldi call karenge."
                break

            if msg.reasoning_content:
                await self.emit({"type": "reasoning", "ticket_id": ticket_id, "text": msg.reasoning_content[:600]})

            if not msg.tool_calls:
                # Safety net: internal notes must never reach the merchant
                candidate = re.split(r"\[\s*Facts (?:from tools|your tools)", msg.content or "")[0].strip()
                fc = factcheck.check(candidate, "\n".join(evidence), merchant_said)
                tl.mark("fact-check", "PASS" if fc.ok else "FAIL: " + "; ".join(fc.issues))
                self.audit.log(ticket_id, "fact_check", reason="; ".join(fc.issues) or "all claims backed by tools",
                               decision="PASS" if fc.ok else "FAIL", result={"reply": candidate})
                await self.emit({"type": "factcheck", "ticket_id": ticket_id, "ok": fc.ok, "issues": fc.issues,
                                 "rewrite": rewrites_left == 0})
                if fc.ok:
                    reply = candidate
                    break
                if rewrites_left:
                    # One chance to rewrite with the problems spelled out
                    rewrites_left -= 1
                    messages.append({"role": "assistant", "content": candidate})
                    messages.append({"role": "system", "content":
                                     "Fact-check (internal) rejected that reply: " + "; ".join(fc.issues) +
                                     ". Fix it: if you want to tell the merchant the money has arrived, first call "
                                     "trigger_bank_reconciliation with the UTR to confirm it (SETTLED only means it "
                                     "was sent to the bank). Otherwise say when it is expected. Use only facts your "
                                     "tools returned, and don't mention this check."})
                    continue
                # Still wrong after a rewrite: send a safe reply and let a human review the ticket
                reply = factcheck.SAFE_REPLY
                escalated = True
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })

            for tc in msg.tool_calls:
                steps += 1
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                reason = args.pop("reason", "")
                system = TOOL_SYSTEM.get(name, "crm")

                await self.emit({"type": "tool_call", "ticket_id": ticket_id, "tool": name, "system": system,
                                 "args": args, "reason": reason})

                # Guardrail + tool share one DB connection and run off the event loop
                with tl.span(f"tool {name}", json.dumps(args, ensure_ascii=False)) as span:
                    gate, result = await asyncio.to_thread(self._gate_and_run, merchant_id, name, args)
                    span.detail = (f"{gate.decision} → " + json.dumps(result, ensure_ascii=False)
                                   if result is not None else f"{gate.decision}: {'; '.join(gate.reasons)}")
                if name in RECOVERY_TOOLS:
                    tl.mark(f"guardrail {name}", f"{gate.decision} {'; '.join(gate.reasons)}")
                    await self.emit({"type": "guardrail", "ticket_id": ticket_id, "tool": name,
                                     "decision": gate.decision, "reasons": gate.reasons})

                if gate.decision == "ALLOW":
                    if name in RECOVERY_TOOLS:
                        recovered += float(result.get("amount", 0))
                    if name == "escalate_to_human":
                        escalated = True
                    if _found_something(name, result):
                        found_facts = True
                    facts.append({"tool": name, "args": args, "result": result})
                elif gate.decision == "NEEDS_APPROVAL":
                    approval = await asyncio.to_thread(self._create_approval, ticket_id, merchant_id, name,
                                                       args, gate.reasons, reason)
                    awaiting = True
                    result = {"status": "PENDING_HUMAN_APPROVAL", "approval_id": approval["id"], "why": gate.reasons}
                    await self.emit({"type": "approval_created", "ticket_id": ticket_id, "approval": approval})
                else:
                    escalated = True
                    result = {"status": "BLOCKED_BY_GUARDRAIL", "why": gate.reasons}

                self.audit.log(ticket_id, "tool_call", tool=name, args=args, result=result,
                               reason=reason, decision=gate.decision)
                evidence.append(json.dumps({"tool": name, "args": args, "result": result}, ensure_ascii=False,
                                           default=str))
                await self.emit({"type": "tool_result", "ticket_id": ticket_id, "tool": name, "system": system,
                                 "result": result, "decision": gate.decision})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})

        if not reply:
            escalated = True
            reply = "Aapka ticket humare specialist ko bhej diya gaya hai. Woh jaldi aapse sampark karenge."

        # RESOLVED only when the agent actually found the merchant's transaction(s); a reply that just
        # asks a question (or promises to look) is NEEDS_INFO, so it doesn't inflate the numbers
        status = ("AWAITING_APPROVAL" if awaiting else "ESCALATED" if escalated
                  else "RESOLVED" if found_facts else "NEEDS_INFO")
        tl.mark("agent done", f"{status}, {steps} tool calls")
        self.audit.close_ticket(ticket_id, status, reply=reply, steps=steps, recovered=recovered,
                                human=awaiting or escalated)
        self.audit.log(ticket_id, "reply_sent", reason=reply, decision=status)

        result = {"ticket_id": ticket_id, "status": status, "reply": reply, "steps": steps,
                  "duration_ms": int((time.time() - started) * 1000), "conversation_id": conversation_id}
        await self.emit({"type": "ticket_done", **result})
        result["facts"] = facts
        # KPI tiles update once the audit writes land; the merchant's reply doesn't wait for that
        asyncio.create_task(self._emit_metrics())
        return result

    async def _emit_metrics(self):
        await self.emit({"type": "metrics", "metrics": await asyncio.to_thread(self.audit.metrics)})

    def _gate_and_run(self, merchant_id: str, name: str, args: dict):
        """Runs in a worker thread: guardrail check, then the tool if allowed, on one connection."""
        with db.session():
            gate = guardrails.check(self.store, merchant_id, name, args)
            result = execute_tool(self.store, merchant_id, name, args) if gate.decision == "ALLOW" else None
        return gate, result

    def _create_approval(self, ticket_id, merchant_id, name, args, reasons, agent_reason) -> dict:
        with db.session():
            # One pending approval per transaction: a repeat ticket joins the existing request
            existing = self.audit.find_pending_approval(merchant_id, name, args.get("txn_id", ""))
            if existing:
                return existing
            approval_id = self.audit.create_approval(ticket_id, merchant_id, name, args, reasons, agent_reason)
            return self.audit.get_approval(approval_id)
