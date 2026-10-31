"""
Mock systems backed by the database: Payments, Settlement Ledger, Bank/NPCI, Credits and CRM.

Each public method is one "backend API" the agent's tools call. Data lives in Neon (or the
local SQLite fallback) and is reseeded from data/seed.json on reset or on a new day.

Queries are kept to as few round trips as possible: merchants are cached in memory (they don't
change during a demo), and related rows are fetched with joins rather than one query each.
"""

import random
import re
from datetime import date, timedelta

from sqlalchemy import func, insert, or_, select, update

from config import settings
from storage import db


def _txn_key(txn_id: str | None) -> str:
    """Spoken ids come back from speech-to-text as "TXN 240 004" or "txn-240004"; match them anyway."""
    return re.sub(r"[\s\-_.]", "", txn_id or "").upper()


def _row(result) -> dict | None:
    row = result.first()
    return dict(row._mapping) if row else None


T, S, B, C = db.transactions, db.settlements, db.bank_records, db.credits


class MockStore:
    """Data access for all five mock systems."""

    def __init__(self):
        self._rng = random.Random()
        self._merchants: dict[str, dict] | None = None

    def reset(self):
        """Restore every system to the seed state."""
        db.seed_mock_systems()
        self._merchants = None
        self._merchant_cache()  # reload now, not on the next ticket

    # ---------- lookups used by guardrails and endpoints ----------

    def _merchant_cache(self) -> dict[str, dict]:
        if self._merchants is None:
            with db.use() as c:
                rows = c.execute(select(db.merchants).order_by(db.merchants.c.merchant_id))
                self._merchants = {r.merchant_id: dict(r._mapping) for r in rows}
        return self._merchants

    def get_merchant(self, merchant_id: str) -> dict | None:
        return self._merchant_cache().get(merchant_id)

    def list_merchants(self) -> list[dict]:
        return list(self._merchant_cache().values())

    def list_seed_tickets(self, prefix: str = "") -> list[dict]:
        with db.use() as c:
            q = select(db.seed_tickets).order_by(db.seed_tickets.c.sort_order)
            if prefix:
                q = q.where(db.seed_tickets.c.ticket_id.like(f"{prefix}%"))
            return [dict(r._mapping) for r in c.execute(q)]

    def _own_txn(self, txn_id: str, merchant_id: str) -> dict | None:
        """The txn joined with its settlement, only if it belongs to this merchant
        (the agent can't see other merchants' data)."""
        with db.use() as c:
            return _row(c.execute(
                select(T, S.c.status.label("settlement_status"), S.c.utr, S.c.failure_reason)
                .join(S, S.c.txn_id == T.c.txn_id, isouter=True)
                .where(T.c.txn_id == _txn_key(txn_id), T.c.merchant_id == merchant_id)))

    def twin_of(self, txn: dict) -> dict | None:
        """The other half of a duplicate-debit pair, if any."""
        with db.use() as c:
            return _row(c.execute(select(T).where(or_(T.c.duplicate_of == txn["txn_id"],
                                                      T.c.txn_id == (txn["duplicate_of"] or "")))))

    def get_bank_record(self, utr: str) -> dict | None:
        with db.use() as c:
            return _row(c.execute(select(B).where(B.c.utr == (utr or "").strip().upper())))

    def prior_credit(self, txn_id: str) -> dict | None:
        with db.use() as c:
            return _row(c.execute(select(C).where(C.c.txn_id == txn_id)))

    def credited_today(self, merchant_id: str) -> int:
        with db.use() as c:
            return c.execute(select(func.coalesce(func.sum(C.c.amount), 0))
                             .where(C.c.merchant_id == merchant_id, C.c.credit_date == date.today())).scalar()

    def credit_context(self, merchant_id: str, txn_id: str) -> dict | None:
        """Everything the instant-credit guardrail needs, in one query: the txn, its settlement,
        the bank's view of the UTR, any earlier credit for this txn, and today's credits."""
        prior = select(func.count()).select_from(C).where(C.c.txn_id == T.c.txn_id).scalar_subquery()
        today = (select(func.coalesce(func.sum(C.c.amount), 0)).select_from(C)
                 .where(C.c.merchant_id == merchant_id, C.c.credit_date == date.today()).scalar_subquery())
        q = (select(T, S.c.status.label("settlement_status"), S.c.utr, B.c.status.label("bank_status"),
                    prior.label("prior_credits"), today.label("credited_today"))
             .join(S, S.c.txn_id == T.c.txn_id, isouter=True)
             .join(B, B.c.utr == S.c.utr, isouter=True)
             .where(T.c.txn_id == _txn_key(txn_id), T.c.merchant_id == merchant_id))
        with db.use() as c:
            return _row(c.execute(q))

    def _settlement_view(self, txn: dict) -> dict:
        days = (date.today() - txn["payment_date"]).days
        open_ = txn["settlement_status"] in ("PENDING", "FAILED")
        overdue = max(0, days - settings.merchant_tat_days) if open_ else 0
        return {
            "txn_id": txn["txn_id"], "settlement_status": txn["settlement_status"], "utr": txn["utr"],
            "failure_reason": txn.get("failure_reason"), "days_since_payment": days,
            "rbi_tat_days": settings.merchant_tat_days, "tat_breached": overdue > 0,
            "compensation_due": overdue * settings.tat_compensation_per_day,
        }

    # ---------- CRM ----------

    def lookup_merchant(self, merchant_id: str) -> dict:
        return self.get_merchant(merchant_id) or {"error": "MERCHANT_NOT_FOUND"}

    # ---------- Payments ----------

    def check_payment_status(self, merchant_id: str, txn_id: str) -> dict:
        txn = self._own_txn(txn_id, merchant_id)
        if not txn:
            return {"error": "TXN_NOT_FOUND", "txn_id": txn_id}
        twin = self.twin_of(txn)
        return {
            "txn_id": txn["txn_id"], "amount": txn["amount"], "status": txn["status"],
            "date": txn["payment_date"].isoformat(), "time": txn["time"], "payer_vpa": txn["payer_vpa"],
            "is_duplicate_debit": twin is not None,
            "duplicate_pair": twin["txn_id"] if twin else None,
        }

    def find_txn_by_amount_time(self, merchant_id: str, amount: float, days_ago: int | None = None) -> dict:
        q = select(T).where(T.c.merchant_id == merchant_id, func.abs(T.c.amount - float(amount)) < 1)
        if days_ago is not None:
            q = q.where(T.c.payment_date == date.today() - timedelta(days=int(days_ago)))
        with db.use() as c:
            rows = [dict(r._mapping) for r in c.execute(q.order_by(T.c.payment_date.desc()))]
        return {"matches": [{"txn_id": r["txn_id"], "amount": r["amount"], "date": r["payment_date"].isoformat(),
                             "time": r["time"]} for r in rows]}

    def list_recent_transactions(self, merchant_id: str, limit: int = 10) -> dict:
        q = (select(T.c.txn_id, T.c.amount, T.c.payment_date, T.c.time, T.c.status,
                    S.c.status.label("settlement_status"), S.c.utr)
             .join(S, S.c.txn_id == T.c.txn_id, isouter=True)
             .where(T.c.merchant_id == merchant_id)
             .order_by(T.c.payment_date.desc(), T.c.time.desc())
             .limit(max(1, min(int(limit), 20))))
        with db.use() as c:
            rows = [dict(r._mapping) for r in c.execute(q)]
        return {"transactions": [{**r, "payment_date": r["payment_date"].isoformat()} for r in rows]}

    # ---------- Settlement ledger ----------

    def get_settlement_status(self, merchant_id: str, txn_id: str) -> dict:
        txn = self._own_txn(txn_id, merchant_id)
        if not txn:
            return {"error": "TXN_NOT_FOUND", "txn_id": txn_id}
        return self._settlement_view(txn)

    def retry_settlement(self, merchant_id: str, txn_id: str) -> dict:
        txn = self._own_txn(txn_id, merchant_id)
        utr = f"UTR{self._rng.randint(100000000, 999999999)}"
        with db.use() as c:
            c.execute(update(S).where(S.c.txn_id == txn["txn_id"]).values(status="SETTLED", utr=utr, failure_reason=None))
            c.execute(insert(B).values(utr=utr, status="CREDITED", amount=txn["amount"]))
        return {"txn_id": txn["txn_id"], "settlement_status": "SETTLED", "new_utr": utr,
                "amount": txn["amount"], "expected_in_account": "within 2 hours"}

    # ---------- Bank / NPCI ----------

    def trigger_bank_reconciliation(self, merchant_id: str, utr: str) -> dict:
        utr = (utr or "").strip().upper()
        with db.use() as c:
            row = _row(c.execute(
                select(S.c.txn_id, S.c.status.label("settlement_status"), B.c.status.label("bank_status"), B.c.amount)
                .join(T, T.c.txn_id == S.c.txn_id)
                .join(B, B.c.utr == S.c.utr)
                .where(S.c.utr == utr, T.c.merchant_id == merchant_id)))
            if not row:
                return {"utr": utr, "bank_status": "NOT_FOUND"}
            if row["bank_status"] == "CREDITED" and row["settlement_status"] == "PENDING":
                c.execute(update(S).where(S.c.txn_id == row["txn_id"]).values(status="SETTLED"))
        return {"utr": utr, "bank_status": row["bank_status"], "credited_amount": row["amount"],
                "merchant_bank": self.get_merchant(merchant_id)["settlement_bank"]}

    # ---------- Credits ----------

    def reverse_duplicate_debit(self, merchant_id: str, txn_id: str) -> dict:
        txn = self._own_txn(txn_id, merchant_id)
        dup = txn if txn["duplicate_of"] else self.twin_of(txn)
        with db.use() as c:
            c.execute(update(T).where(T.c.txn_id == dup["txn_id"]).values(status="REVERSED"))
        return {"reversed_txn_id": dup["txn_id"], "amount": dup["amount"], "refund_to": dup["payer_vpa"],
                "refund_ref": f"RFND{self._rng.randint(10000, 99999)}", "eta": "T+1"}

    def issue_instant_credit(self, merchant_id: str, txn_id: str, amount: float) -> dict:
        txn = self._own_txn(txn_id, merchant_id)
        amount = int(round(amount))  # whole rupees, like the rest of the mock data
        credit_id = f"CR-{self._rng.randint(80000, 99999)}"
        with db.use() as c:
            c.execute(insert(C).values(credit_id=credit_id, merchant_id=merchant_id, txn_id=txn["txn_id"],
                                       amount=amount, credit_date=date.today()))
            c.execute(update(S).where(S.c.txn_id == txn["txn_id"]).values(status="CREDITED_INSTANT"))
        return {"credit_id": credit_id, "txn_id": txn["txn_id"], "amount": amount, "status": "CREDITED",
                "to_bank": self.get_merchant(merchant_id)["settlement_bank"]}
