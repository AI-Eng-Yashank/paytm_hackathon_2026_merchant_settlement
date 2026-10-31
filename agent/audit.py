"""
Audit log, ticket records and the human-approval queue, stored in the database (Neon or SQLite).

Every agent step is written with its tool, arguments, result, reason and guardrail decision,
so any resolution can be replayed and explained later.

Writes (ticket open/close, audit lines) go through a single background thread in order, so the
agent never waits on a database round trip to log. Reads call flush() first, so they always see
everything written so far.
"""

import json
import queue
import threading
import time

from sqlalchemy import delete, insert, select, update

from storage import db
from agent.timing import log_error


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


class AuditStore:
    """Agent records: tickets, audit log, approvals."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._writer, name="audit-writer", daemon=True).start()

    def _writer(self):
        while True:
            # Take everything queued so far and write it on one connection, in order;
            # consecutive audit lines go in as a single multi-row insert (one round trip).
            jobs = [self._queue.get()]
            while not self._queue.empty():
                jobs.append(self._queue.get_nowait())
            try:
                with db.session():
                    pending_logs: list[dict] = []
                    for fn, args in jobs:
                        if fn == self._insert_log:
                            pending_logs.append(args[0])
                            continue
                        self._insert_logs(pending_logs)
                        pending_logs = []
                        fn(*args)
                    self._insert_logs(pending_logs)
            except Exception as e:
                log_error(f"audit writer ({len(jobs)} writes)", e)
            finally:
                for _ in jobs:
                    self._queue.task_done()

    def _insert_logs(self, rows: list[dict]):
        if rows:
            with db.use() as c:
                c.execute(insert(db.audit_log), rows)

    def _later(self, fn, *args):
        self._queue.put((fn, args))

    def flush(self):
        """Block until every queued write is in the database."""
        self._queue.join()

    def reset(self):
        self.flush()
        db.reset_agent_records()

    # ---------- tickets ----------

    def open_ticket(self, ticket_id: str, merchant_id: str, text: str, language: str,
                    conversation_id: str | None = None):
        self._later(self._open_ticket, ticket_id, merchant_id, text, language, time.time(), conversation_id)

    def _open_ticket(self, ticket_id, merchant_id, text, language, created_at, conversation_id=None):
        with db.use() as c:
            # Re-running a demo ticket id replaces the earlier record
            c.execute(delete(db.tickets).where(db.tickets.c.ticket_id == ticket_id))
            c.execute(insert(db.tickets).values(
                ticket_id=ticket_id, merchant_id=merchant_id, text=text, language=language,
                status="IN_PROGRESS", steps=0, recovered_amount=0, human_involved=0, created_at=created_at,
                conversation_id=conversation_id))

    def close_ticket(self, ticket_id: str, status: str, reply: str | None = None, steps: int | None = None,
                     recovered: float = 0, human: bool = False):
        self._later(self._close_ticket, ticket_id, status, reply, steps, recovered, human, time.time())

    def _close_ticket(self, ticket_id, status, reply, steps, recovered, human, now):
        row = self._get_ticket(ticket_id)
        values = {
            "status": status, "resolved_at": now,
            "recovered_amount": (row["recovered_amount"] or 0) + recovered,
            "human_involved": max(row["human_involved"] or 0, int(human)),
        }
        if reply is not None:
            values["reply"] = reply
        if steps is not None:
            values["steps"] = steps
        if row["duration_ms"] is None:
            values["duration_ms"] = int((now - row["created_at"]) * 1000)
        with db.use() as c:
            c.execute(update(db.tickets).where(db.tickets.c.ticket_id == ticket_id).values(**values))

    def _get_ticket(self, ticket_id: str) -> dict | None:
        with db.use() as c:
            row = c.execute(select(db.tickets).where(db.tickets.c.ticket_id == ticket_id)).first()
        return dict(row._mapping) if row else None

    def get_ticket(self, ticket_id: str) -> dict | None:
        self.flush()
        return self._get_ticket(ticket_id)

    def list_tickets(self) -> list[dict]:
        self.flush()
        with db.use() as c:
            return _rows(c.execute(select(db.tickets).order_by(db.tickets.c.created_at.desc())))

    # ---------- audit ----------

    def log(self, ticket_id: str, event: str, tool: str | None = None, args: dict | None = None,
            result: dict | None = None, reason: str = "", decision: str = "", actor: str = "agent"):
        # Serialise now (the caller may mutate dicts later); write in the background
        row = dict(ts=time.time(), ticket_id=ticket_id, actor=actor, event=event, tool=tool,
                   args=json.dumps(args or {}, ensure_ascii=False, default=str),
                   result=json.dumps(result or {}, ensure_ascii=False, default=str),
                   reason=reason, decision=decision)
        self._later(self._insert_log, row)

    def _insert_log(self, row: dict):
        with db.use() as c:
            c.execute(insert(db.audit_log).values(**row))

    def list_audit(self, ticket_id: str | None = None, limit: int = 200) -> list[dict]:
        self.flush()
        a = db.audit_log
        with db.use() as c:
            if ticket_id:
                return _rows(c.execute(select(a).where(a.c.ticket_id == ticket_id).order_by(a.c.id)))
            return _rows(c.execute(select(a).order_by(a.c.id.desc()).limit(limit)))

    # ---------- approvals ----------

    def create_approval(self, ticket_id: str, merchant_id: str, tool: str, args: dict,
                        reasons: list[str], agent_reason: str) -> int:
        with db.use() as c:
            result = c.execute(insert(db.approvals).values(
                ticket_id=ticket_id, merchant_id=merchant_id, tool=tool, args=json.dumps(args),
                reasons=json.dumps(reasons, ensure_ascii=False), agent_reason=agent_reason,
                status="PENDING", created_at=time.time()).returning(db.approvals.c.id))
            return result.scalar_one()

    def find_pending_approval(self, merchant_id: str, tool: str, txn_id: str) -> dict | None:
        a = db.approvals
        with db.use() as c:
            rows = _rows(c.execute(select(a).where(a.c.status == "PENDING", a.c.merchant_id == merchant_id,
                                                   a.c.tool == tool)))
        return next((r for r in rows if json.loads(r["args"]).get("txn_id") == txn_id), None)

    def get_approval(self, approval_id: int) -> dict | None:
        with db.use() as c:
            row = c.execute(select(db.approvals).where(db.approvals.c.id == approval_id)).first()
        return dict(row._mapping) if row else None

    def list_approvals(self, status: str | None = None) -> list[dict]:
        a = db.approvals
        q = select(a).order_by(a.c.id.desc())
        if status:
            q = q.where(a.c.status == status)
        with db.use() as c:
            return _rows(c.execute(q))

    def decide_approval(self, approval_id: int, status: str, decided_by: str):
        with db.use() as c:
            c.execute(update(db.approvals).where(db.approvals.c.id == approval_id)
                      .values(status=status, decided_at=time.time(), decided_by=decided_by))

    # ---------- metrics ----------

    def metrics(self) -> dict:
        self.flush()
        t = db.tickets
        with db.use() as c:
            rows = _rows(c.execute(select(t.c.status, t.c.human_involved, t.c.recovered_amount, t.c.duration_ms,
                                          t.c.steps).where(t.c.status != "IN_PROGRESS")))
        auto = [r for r in rows if r["status"] == "RESOLVED" and not r["human_involved"]]
        durations = [r["duration_ms"] for r in auto if r["duration_ms"]]
        return {
            "tickets": len(rows),
            "auto_resolved": len(auto),
            "auto_resolution_rate": round(100 * len(auto) / len(rows)) if rows else 0,
            "escalated": sum(r["status"] == "ESCALATED" for r in rows),
            "awaiting_approval": sum(r["status"] == "AWAITING_APPROVAL" for r in rows),
            "needs_info": sum(r["status"] == "NEEDS_INFO" for r in rows),
            "recovered_amount": sum(r["recovered_amount"] or 0 for r in rows),
            "avg_resolve_ms": int(sum(durations) / len(durations)) if durations else 0,
            "avg_steps": round(sum(r["steps"] or 0 for r in rows) / len(rows), 1) if rows else 0,
        }
