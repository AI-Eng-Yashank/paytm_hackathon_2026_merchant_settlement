"""
Collections data: invoices merchant B owes merchant A, UPI payment links, and the outbox of
messages (reminders to B, notifications to A).

Each public method is one "backend API" the collections agent's tools call.
"""

import math
import secrets
import time
from datetime import date, timedelta

from sqlalchemy import func, insert, select, update

from storage import db


I, L, O = db.invoices, db.payment_links, db.outbox


def _row(result) -> dict | None:
    row = result.first()
    return dict(row._mapping) if row else None


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


class CollectionsStore:
    """Synchronous; call from a worker thread (asyncio.to_thread)."""

    def __init__(self, store):
        self.store = store  # merchant profiles (A and B are both merchants)

    # ---------- invoices ----------

    def invoice(self, invoice_id: str) -> dict | None:
        with db.use() as c:
            return _row(c.execute(select(I).where(I.c.invoice_id == invoice_id)))

    def view(self, inv: dict) -> dict:
        """Invoice as the agent and dashboard see it."""
        a, b = self.store.get_merchant(inv["creditor_id"]), self.store.get_merchant(inv["debtor_id"])
        outstanding = inv["amount"] - (inv["paid_amount"] or 0)
        overdue = (date.today() - inv["due_on"]).days
        return {
            "invoice_id": inv["invoice_id"], "status": inv["status"], "description": inv["description"],
            "creditor": a["name"], "creditor_id": a["merchant_id"],
            "debtor": b["name"], "debtor_id": b["merchant_id"], "debtor_phone": inv["debtor_phone"],
            "amount": inv["amount"], "paid_amount": inv["paid_amount"] or 0, "outstanding": outstanding,
            "issued_on": inv["issued_on"].isoformat(), "due_on": inv["due_on"].isoformat(),
            "days_overdue": max(0, overdue), "due_in_days": max(0, -overdue),
            "max_instalments": inv["max_instalments"],
            "min_instalment": math.ceil(outstanding / inv["max_instalments"]) if inv["max_instalments"] > 1 else outstanding,
            "promise_date": inv["promise_date"].isoformat() if inv["promise_date"] else None,
            "promise_amount": inv["promise_amount"], "dispute_reason": inv["dispute_reason"],
        }

    def list_invoices(self, creditor_id: str | None = None) -> list[dict]:
        q = select(I).order_by(I.c.due_on)
        if creditor_id:
            q = q.where(I.c.creditor_id == creditor_id)
        with db.use() as c:
            rows = _rows(c.execute(q))
            links = _rows(c.execute(select(L).order_by(L.c.created_at.desc())))
        out = []
        for inv in rows:
            v = self.view(inv)
            v["links"] = [link for link in links if link["invoice_id"] == inv["invoice_id"]]
            out.append(v)
        return out

    # ---------- agent actions ----------

    def create_link(self, invoice_id: str, amount: int) -> dict:
        link_id = f"PAY-{secrets.token_hex(3).upper()}"
        with db.use() as c:
            c.execute(insert(L).values(link_id=link_id, invoice_id=invoice_id, amount=int(amount),
                                       status="ACTIVE", created_at=time.time()))
        return {"link_id": link_id, "amount": int(amount), "url_path": f"/pay/{link_id}"}

    def active_links(self, invoice_id: str) -> int:
        with db.use() as c:
            return c.execute(select(func.count()).select_from(L)
                             .where(L.c.invoice_id == invoice_id, L.c.status == "ACTIVE")).scalar()

    def record_promise(self, invoice_id: str, promise_date: date, amount: int):
        with db.use() as c:
            c.execute(update(I).where(I.c.invoice_id == invoice_id)
                      .values(status="PROMISED", promise_date=promise_date, promise_amount=int(amount)))

    def record_dispute(self, invoice_id: str, reason: str):
        with db.use() as c:
            c.execute(update(I).where(I.c.invoice_id == invoice_id).values(status="DISPUTED", dispute_reason=reason))

    # ---------- payment (merchant B pays the link) ----------

    def link(self, link_id: str) -> dict | None:
        with db.use() as c:
            return _row(c.execute(select(L).where(L.c.link_id == link_id)))

    def pay(self, link_id: str) -> dict:
        """Mark a link paid and apply it to the invoice. Paying twice is a no-op."""
        with db.session() as c:
            link = _row(c.execute(select(L).where(L.c.link_id == link_id)))
            if not link or link["status"] == "PAID":
                return {"already_paid": bool(link), "link": link}
            inv = _row(c.execute(select(I).where(I.c.invoice_id == link["invoice_id"])))
            paid = (inv["paid_amount"] or 0) + link["amount"]
            status = "PAID" if paid >= inv["amount"] else inv["status"] if inv["status"] != "OPEN" else "OPEN"
            c.execute(update(L).where(L.c.link_id == link_id).values(status="PAID", paid_at=time.time()))
            c.execute(update(I).where(I.c.invoice_id == inv["invoice_id"]).values(paid_amount=paid, status=status))
        return {"already_paid": False, "link": {**link, "status": "PAID"}, "invoice": self.view(self.invoice(link["invoice_id"]))}

    # ---------- outbox ----------

    def add_message(self, *, channel: str, to_id: str, to_name: str, to_contact: str, subject: str, body: str,
                    invoice_id: str | None, status: str) -> int:
        now = time.time()
        with db.use() as c:
            return c.execute(insert(O).values(
                created_at=now, channel=channel, to_id=to_id, to_name=to_name, to_contact=to_contact,
                subject=subject, body=body, invoice_id=invoice_id, status=status,
                sent_at=now if status == "SENT" else None).returning(O.c.id)).scalar_one()

    def message(self, message_id: int) -> dict | None:
        with db.use() as c:
            return _row(c.execute(select(O).where(O.c.id == message_id)))

    def send_message(self, message_id: int, body: str, approved_by: str):
        with db.use() as c:
            c.execute(update(O).where(O.c.id == message_id)
                      .values(body=body, status="SENT", sent_at=time.time(), approved_by=approved_by))

    def outbox(self, limit: int = 50) -> list[dict]:
        with db.use() as c:
            return _rows(c.execute(select(O).order_by(O.c.id.desc()).limit(limit)))

    def notify_creditor(self, inv: dict, subject: str, body: str) -> int:
        """Tell merchant A what happened (shown on A's dashboard)."""
        a = self.store.get_merchant(inv["creditor_id"])
        return self.add_message(channel="dashboard", to_id=a["merchant_id"], to_name=a["name"], to_contact="dashboard",
                                subject=subject, body=body, invoice_id=inv["invoice_id"], status="SENT")

    # ---------- summary ----------

    def summary(self, creditor_id: str | None = None) -> dict:
        invs = self.list_invoices(creditor_id)
        paid_links = [link for inv in invs for link in inv["links"] if link["status"] == "PAID"]
        today = date.today()
        return {
            "invoices": len(invs),
            "outstanding": sum(i["outstanding"] for i in invs if i["status"] != "PAID"),
            "collected": sum(link["amount"] for link in paid_links),
            "collected_today": sum(link["amount"] for link in paid_links
                                   if link["paid_at"] and date.fromtimestamp(link["paid_at"]) == today),
            "paid": sum(i["status"] == "PAID" for i in invs),
            "promised": sum(i["status"] == "PROMISED" for i in invs),
            "disputed": sum(i["status"] == "DISPUTED" for i in invs),
            "overdue": sum(i["days_overdue"] > 0 and i["status"] != "PAID" for i in invs),
        }

    def promise_window(self, max_days: int) -> tuple[date, date]:
        return date.today(), date.today() + timedelta(days=max_days)
