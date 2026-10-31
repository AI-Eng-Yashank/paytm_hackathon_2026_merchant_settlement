"""
LLM Service using Sarvam-105B.
Handles chat completions with function calling support.
"""

import httpx
from sarvamai import AsyncSarvamAI

from config import settings
from agent.timing import log_error


class SarvamLLMService:
    """
    LLM service using Sarvam-105B with tool calling.
    Returns the assistant message (content, tool_calls, reasoning_content).
    """

    def __init__(self):
        """Initialize Sarvam client with API key from config."""
        self.client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)
        self.model = settings.llm_model

    async def chat(self, messages: list, tools: list):
        """
        Create a non-streaming chat completion.

        Each attempt times out after settings.llm_timeout_s; timeouts and connection errors are
        retried settings.llm_retries times (the SDK's own retries don't cover timeouts).

        Args:
            messages: OpenAI-style message dicts
            tools: OpenAI-style tool definitions

        Returns:
            The assistant message object (.content, .tool_calls, .reasoning_content)
        """
        for attempt in range(settings.llm_retries + 1):
            try:
                response = await self.client.chat.completions(
                    messages=messages,
                    model=self.model,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.2,
                    seed=7,
                    # Reasoning tokens count toward max_tokens; keep headroom so content isn't None
                    max_tokens=4096,
                    reasoning_effort=None if settings.reasoning_effort in ("off", "none", "") else settings.reasoning_effort,
                    request_options={"timeout_in_seconds": settings.llm_timeout_s},
                )
                return response.choices[0].message
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt == settings.llm_retries:
                    raise
                log_error("AI call failed, retrying", e,
                          detail=f"attempt {attempt + 1}/{settings.llm_retries + 1}, timeout {settings.llm_timeout_s}s")
