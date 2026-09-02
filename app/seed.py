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
        "email": "abjkashyap@gmail.com",
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
        "email": "priya.sharma@example.com",
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
        "email": "imran.qureshi@example.com",
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
        "email": "lakshmi.narayanan@example.com",
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
        "email": "vikram.anand@example.com",
        "monthly_income": 150_000,
        "employment_type": "self_employed",
        "existing_emi": 0,
        "credit_score": 800,
        "is_sanctioned": True,
    },
    # The four below share the clean-approval profile. They exist because
    # passcode issuance is rate limited per customer, so evaluation scenarios
    # that each need a fresh code would otherwise collide with one another
    # inside the rate window.
    {
        "full_name": "Anita Desai",
        "date_of_birth": "1990-02-18",
        "pan": "FGHIJ6789K",
        "phone": "+919800000006",
        "email": "anita.desai@example.com",
        "monthly_income": 88_000,
        "employment_type": "salaried",
        "existing_emi": 5_000,
        "credit_score": 771,
    },
    {
        "full_name": "Suresh Menon",
        "date_of_birth": "1986-06-09",
        "pan": "GHIJK7890L",
        "phone": "+919800000007",
        "email": "suresh.menon@example.com",
        "monthly_income": 110_000,
        "employment_type": "salaried",
        "existing_emi": 12_000,
        "credit_score": 795,
    },
    {
        "full_name": "Fatima Sheikh",
        "date_of_birth": "1993-12-24",
        "pan": "HIJKL8901M",
        "phone": "+919800000008",
        "email": "fatima.sheikh@example.com",
        "monthly_income": 76_000,
        "employment_type": "salaried",
        "existing_emi": 3_000,
        "credit_score": 758,
    },
    {
        "full_name": "Arjun Nair",
        "date_of_birth": "1989-03-05",
        "pan": "IJKLM9012N",
        "phone": "+919800000009",
        "email": "arjun.nair@example.com",
        "monthly_income": 92_000,
        "employment_type": "salaried",
        "existing_emi": 6_500,
        "credit_score": 767,
    },
]


def seed(reset: bool = False) -> int:
    """Insert seed customers. Idempotent: existing PANs are backfilled, not skipped.

    A prior deploy's row surviving a redeploy is the normal case on a
    persistent disk, and a schema addition with no matching data migration
    will otherwise leave it with nulls in every new column forever. Backfilling
    only fields that are currently unset — never overwriting a value someone
    might have edited — keeps this safe to run against a live database.
    """
    init_db()
    inserted = 0
    updated = 0
    with SessionLocal() as db:
        if reset:
            db.query(Customer).delete()
            db.commit()
        for row in SEED_CUSTOMERS:
            existing = db.execute(
                select(Customer).where(Customer.pan == row["pan"])
            ).scalar_one_or_none()
            if existing is None:
                db.add(Customer(**row))
                inserted += 1
                continue
            changed = False
            for field, value in row.items():
                if getattr(existing, field, None) in (None, "") and value not in (
                    None,
                    "",
                ):
                    setattr(existing, field, value)
                    changed = True
            if changed:
                updated += 1
        db.commit()
    if updated:
        print(f"Backfilled {updated} existing record(s) with new fields.")
    return inserted


if __name__ == "__main__":
    count = seed()
    print(f"Seeded {count} customer record(s).")