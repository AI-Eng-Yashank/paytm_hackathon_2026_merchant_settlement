"""
Customer (merchant) view: health score, money, tickets timeline, conversations and mood.

Health score is a simple, explainable 0-100: start at 100 and subtract for each problem,
listing every deduction so a support lead can see why a merchant is "at risk".
"""

import json
import time
from datetime import date

from sqlalchemy import func, select

from config import settings
from storage import db


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


def health(*, escalated: int, pending_approvals: int, needs_info: int, contacts_7d: int,
           mood: str | None, compensation_due: int, risk_score: float) -> dict:
    factors = []

    def minus(points: int, why: str):
        if points > 0:
            factors.append({"points": -points, "why": why})

    minus(15 * escalated, f"{escalated} escalated ticket(s)")
    minus(10 * pending_approvals, f"{pending_approvals} credit(s) waiting for approval")
    minus(5 * needs_info, f"{needs_info} unanswered question(s) (needs info)")
    minus(min(20, 5 * max(0, contacts_7d - 1)), f"contacted us {contacts_7d} times in 7 days")
    minus({"angry": 15, "worried": 7}.get(mood or "", 0), f"latest mood: {mood}")
    minus(10 if compensation_due else 0, f"RBI TAT breached, ₹{compensation_due} compensation due")
    minus(10 if risk_score > settings.max_risk_score else 0, f"high risk score {risk_score}")

    score = max(0, 100 + sum(f["points"] for f in factors))
    label = "healthy" if score >= 75 else "watch" if score >= 50 else "at_risk"
    return {"score": score, "label": label, "factors": factors}


class CustomerView:
    def __init__(self, store, conversations, audit):
        self.store = store
        self.conversations = conversations
        self.audit = audit  # tickets are written by its background writer; flush before reading

    def _compensation_due(self, merchant_id: str | None = None) -> dict[str, int]:
        """₹100/day beyond T+5 for each still-open settlement, per merchant."""
        T, S = db.transactions, db.settlements
        q = (select(T.c.merchant_id, T.c.payment_date)
             .join(S, S.c.txn_id == T.c.txn_id).where(S.c.status.in_(["PENDING", "FAILED"])))
        if merchant_id:
            q = q.where(T.c.merchant_id == merchant_id)
        due: dict[str, int] = {}
        with db.use() as c:
            for r in c.execute(q):
                overdue = (date.today() - r.payment_date).days - settings.merchant_tat_days
                if overdue > 0:
                    due[r.merchant_id] = due.get(r.merchant_id, 0) + overdue * settings.tat_compensation_per_day
        return due

    def _ticket_stats(self, merchant_id: str | None = None) -> dict[str, dict]:
        """Counts per *contact*: all turns of one conversation are one contact, judged by its latest
        turn's status (a question answered on the next turn is not an open issue)."""
        t = db.tickets
        week_ago = time.time() - 7 * 86400
        q = select(t.c.merchant_id, t.c.ticket_id, t.c.conversation_id, t.c.status, t.c.created_at,
                   t.c.recovered_amount).where(t.c.ticket_id.not_like("COL-%")).order_by(t.c.created_at)
        if merchant_id:
            q = q.where(t.c.merchant_id == merchant_id)
        contacts: dict[tuple, dict] = {}
        stats: dict[str, dict] = {}
        with db.use() as c:
            for r in c.execute(q):
                s = stats.setdefault(r.merchant_id, {"tickets": 0, "resolved": 0, "escalated": 0, "needs_info": 0,
                                                     "awaiting": 0, "contacts_7d": 0, "recovered": 0.0, "last_contact": 0})
                s["recovered"] += r.recovered_amount or 0
                s["last_contact"] = max(s["last_contact"], r.created_at or 0)
                key = (r.merchant_id, r.conversation_id or r.ticket_id)
                first = contacts.get(key, {}).get("started", r.created_at or 0)
                contacts[key] = {"status": r.status, "started": first}  # rows are oldest first: last one wins
        for (mid, _), contact in contacts.items():
            s = stats[mid]
            s["tickets"] += 1
            s["resolved"] += contact["status"] == "RESOLVED"
            s["escalated"] += contact["status"] == "ESCALATED"
            s["needs_info"] += contact["status"] == "NEEDS_INFO"
            s["awaiting"] += contact["status"] == "AWAITING_APPROVAL"
            s["contacts_7d"] += contact["started"] >= week_ago
        return stats

    def _latest_moods(self, merchant_id: str | None = None) -> dict[str, dict]:
        cv = db.conversations
        q = select(cv.c.merchant_id, cv.c.mood, cv.c.mood_reason, cv.c.updated_at).where(cv.c.mood.is_not(None))
        if merchant_id:
            q = q.where(cv.c.merchant_id == merchant_id)
        latest: dict[str, dict] = {}
        with db.use() as c:
            for r in c.execute(q.order_by(cv.c.updated_at)):
                latest[r.merchant_id] = {"mood": r.mood, "reason": r.mood_reason}
        return latest

    def _pending_approvals(self, merchant_id: str | None = None) -> dict[str, list[dict]]:
        a = db.approvals
        q = select(a).where(a.c.status == "PENDING")
        if merchant_id:
            q = q.where(a.c.merchant_id == merchant_id)
        out: dict[str, list[dict]] = {}
        with db.use() as c:
            for r in _rows(c.execute(q)):
                args = json.loads(r["args"])
                out.setdefault(r["merchant_id"], []).append(
                    {"approval_id": r["id"], "ticket_id": r["ticket_id"], "txn_id": args.get("txn_id"),
                     "amount": args.get("amount"), "reasons": json.loads(r["reasons"])})
        return out

    def _health_for(self, m: dict, stats: dict, mood: dict, pending: list, comp: int) -> dict:
        return health(escalated=stats.get("escalated", 0), pending_approvals=len(pending),
                      needs_info=stats.get("needs_info", 0), contacts_7d=stats.get("contacts_7d", 0),
                      mood=mood.get("mood"), compensation_due=comp, risk_score=m["risk_score"])

    def summary(self) -> list[dict]:
        """Every merchant with health, mood and activity; most at-risk first."""
        self.audit.flush()
        with db.session():
            stats, moods = self._ticket_stats(), self._latest_moods()
            pending, comp = self._pending_approvals(), self._compensation_due()
        out = []
        for m in self.store.list_merchants():
            mid = m["merchant_id"]
            s = stats.get(mid, {})
            h = self._health_for(m, s, moods.get(mid, {}), pending.get(mid, []), comp.get(mid, 0))
            out.append({"merchant_id": mid, "name": m["name"], "city": m["city"], "business": m["business"],
                        "health": h["score"], "label": h["label"], "mood": moods.get(mid, {}).get("mood"),
                        "tickets": s.get("tickets", 0), "open_issues": s.get("escalated", 0) + s.get("awaiting", 0)
                        + s.get("needs_info", 0), "last_contact": s.get("last_contact") or None})
        # Merchants we've talked to first, then by health (worst first)
        return sorted(out, key=lambda x: (x["tickets"] == 0, x["health"], x["merchant_id"]))

    def overview(self, merchant_id: str) -> dict | None:
        m = self.store.get_merchant(merchant_id)
        if not m:
            return None
        self.audit.flush()
        with db.session() as c:
            stats = self._ticket_stats(merchant_id).get(merchant_id, {})
            mood = self._latest_moods(merchant_id).get(merchant_id, {})
            pending = self._pending_approvals(merchant_id).get(merchant_id, [])
            comp = self._compensation_due(merchant_id).get(merchant_id, 0)
            t = db.tickets
            tickets = _rows(c.execute(select(t.c.ticket_id, t.c.text, t.c.status, t.c.reply, t.c.steps,
                                             t.c.recovered_amount, t.c.created_at, t.c.duration_ms,
                                             t.c.conversation_id, t.c.human_involved)
                                      .where(t.c.merchant_id == merchant_id).order_by(t.c.created_at.desc())))
            credited = c.execute(select(func.coalesce(func.sum(db.credits.c.amount), 0))
                                 .where(db.credits.c.merchant_id == merchant_id)).scalar()
            conversations = self.conversations.list_for_merchant(merchant_id)
        return {
            "merchant": m,
            "health": self._health_for(m, stats, mood, pending, comp),
            "mood": mood or None,
            "money": {"recovered": stats.get("recovered", 0), "credited_total": credited,
                      "pending_approval": sum(p["amount"] or 0 for p in pending), "compensation_due": comp},
            "counts": {k: stats.get(k, 0) for k in ("tickets", "resolved", "escalated", "needs_info", "awaiting",
                                                     "contacts_7d")},
            "last_contact": stats.get("last_contact") or None,
            "pending_approvals": pending,
            "tickets": tickets,
            "conversations": conversations,
        }
