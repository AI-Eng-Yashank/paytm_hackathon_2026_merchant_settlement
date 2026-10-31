"""
Seed data generator for the mock payment, ledger, bank, credit and CRM systems.

Run:  python -m mock_systems.seed   (writes data/seed.json)

Dates are stored as `days_ago` so TAT checks stay valid whatever day the demo runs.
The first five tickets are hand-written demo scenarios; the rest are generated for
the batch run.
"""

import json
import random
from pathlib import Path

from faker import Faker


SEED_PATH = Path(__file__).parent.parent / "data" / "seed.json"

BANKS = ["HDFC Bank", "State Bank of India", "ICICI Bank", "Axis Bank", "Kotak Mahindra Bank", "Bank of Baroda"]
BUSINESSES = ["Kirana Store", "Medical Store", "Tiffin Centre", "Mobile Shop", "Sweet Shop", "Hardware Store",
              "Boutique", "Chai Stall", "Stationery", "Bakery", "Salon", "Dairy"]


def _merchant(mid, name, business, city, kyc="FULL", age=400, risk=0.1, bank="HDFC Bank"):
    return {
        "merchant_id": mid, "name": name, "business": business, "city": city,
        "kyc_tier": kyc, "account_age_days": age, "risk_score": risk,
        "settlement_bank": bank, "settlement_cycle": "T+1",
    }


def _txn(txn_id, mid, amount, days_ago, time, payer, status="SUCCESS", duplicate_of=None):
    return {
        "txn_id": txn_id, "merchant_id": mid, "amount": amount, "days_ago": days_ago,
        "time": time, "payer_vpa": payer, "status": status, "duplicate_of": duplicate_of,
    }


def _settlement(txn_id, status, utr=None, failure_reason=None):
    return {"txn_id": txn_id, "status": status, "utr": utr, "failure_reason": failure_reason}


def demo_scenarios() -> dict:
    """Five hand-written scenarios, one per resolution path, for the live demo."""
    merchants = [
        _merchant("M1001", "Sharma General Store", "Kirana Store", "Jaipur"),
        _merchant("M1002", "Gupta Medicals", "Medical Store", "Lucknow", bank="State Bank of India"),
        _merchant("M1003", "Balaji Tiffin Centre", "Tiffin Centre", "Hyderabad", bank="ICICI Bank"),
        _merchant("M1004", "Mehta Electronics", "Mobile Shop", "Pune", bank="Axis Bank"),
        _merchant("M1005", "New Fashion Hub", "Boutique", "Surat", kyc="MIN", age=12, risk=0.85),
    ]
    txns = [
        _txn("TXN240001", "M1001", 4500, 1, "14:05", "ravi.k@okhdfc"),
        _txn("TXN240002", "M1002", 1200, 0, "11:42", "neha.s@oksbi"),
        _txn("TXN240003", "M1002", 1200, 0, "11:42", "neha.s@oksbi", duplicate_of="TXN240002"),
        _txn("TXN240004", "M1003", 3250, 2, "19:20", "arjun.r@okicici"),
        _txn("TXN240005", "M1004", 25000, 7, "16:10", "pooja.m@okaxis"),
        _txn("TXN240006", "M1005", 8000, 6, "12:30", "vikas.t@ybl"),
    ]
    settlements = [
        _settlement("TXN240001", "FAILED", failure_reason="Beneficiary bank timeout"),
        _settlement("TXN240002", "SETTLED", utr="UTR998877000"),
        _settlement("TXN240003", "SETTLED", utr="UTR998877000"),
        _settlement("TXN240004", "PENDING", utr="UTR998877001"),
        _settlement("TXN240005", "PENDING", utr="UTR998877002"),
        _settlement("TXN240006", "PENDING", utr="UTR998877003"),
    ]
    bank = {
        "UTR998877000": {"status": "CREDITED", "amount": 2400},
        "UTR998877001": {"status": "CREDITED", "amount": 3250},
        "UTR998877002": {"status": "NOT_FOUND", "amount": 0},
        "UTR998877003": {"status": "NOT_FOUND", "amount": 0},
    }
    credits = [
        # Already credited once two days ago: a repeat claim must be blocked
        {"credit_id": "CR-77001", "merchant_id": "M1005", "txn_id": "TXN240006", "amount": 8000, "days_ago": 2},
    ]
    past_tickets = {"M1004": 2, "M1005": 3}
    tickets = [
        {"ticket_id": "DEMO-1", "merchant_id": "M1001", "scenario": "settlement_failed",
         "label": "Failed settlement", "text": "Mera kal ka ₹4,500 ka settlement abhi tak nahi aaya. Txn id TXN240001 hai."},
        {"ticket_id": "DEMO-2", "merchant_id": "M1002", "scenario": "duplicate_debit",
         "label": "Duplicate debit", "text": "Customer bol raha hai ₹1,200 do baar kat gaye aaj, TXN240003. Please check karo."},
        {"ticket_id": "DEMO-3", "merchant_id": "M1003", "scenario": "pending_credited",
         "label": "Pending but credited", "text": "TXN240004 ka settlement pending dikh raha hai, kab aayega?"},
        {"ticket_id": "DEMO-4", "merchant_id": "M1004", "scenario": "tat_breach_large",
         "label": "₹25,000 stuck 7 days", "text": "7 din ho gaye, ₹25,000 ka settlement nahi aaya. TXN240005. Bahut problem ho rahi hai."},
        {"ticket_id": "DEMO-5", "merchant_id": "M1005", "scenario": "repeat_claim",
         "label": "Repeat credit claim", "text": "Mera ₹8,000 abhi tak nahi aaya TXN240006, turant credit karo."},
        {"ticket_id": "DEMO-6", "merchant_id": "M1001", "scenario": "no_txn_id",
         "label": "No txn id given", "text": "Kal dopahar 2 baje ₹4,500 ka payment aaya tha, uska paisa account me nahi aaya."},
    ]
    # Collections: Sharma General Store (A) supplies other shops on credit; each invoice is one demo path
    invoices = [
        {"invoice_id": "INV-1001", "creditor_id": "M1001", "debtor_id": "M1002", "debtor_phone": "+91 98290 11223",
         "description": "Wholesale FMCG cartons (October)", "amount": 12000, "issued_days_ago": 27,
         "due_in_days": -12, "max_instalments": 2},     # reliable payer: pays via link on the call
        {"invoice_id": "INV-1002", "creditor_id": "M1001", "debtor_id": "M1003", "debtor_phone": "+91 98490 33441",
         "description": "Rice and oil bulk order", "amount": 3500, "issued_days_ago": 20,
         "due_in_days": -5, "max_instalments": 1},      # can't pay today: promise to pay
        {"invoice_id": "INV-1003", "creditor_id": "M1001", "debtor_id": "M1005", "debtor_phone": "+91 99250 77801",
         "description": "Packaging material", "amount": 18000, "issued_days_ago": 45,
         "due_in_days": -30, "max_instalments": 2},     # disputes the goods: hand back to A
        {"invoice_id": "INV-1004", "creditor_id": "M1001", "debtor_id": "M1004", "debtor_phone": "+91 98220 55190",
         "description": "Office supplies", "amount": 7200, "issued_days_ago": 13,
         "due_in_days": 2, "max_instalments": 1},       # not due yet: friendly reminder message
    ]
    return {"merchants": merchants, "transactions": txns, "settlements": settlements,
            "bank": bank, "credits": credits, "past_tickets": past_tickets, "tickets": tickets,
            "invoices": invoices}


TEMPLATES = {
    "settlement_failed": ["Mera ₹{amount} ka settlement fail ho gaya lagta hai, {ref}.",
                          "Settlement nahi aaya ₹{amount} ka, {ref}. Kya hua?"],
    "duplicate_debit": ["Customer ka ₹{amount} do baar kat gaya, {ref}.",
                        "Ek hi payment do baar debit hua ₹{amount}, {ref}. Refund karo."],
    "pending_credited": ["{ref} ka ₹{amount} pending dikh raha hai, kab aayega?",
                         "Mera settlement kab aayega? ₹{amount}, {ref}."],
    "tat_breach_small": ["{days} din se ₹{amount} atka hua hai, {ref}.",
                         "Bahut din ho gaye ₹{amount} nahi aaya. {ref}."],
    "tat_breach_large": ["{days} din ho gaye, ₹{amount} ka settlement nahi aaya, {ref}. Urgent hai.",
                         "Itna bada amount ₹{amount} {days} din se atka hai, {ref}."],
    "repeat_claim": ["₹{amount} abhi tak nahi aaya {ref}, turant credit karo.",
                     "Mujhe ₹{amount} ka credit chahiye abhi, {ref}."],
}

# (scenario, share of generated tickets)
MIX = [("settlement_failed", 0.30), ("duplicate_debit", 0.20), ("pending_credited", 0.20),
       ("tat_breach_small", 0.15), ("tat_breach_large", 0.10), ("repeat_claim", 0.05)]


def generated(n_tickets: int = 44, seed: int = 42) -> dict:
    """Random merchants and tickets across all scenarios, for the batch run."""
    fake = Faker("en_IN")
    Faker.seed(seed)
    rng = random.Random(seed)

    data = {"merchants": [], "transactions": [], "settlements": [], "bank": {},
            "credits": [], "past_tickets": {}, "tickets": [], "invoices": []}
    scenarios = [s for s, share in MIX for _ in range(round(share * n_tickets))][:n_tickets]
    rng.shuffle(scenarios)

    for i, scenario in enumerate(scenarios):
        mid = f"M{2001 + i}"
        risky = scenario == "repeat_claim"
        data["merchants"].append(_merchant(
            mid, f"{fake.last_name()} {rng.choice(BUSINESSES)}", rng.choice(BUSINESSES), fake.city(),
            kyc="MIN" if risky else "FULL", age=rng.randint(5, 25) if risky else rng.randint(60, 1500),
            risk=round(rng.uniform(0.75, 0.95), 2) if risky else round(rng.uniform(0.02, 0.3), 2),
            bank=rng.choice(BANKS)))

        txn_id = f"TXN{250001 + i}"
        utr = f"UTR{770000000 + i}"
        payer = f"{fake.first_name().lower()}{rng.randint(1, 99)}@{rng.choice(['okhdfc', 'oksbi', 'ybl', 'paytm', 'okaxis'])}"
        time = f"{rng.randint(9, 21):02d}:{rng.choice(['05', '15', '30', '45'])}"

        if scenario == "tat_breach_large":
            amount = rng.choice([12000, 15500, 18000, 22000, 30000])
        elif scenario == "tat_breach_small":
            amount = rng.choice([650, 1800, 2500, 4200, 7300])
        else:
            amount = rng.choice([150, 380, 999, 1200, 2450, 3600, 5200, 8800])

        days = {"settlement_failed": 1, "duplicate_debit": 0, "pending_credited": 2,
                "tat_breach_small": rng.randint(6, 9), "tat_breach_large": rng.randint(6, 10),
                "repeat_claim": 6}[scenario]

        data["transactions"].append(_txn(txn_id, mid, amount, days, time, payer))
        ref_txn = txn_id
        if scenario == "settlement_failed":
            data["settlements"].append(_settlement(txn_id, "FAILED", failure_reason=rng.choice(
                ["Beneficiary bank timeout", "NPCI switch timeout", "Account validation pending"])))
        elif scenario == "duplicate_debit":
            dup_id = f"TXN{260001 + i}"
            data["transactions"].append(_txn(dup_id, mid, amount, days, time, payer, duplicate_of=txn_id))
            data["settlements"].append(_settlement(txn_id, "SETTLED", utr=utr))
            data["settlements"].append(_settlement(dup_id, "SETTLED", utr=utr))
            data["bank"][utr] = {"status": "CREDITED", "amount": amount * 2}
            ref_txn = dup_id
        elif scenario == "pending_credited":
            data["settlements"].append(_settlement(txn_id, "PENDING", utr=utr))
            data["bank"][utr] = {"status": "CREDITED", "amount": amount}
        else:
            data["settlements"].append(_settlement(txn_id, "PENDING", utr=utr))
            data["bank"][utr] = {"status": "NOT_FOUND", "amount": 0}
            if scenario == "repeat_claim":
                data["credits"].append({"credit_id": f"CR-{78001 + i}", "merchant_id": mid,
                                        "txn_id": txn_id, "amount": amount, "days_ago": 2})
                data["past_tickets"][mid] = 3

        # ~15% of tickets give amount + time instead of a txn id (agent must find it)
        if scenario != "duplicate_debit" and rng.random() < 0.15:
            ref = f"{'kal' if days == 1 else f'{days} din pehle'} {time} baje ka payment"
        else:
            ref = f"txn {ref_txn}"
        text = rng.choice(TEMPLATES[scenario]).format(amount=f"{amount:,}", ref=ref, days=days)
        data["tickets"].append({"ticket_id": f"BATCH-{i + 1:02d}", "merchant_id": mid,
                                "scenario": scenario, "label": scenario.replace("_", " "), "text": text})
    return data


def build() -> dict:
    demo, gen = demo_scenarios(), generated()
    merged = {}
    for key in demo:
        if isinstance(demo[key], list):
            merged[key] = demo[key] + gen[key]
        else:
            merged[key] = {**demo[key], **gen[key]}
    return merged


if __name__ == "__main__":
    SEED_PATH.parent.mkdir(exist_ok=True)
    data = build()
    SEED_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {SEED_PATH}: {len(data['merchants'])} merchants, "
          f"{len(data['transactions'])} txns, {len(data['tickets'])} tickets")
