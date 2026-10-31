"""
Live checks against the real Neon database and Sarvam API, using the keys in .env.
Skipped unless RUN_LIVE=1. Run these at the venue before the demo:

    RUN_LIVE=1 .venv/bin/python -m pytest -m live -v

They only read from Neon (no writes) and make a handful of small Sarvam calls.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine, text

from config import settings

ENV = dotenv_values(Path(__file__).parent.parent / ".env")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to run live checks"),
]


@pytest.fixture
def sarvam_key(monkeypatch):
    key = (ENV.get("SARVAM_API_KEY") or "").strip()
    if not key:
        pytest.skip("no SARVAM_API_KEY in .env")
    monkeypatch.setattr(settings, "sarvam_api_key", key)
    return key


def test_neon_is_in_singapore_and_fast():
    from storage.db import _neon_url
    url = (ENV.get("DATABASE_URL") or "").strip()
    if not url:
        pytest.skip("no DATABASE_URL in .env")
    assert "ap-southeast-1" in url, "Neon project is not in Singapore; latency will be 300ms+ per round trip"
    engine = create_engine(_neon_url(url), connect_args={"connect_timeout": 10})
    with engine.connect() as c:
        c.execute(text("select 1"))
        times = []
        for _ in range(5):
            start = time.perf_counter()
            c.execute(text("select 1"))
            times.append((time.perf_counter() - start) * 1000)
        merchants = c.execute(text("select count(*) from merchants")).scalar()
    engine.dispose()
    assert sorted(times)[2] < 200, f"median round trip {sorted(times)[2]:.0f}ms"
    assert merchants == 49


def test_sarvam_calls_the_right_tool_from_hinglish(sarvam_key):
    from agent.prompts import SYSTEM_PROMPT
    from agent.tools import TOOL_SCHEMAS
    from services.llm_service import SarvamLLMService
    msg = asyncio.run(SarvamLLMService().chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "[Merchant M1001: Sharma General Store, Jaipur]\n"
                                    "Mera kal ka ₹4,500 ka settlement abhi tak nahi aaya. Txn id TXN240001 hai."},
    ], TOOL_SCHEMAS))
    assert msg.tool_calls, f"expected a tool call, got text: {msg.content}"
    assert msg.tool_calls[0].function.name in {"check_payment_status", "get_settlement_status"}
    assert "TXN240001" in msg.tool_calls[0].function.arguments


def test_sarvam_voice_round_trip(sarvam_key):
    from services import SarvamSTTService, SarvamTTSService

    async def run():
        wav = await SarvamTTSService().generate_audio("Mera settlement kab aayega?", "hi-IN")
        assert wav[:4] == b"RIFF" and len(wav) > 10_000
        transcript, lang = await SarvamSTTService().transcribe(wav, "audio.wav")
        return transcript, lang

    transcript, lang = asyncio.run(run())
    assert lang.startswith("hi") or lang.startswith("en")
    assert transcript.strip()
