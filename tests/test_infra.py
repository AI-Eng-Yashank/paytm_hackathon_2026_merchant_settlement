"""Database layer, audit writer and the Sarvam retry wrapper."""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, insert, select

from config import settings
from storage import db


def test_neon_url_gets_psycopg_driver():
    assert db._neon_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert db._neon_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"


def test_unreachable_neon_falls_back_to_sqlite(monkeypatch, log_dir):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@127.0.0.1:1/nodb")
    engine, backend, host = db._make_engine()
    assert backend == "sqlite"
    engine.dispose()
    assert "Neon unreachable, using local SQLite instead" in (log_dir / "errors.log").read_text()


def test_mock_data_reseeded_on_a_new_day(store):
    store.retry_settlement("M1001", "TXN240001")
    with db.engine.connect() as c:
        c.execute(delete(db.meta).where(db.meta.c.key == "seeded_on"))
        c.execute(insert(db.meta).values(key="seeded_on", value=(date.today() - timedelta(days=1)).isoformat()))
    db.init_db()
    assert store.get_settlement_status("M1001", "TXN240001")["settlement_status"] == "FAILED"


def test_session_shares_one_connection():
    with db.session() as outer:
        with db.use() as inner:
            assert inner is outer


def test_audit_writer_keeps_order_and_loses_nothing(audit):
    for i in range(25):
        audit.log("ORDER-1", "tool_call", tool=f"t{i}", reason=f"step {i}")
    rows = audit.list_audit("ORDER-1")
    assert [r["tool"] for r in rows] == [f"t{i}" for i in range(25)]


def test_audit_reads_see_queued_writes(audit):
    audit.open_ticket("Q-1", "M1001", "hello", "hi-IN")
    audit.close_ticket("Q-1", "RESOLVED", reply="done", steps=1)
    t = audit.get_ticket("Q-1")
    assert t["status"] == "RESOLVED" and t["reply"] == "done" and t["duration_ms"] is not None


# ---------- Sarvam call: timeout + one retry ----------

def _service(monkeypatch, behaviours):
    from services.llm_service import SarvamLLMService
    svc = SarvamLLMService()
    calls = []

    async def fake_completions(**kwargs):
        calls.append(kwargs["request_options"])
        b = behaviours[len(calls) - 1]
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(choices=[SimpleNamespace(message=b)])

    monkeypatch.setattr(svc.client.chat, "completions", fake_completions)
    return svc, calls


def test_llm_retries_once_after_timeout(monkeypatch):
    svc, calls = _service(monkeypatch, [httpx.ReadTimeout(""), "ok-message"])
    assert asyncio.run(svc.chat([], [])) == "ok-message"
    assert len(calls) == 2
    assert calls[0] == {"timeout_in_seconds": settings.llm_timeout_s}


def test_llm_gives_up_after_retries(monkeypatch):
    svc, calls = _service(monkeypatch, [httpx.ReadTimeout(""), httpx.ReadTimeout("")])
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(svc.chat([], []))
    assert len(calls) == settings.llm_retries + 1


def test_llm_does_not_retry_bad_requests(monkeypatch):
    svc, calls = _service(monkeypatch, [ValueError("400 bad request")])
    with pytest.raises(ValueError):
        asyncio.run(svc.chat([], []))
    assert len(calls) == 1
