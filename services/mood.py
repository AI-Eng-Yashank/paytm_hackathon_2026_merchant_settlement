"""
Merchant mood per conversation: calm, worried or angry, with a one-line reason.

Uses one small Sarvam call when a key is set; otherwise (or if that call fails) a keyword
heuristic for Hinglish/Hindi/English, so the customer dashboard always has a value.
"""

import json
import re

from config import settings
from agent.timing import log_error


MOODS = ("calm", "worried", "angry")

ANGRY = ["fraud", "bakwas", "chor", "cheat", "complaint", "consumer court", "police", "worst", "pagal",
         "ganda", "bekar service", "kab tak", "har baar", "phir se", "!!!", "धोखा", "शिकायत", "बकवास"]
WORRIED = ["urgent", "problem", "pareshan", "atka", "atak", "nahi aaya", "nahi aya", "kab aayega", "turant",
           "jaldi", "bahut", "din ho gaye", "help", "please", "dikkat", "परेशान", "नहीं आया", "कब आएगा", "जल्दी"]

PROMPT = ("You rate the mood of an Indian merchant in a payment-support chat. Read only the merchant's messages. "
          'Answer with JSON only: {"mood": "calm" | "worried" | "angry", "reason": "<max 12 words, English>"}. '
          "worried = anxious about money or delays; angry = frustrated, accusing, threatening, repeated complaints.")


def heuristic_mood(texts: list[str]) -> tuple[str, str]:
    joined = " ".join(texts).lower()
    angry = [w for w in ANGRY if w in joined]
    worried = [w for w in WORRIED if w in joined]
    if angry or len(texts) >= 4:
        return "angry", f"keywords: {', '.join(angry[:3])}" if angry else f"{len(texts)} messages in one conversation"
    if worried:
        return "worried", f"keywords: {', '.join(worried[:3])}"
    return "calm", "no stress signals"


async def rate_mood(merchant_texts: list[str], allow_llm: bool = True) -> tuple[str, str]:
    """Returns (mood, reason). allow_llm=False uses the keyword rating (e.g. for the 50-ticket batch)."""
    if not merchant_texts:
        return "calm", "no messages"
    if settings.offline or not allow_llm:
        return heuristic_mood(merchant_texts)
    try:
        from sarvamai import AsyncSarvamAI
        client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)
        response = await client.chat.completions(
            messages=[{"role": "system", "content": PROMPT},
                      {"role": "user", "content": "\n".join(f"- {t}" for t in merchant_texts[-6:])}],
            model=settings.llm_model, temperature=0, max_tokens=200, reasoning_effort=None,
            request_options={"timeout_in_seconds": settings.llm_timeout_s},
        )
        raw = response.choices[0].message.content or ""
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        mood = str(data.get("mood", "")).lower()
        if mood in MOODS:
            return mood, str(data.get("reason", ""))[:120]
        raise ValueError(f"unexpected mood {raw!r}")
    except Exception as e:
        log_error("mood rating (using keyword fallback)", e)
        return heuristic_mood(merchant_texts)
