"""Seed the reference customer table.

Each row exists to exercise a specific branch of the policy, so the eval
personas in `evals/` map one-to-one onto these records. Run with:

    python -m app.seed
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Customer

# All PANs are syntactically valid but deliberately fictitious.
SEED_CUSTOMERS: list[dict] = [
    {
        # Clean approval: prime score, comfortable headroom.
        "full_name": "Rajesh Kumar",
        "date_of_birth": "1988-04-12",
        "pan": "ABCDE1234F",
        "phone": "+919800000001",
        "monthly_income": 95_000,
        "employment_type": "salaried",
        "existing_emi": 8_000,
        "credit_score": 782,
    },
    {
        # Counter-offer: affordable, but not at the amount usually requested.
        "full_name": "Priya Sharma",
        "date_of_birth": "1994-11-03",
        "pan": "BCDEF2345G",
        "phone": "+919800000002",
        "monthly_income": 42_000,
        "employment_type": "salaried",
        "existing_emi": 9_500,
        "credit_score": 715,
    },
    {
        # Hard decline: score below the policy floor.
        "full_name": "Imran Qureshi",
        "date_of_birth": "1991-07-21",
        "pan": "CDEFG3456H",
        "phone": "+919800000003",
        "monthly_income": 60_000,
        "employment_type": "self_employed",
        "existing_emi": 4_000,
        "credit_score": 612,
    },
    {
        # Decline: existing obligations exhaust debt-service capacity.
        "full_name": "Lakshmi Narayanan",
        "date_of_birth": "1985-01-30",
        "pan": "DEFGH4567I",
        "phone": "+919800000004",
        "monthly_income": 38_000,
        "employment_type": "salaried",
        "existing_emi": 21_000,
        "credit_score": 760,
    },
    {
        # Sanctions hit: must be routed to a human without explanation.
        "full_name": "Vikram Anand",
        "date_of_birth": "1979-09-15",
        "pan": "EFGHI5678J",
        "phone": "+919800000005",
        "monthly_income": 150_000,
        "employment_type": "self_employed",
        "existing_emi": 0,
        "credit_score": 800,
        "is_sanctioned": True,
    },
]


def seed(reset: bool = False) -> int:
    """Insert seed customers. Idempotent: existing PANs are skipped."""
    init_db()
    inserted = 0
    with SessionLocal() as db:
        if reset:
            db.query(Customer).delete()
            db.commit()
        for row in SEED_CUSTOMERS:
            exists = db.execute(
                select(Customer).where(Customer.pan == row["pan"])
            ).scalar_one_or_none()
            if exists:
                continue
            db.add(Customer(**row))
            inserted += 1
        db.commit()
    return inserted


if __name__ == "__main__":
    count = seed()
    print(f"Seeded {count} customer record(s).")
