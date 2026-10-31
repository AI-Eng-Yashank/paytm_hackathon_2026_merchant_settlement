"""
Test setup: every test runs against a throwaway SQLite database and log folder, with the
offline scripted planner and no delays. Nothing here touches Neon, Sarvam or your real logs.

Environment variables are set before any app module is imported; real env vars take
precedence over .env, so your keys in .env are ignored during tests.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TMP = Path(tempfile.mkdtemp(prefix="settlementresolver-tests-"))

os.environ.update({
    "SARVAM_API_KEY": "",
    "DATABASE_URL": "",
    "SQLITE_PATH": str(TMP / "test.db"),
    "LOG_DIR": str(TMP / "logs"),
    "RECORDINGS_DIR": str(TMP / "recordings"),
    "SCRIPTED_DELAY_S": "0",
    "DETAILED_LOG": "true",
})
sys.path.insert(0, str(ROOT))

import main  # noqa: E402  (must come after the environment is set)
from fastapi.testclient import TestClient  # noqa: E402

SEED = json.loads((ROOT / "data" / "seed.json").read_text(encoding="utf-8"))

# Expected outcome for each ticket scenario in data/seed.json
EXPECTED_STATUS = {
    "settlement_failed": "RESOLVED",
    "duplicate_debit": "RESOLVED",
    "pending_credited": "RESOLVED",
    "tat_breach_small": "RESOLVED",
    "tat_breach_large": "AWAITING_APPROVAL",
    "repeat_claim": "ESCALATED",
    "no_txn_id": "RESOLVED",
}


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh mock data and empty agent records before every test."""
    main.store.reset()
    main.audit.reset()
    main.debug.enabled = True
    yield


@pytest.fixture
def store():
    return main.store


@pytest.fixture
def audit():
    return main.audit


@pytest.fixture
def log_dir() -> Path:
    return TMP / "logs"


def seed_ticket(ticket_id: str) -> dict:
    return next(t for t in SEED["tickets"] if t["ticket_id"] == ticket_id)


def first_of(scenario: str) -> dict:
    return next(t for t in SEED["tickets"] if t["scenario"] == scenario)


def run_ticket(client, ticket_id: str) -> dict:
    t = seed_ticket(ticket_id)
    r = client.post("/api/tickets", json={"merchant_id": t["merchant_id"], "text": t["text"], "ticket_id": ticket_id})
    assert r.status_code == 200, r.text
    return r.json()
