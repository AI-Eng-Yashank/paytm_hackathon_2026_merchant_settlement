"""
Database layer: Neon Postgres when DATABASE_URL is set, local SQLite otherwise.

If DATABASE_URL is set but Neon can't be reached at startup (e.g. venue Wi-Fi), the app
falls back to the local SQLite file so the demo still runs; the reason goes to logs/errors.log.

Tables:
  mock systems   merchants, transactions, settlements, bank_records, credits, seed_tickets
  agent records  tickets, audit_log, approvals
  conversations  conversations, messages (for multi-turn memory)
  meta           key/value (e.g. the date the mock data was seeded)
"""

import contextvars
import json
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import (Column, Date, Float, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, delete, insert, inspect, select, text)

from config import settings
from agent.timing import log_error


DATA_DIR = Path(__file__).parent.parent / "data"
SQLITE_PATH = Path(settings.sqlite_path) if settings.sqlite_path else DATA_DIR / "local.db"
SEED_PATH = DATA_DIR / "seed.json"

metadata = MetaData()

# ---------- mock systems ----------

merchants = Table(
    "merchants", metadata,
    Column("merchant_id", String(16), primary_key=True),
    Column("name", String(120)), Column("business", String(60)), Column("city", String(60)),
    Column("kyc_tier", String(8)), Column("account_age_days", Integer), Column("risk_score", Float),
    Column("settlement_bank", String(60)), Column("settlement_cycle", String(8)),
    Column("past_tickets_30d", Integer, default=0),
)

transactions = Table(
    "transactions", metadata,
    Column("txn_id", String(16), primary_key=True),
    Column("merchant_id", String(16), ForeignKey("merchants.merchant_id"), index=True),
    Column("amount", Integer), Column("payment_date", Date), Column("time", String(5)),
    Column("payer_vpa", String(80)), Column("status", String(16)),
    Column("duplicate_of", String(16), nullable=True, index=True),
)

settlements = Table(
    "settlements", metadata,
    Column("txn_id", String(16), ForeignKey("transactions.txn_id"), primary_key=True),
    Column("status", String(20)), Column("utr", String(20), nullable=True, index=True),
    Column("failure_reason", String(120), nullable=True),
)

bank_records = Table(
    "bank_records", metadata,
    Column("utr", String(20), primary_key=True),
    Column("status", String(16)), Column("amount", Integer),
)

credits = Table(
    "credits", metadata,
    Column("credit_id", String(16), primary_key=True),
    Column("merchant_id", String(16), index=True), Column("txn_id", String(16), index=True),
    Column("amount", Integer), Column("credit_date", Date),
)

seed_tickets = Table(
    "seed_tickets", metadata,
    Column("ticket_id", String(16), primary_key=True),
    Column("merchant_id", String(16)), Column("scenario", String(40)), Column("label", String(80)),
    Column("text", Text), Column("sort_order", Integer),
)

# ---------- agent records ----------

tickets = Table(
    "tickets", metadata,
    Column("ticket_id", String(24), primary_key=True),
    Column("merchant_id", String(16), index=True), Column("text", Text), Column("language", String(8)),
    Column("status", String(24)), Column("reply", Text, nullable=True), Column("steps", Integer, default=0),
    Column("recovered_amount", Float, default=0), Column("human_involved", Integer, default=0),
    Column("created_at", Float), Column("resolved_at", Float, nullable=True),
    Column("duration_ms", Integer, nullable=True),
    Column("conversation_id", String(24), nullable=True, index=True),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float), Column("ticket_id", String(24), index=True), Column("actor", String(60)),
    Column("event", String(40)), Column("tool", String(40), nullable=True),
    Column("args", Text), Column("result", Text), Column("reason", Text), Column("decision", String(24)),
)

approvals = Table(
    "approvals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticket_id", String(24), index=True), Column("merchant_id", String(16)), Column("tool", String(40)),
    Column("args", Text), Column("reasons", Text), Column("agent_reason", Text), Column("status", String(16)),
    Column("created_at", Float), Column("decided_at", Float, nullable=True), Column("decided_by", String(80), nullable=True),
)

# ---------- conversations (multi-turn memory) ----------

conversations = Table(
    "conversations", metadata,
    Column("conversation_id", String(24), primary_key=True),
    Column("merchant_id", String(16), index=True), Column("channel", String(8)),
    Column("status", String(24)), Column("created_at", Float), Column("updated_at", Float),
    Column("mood", String(16), nullable=True), Column("mood_reason", Text, nullable=True),
)

messages = Table(
    "messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("conversation_id", String(24), ForeignKey("conversations.conversation_id"), index=True),
    Column("role", String(16)), Column("content", Text), Column("ticket_id", String(24), nullable=True),
    Column("created_at", Float),
    Column("channel", String(8), nullable=True),        # text | voice
    Column("audio_path", String(200), nullable=True),   # recording, relative to recordings/
    Column("facts", Text, nullable=True),               # agent turns: tools used + key results (JSON), for memory
)

meta = Table(
    "meta", metadata,
    Column("key", String(40), primary_key=True), Column("value", Text),
)

MOCK_TABLES = [credits, bank_records, settlements, transactions, merchants, seed_tickets]  # delete order
AGENT_TABLES = [audit_log, approvals, tickets, messages, conversations]


def _neon_url(url: str) -> str:
    """Neon gives postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg driver named."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _make_engine():
    # AUTOCOMMIT: each statement is one network round trip (no BEGIN / ROLLBACK around it).
    # Matters for Neon, where every round trip costs 50-300ms depending on region.
    url = settings.database_url.strip()
    if url:
        try:
            engine = create_engine(_neon_url(url), isolation_level="AUTOCOMMIT", pool_pre_ping=True,
                                   pool_recycle=300, pool_size=5, max_overflow=10,
                                   connect_args={"connect_timeout": 10})
            with engine.connect() as conn:
                conn.execute(text("select 1"))
            return engine, "neon", urlparse(url).hostname or "neon"
        except Exception as e:
            log_error("database: Neon unreachable, using local SQLite instead", e)
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{SQLITE_PATH}", isolation_level="AUTOCOMMIT",
                           connect_args={"check_same_thread": False})
    return engine, "sqlite", SQLITE_PATH.name


engine, backend, backend_host = _make_engine()

_current_conn: contextvars.ContextVar = contextvars.ContextVar("db_conn", default=None)


@contextmanager
def session():
    """Share one pooled connection across a group of queries (e.g. one agent tool step)."""
    if _current_conn.get() is not None:
        yield _current_conn.get()
        return
    with engine.connect() as conn:
        token = _current_conn.set(conn)
        try:
            yield conn
        finally:
            _current_conn.reset(token)


@contextmanager
def use():
    """The current session's connection, or a fresh pooled one."""
    conn = _current_conn.get()
    if conn is not None:
        yield conn
    else:
        with engine.connect() as c:
            yield c


def _add_missing_columns():
    """create_all() doesn't alter existing tables; add new nullable columns to databases created earlier."""
    existing = inspect(engine)
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            if not existing.has_table(table.name):
                continue
            have = {c["name"] for c in existing.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    col_type = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}'))


def init_db():
    """Create tables if missing and make sure the mock data is seeded for today."""
    metadata.create_all(engine)
    _add_missing_columns()
    with engine.begin() as conn:
        seeded_on = conn.execute(select(meta.c.value).where(meta.c.key == "seeded_on")).scalar()
        has_data = conn.execute(select(merchants.c.merchant_id).limit(1)).first() is not None
    # Payment dates are relative to the day the data was seeded; reseed on a new day so
    # "kal" (yesterday) and RBI TAT ages in the demo scenarios stay correct.
    if not has_data or seeded_on != date.today().isoformat():
        seed_mock_systems()


def seed_mock_systems():
    """Load data/seed.json into the mock-system tables (replacing what is there)."""
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    today = date.today()
    past = data["past_tickets"]
    with engine.begin() as conn:
        for table in MOCK_TABLES:
            conn.execute(delete(table))
        conn.execute(insert(merchants), [{**m, "past_tickets_30d": past.get(m["merchant_id"], 0)}
                                         for m in data["merchants"]])
        conn.execute(insert(transactions), [{
            "txn_id": t["txn_id"], "merchant_id": t["merchant_id"], "amount": t["amount"],
            "payment_date": today - timedelta(days=t["days_ago"]), "time": t["time"],
            "payer_vpa": t["payer_vpa"], "status": t["status"], "duplicate_of": t["duplicate_of"],
        } for t in data["transactions"]])
        conn.execute(insert(settlements), data["settlements"])
        conn.execute(insert(bank_records), [{"utr": utr, **rec} for utr, rec in data["bank"].items()])
        if data["credits"]:
            conn.execute(insert(credits), [{
                "credit_id": c["credit_id"], "merchant_id": c["merchant_id"], "txn_id": c["txn_id"],
                "amount": c["amount"], "credit_date": today - timedelta(days=c["days_ago"]),
            } for c in data["credits"]])
        conn.execute(insert(seed_tickets), [{**t, "sort_order": i} for i, t in enumerate(data["tickets"])])
        conn.execute(delete(meta).where(meta.c.key == "seeded_on"))
        conn.execute(insert(meta), {"key": "seeded_on", "value": today.isoformat()})


def reset_agent_records():
    with engine.begin() as conn:
        for table in AGENT_TABLES:
            conn.execute(delete(table))


def describe() -> dict:
    return {"backend": backend, "host": backend_host}
