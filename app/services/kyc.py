"""Identity verification against the reference customer record.

Matching is deliberately tolerant on name (speech-to-text mangles Indian names
routinely) and strict on PAN and date of birth, which are spelled out digit by
digit and should match exactly.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer


def normalise_name(value: str) -> str:
    """Fold case, accents and honorifics so 'Mr. Rajesh  Kumar' matches 'Rajesh Kumar'."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    tokens = [t for t in stripped.lower().replace(".", " ").split() if t]
    honorifics = {"mr", "mrs", "ms", "shri", "smt", "dr"}
    return " ".join(t for t in tokens if t not in honorifics)


def name_similarity(spoken: str, stored: str) -> float:
    """Token overlap ratio. Cheap, explainable, and good enough for a demo lender.

    A production system would use a phonetic index (Soundex/Metaphone tuned for
    Indic names) — the interface here is deliberately narrow so that swap is
    a one-function change.
    """
    spoken_tokens = set(normalise_name(spoken).split())
    stored_tokens = set(normalise_name(stored).split())
    if not spoken_tokens or not stored_tokens:
        return 0.0
    return len(spoken_tokens & stored_tokens) / len(stored_tokens)


NAME_MATCH_THRESHOLD = 0.5


@dataclass(frozen=True)
class VerificationResult:
    matched: bool
    customer: Customer | None
    failure_reason: str | None = None


def verify_identity(
    db: Session, *, full_name: str, date_of_birth: date, pan: str
) -> VerificationResult:
    """Look up by PAN, then corroborate with date of birth and name.

    Failure reasons are returned rather than raised: the caller decides whether
    a mismatch is a retry or a block, based on attempts already consumed.
    """
    customer = db.execute(
        select(Customer).where(Customer.pan == pan)
    ).scalar_one_or_none()

    if customer is None:
        return VerificationResult(False, None, "pan_not_found")

    if customer.is_sanctioned:
        # Never disclose a sanctions hit to the caller; route it to a human.
        return VerificationResult(False, None, "sanctions_hit")

    if customer.date_of_birth != date_of_birth.isoformat():
        return VerificationResult(False, None, "dob_mismatch")

    if name_similarity(full_name, customer.full_name) < NAME_MATCH_THRESHOLD:
        return VerificationResult(False, None, "name_mismatch")

    return VerificationResult(True, customer)
