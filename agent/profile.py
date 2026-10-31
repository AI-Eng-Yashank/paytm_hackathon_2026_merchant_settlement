"""
An agent profile: everything that makes one job different from another (instructions, tools,
guardrails, wording). The agent loop in resolver.py is shared, so every job gets the same
guardrail gate, fact-check, audit trail, approvals, memory and timing log.

Two profiles exist: settlement support (merchant <-> Paytm) and collections (merchant A's AI
collecting what merchant B owes A).
"""

from dataclasses import dataclass, field
from typing import Callable

from agent.guardrails import GateResult


@dataclass
class AgentProfile:
    name: str
    system_prompt: str
    tools: list
    tool_system: dict                                   # tool -> box on the dashboard's system map
    check: Callable[[str, str, dict], GateResult]       # (actor_id, tool, args) -> decision, in code
    execute: Callable[[str, str, dict], dict]           # (actor_id, tool, args) -> result
    user_prefix: Callable[[str], str]                   # header line before the speaker's message
    found: Callable[[str, dict], bool]                  # did this tool result settle the question?
    action_tools: set = field(default_factory=set)      # guardrail decision shown on the dashboard
    recovery_tools: set = field(default_factory=set)    # their "amount" counts as money recovered
    escalate_tools: set = field(default_factory=lambda: {"escalate_to_human"})
    rewrite_hint: str = ""                              # extra advice when the fact-check rejects a draft
    fallback_reply: str = "Aapka ticket humare specialist ko bhej diya gaya hai. Woh jaldi aapse sampark karenge."
    error_reply: str = ("Maaf kijiye, abhi system me dikkat hai. Aapka ticket specialist ko bhej diya gaya hai, "
                        "woh jaldi call karenge.")
    llm: object | None = None                           # overrides the resolver's default LLM
    facts: str = ""                                     # known facts given in the instructions (fact-check evidence)
