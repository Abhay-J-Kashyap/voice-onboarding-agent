"""The emailed application link and the page it opens.

This is the only part of the service a member of the public touches, and the
only part not behind the platform API key. The token in the URL is the whole
credential, so most of what is tested here is what happens when that token is
old, spent, guessed, or belongs to someone else.

The other theme is not losing the applicant. A form that empties itself on a
validation error, or a call that fails because an email bounced, are both ways
to lose a real application to a recoverable problem.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ApplicationLink, ApplicationSubmission, Lead
from app.services import links
from tests.conftest import AUTH

UNKNOWN = {
    "full_name": "Kavya Reddy",
    "date_of_birth": "1992-08-14",
    "pan": "ZZZZZ9999Z",
}
LEAD_DETAILS = {
    **UNKNOWN,
    "email": "kavya.reddy@example.com",
    "product_interest": "personal_loan",
    "stated_monthly_income": 70_000,
}
GOOD_FORM = {
    "employment_type": "salaried",
    "monthly_income": "70000",
    "address_line": "12 MG Road",
    "city": "Bengaluru",
    "pincode": "560001",
    "credit_check_consent": "yes",
}


class CapturingEmailSender:
    """Records what was emailed so tests can read the link back."""

    def __init__(self) -> None:
        self.links: list[tuple[str, str, str]] = []
        self.should_fail = False

    def send_passcode(self, *, email: str, code: str, ttl_minutes: int) -> bool:
        return True

    def send_application_link(
        self, *, email: str, full_name: str, reference: str, url: str
    ) -> bool:
        if self.should_fail:
            return False
        self.links.append((email, reference, url))
        return True

    @property
    def last_token(self) -> str:
        return self.links[-1][2].rsplit("/", 1)[-1]


@pytest.fixture
def mailer(monkeypatch):
    from app.services import email as email_module

    sender = CapturingEmailSender()
    monkeypatch.setattr(email_module, "_sender", sender)
    return sender


@pytest.fixture
def captured_lead(client, session_id, mailer) -> tuple[str, str]:
    """A completed prospect call. Returns (session_id, token)."""
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    response = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    ).json()
    assert response["data"]["link_emailed"] is True
    return session_id, mailer.last_token


def test_capturing_a_lead_emails_a_link(client, session_id, mailer):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    body = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    ).json()

    assert len(mailer.links) == 1
    to_address, reference, url = mailer.links[0]
    assert to_address == "kavya.reddy@example.com"
    assert reference == body["data"]["lead_reference"]
    assert "/apply/" in url


def test_delivery_failure_does_not_fail_the_call(client, session_id, mailer):
    """The lead is the durable record; the email is a convenience on top.

    Telling a caller their application failed because an email bounced would be
    both wrong and alarming, and would lose a lead that is safely on file.
    """
    mailer.should_fail = True
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    body = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    ).json()

    assert body["outcome"] == "ok"
    assert body["data"]["link_emailed"] is False
    assert "colleague" in body["agent_message"]


def test_page_shows_what_was_said_on_the_call(client, captured_lead):
    """The recap is the trust signal: a stranger could not know these."""
    _, token = captured_lead
    page = client.get(f"/apply/{token}").text

    assert "Kavya Reddy" in page
    assert "LEAD-" in page
    assert "ZZ*****99Z" in page
    assert "Personal loan" in page


def test_page_never_shows_the_full_pan(client, captured_lead):
    _, token = captured_lead
    page = client.get(f"/apply/{token}").text
    assert "ZZZZZ9999Z" not in page


def test_page_needs_no_api_key(client, captured_lead):
    """The applicant is a member of the public holding a link."""
    _, token = captured_lead
    assert client.get(f"/apply/{token}").status_code == 200


def test_page_is_not_cached_or_indexed(client, captured_lead):
    _, token = captured_lead
    response = client.get(f"/apply/{token}")
    assert "no-store" in response.headers["cache-control"]
    assert "noindex" in response.headers["x-robots-tag"]


def test_submission_is_recorded_and_tied_to_the_call(
    client, captured_lead, db_session
):
    session_id, token = captured_lead
    page = client.post(f"/apply/{token}", data=GOOD_FORM).text
    assert "Application submitted" in page

    submission = db_session.execute(select(ApplicationSubmission)).scalars().first()
    assert submission is not None
    assert submission.session_id == session_id
    assert submission.monthly_income == 70_000
    assert submission.pincode == "560001"
    assert submission.credit_check_consent is True

    lead = db_session.execute(select(Lead)).scalars().first()
    assert submission.lead_id == lead.id


def test_link_works_only_once(client, captured_lead):
    _, token = captured_lead
    client.post(f"/apply/{token}", data=GOOD_FORM)

    assert "no longer active" in client.get(f"/apply/{token}").text
    assert "no longer active" in client.post(f"/apply/{token}", data=GOOD_FORM).text


def test_second_submission_creates_no_second_record(
    client, captured_lead, db_session
):
    _, token = captured_lead
    client.post(f"/apply/{token}", data=GOOD_FORM)
    client.post(f"/apply/{token}", data=GOOD_FORM)

    rows = db_session.execute(select(ApplicationSubmission)).scalars().all()
    assert len(rows) == 1


def test_expired_link_is_refused(client, captured_lead, db_session):
    _, token = captured_lead
    link = db_session.execute(select(ApplicationLink)).scalars().first()
    link.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    assert "no longer active" in client.get(f"/apply/{token}").text


def test_guessed_token_is_refused(client):
    assert "no longer active" in client.get("/apply/notarealtoken").text


def test_every_rejection_looks_identical(client, captured_lead, db_session):
    """Expired, spent and never-existed must be indistinguishable.

    An applicant gains nothing from the difference, and someone probing tokens
    would gain a way to tell a real one from a guess.
    """
    _, token = captured_lead
    guessed = client.get("/apply/notarealtoken").text

    client.post(f"/apply/{token}", data=GOOD_FORM)
    spent = client.get(f"/apply/{token}").text

    assert guessed == spent


def test_token_is_not_stored_in_the_clear(client, captured_lead, db_session):
    _, token = captured_lead
    link = db_session.execute(select(ApplicationLink)).scalars().first()
    assert token not in link.token_digest
    assert len(link.token_digest) == 64
    assert link.token_digest == links.digest(token)


def test_opening_the_page_is_recorded(client, captured_lead, db_session):
    """Useful for chasing applicants who were emailed but never opened it."""
    _, token = captured_lead
    link = db_session.execute(select(ApplicationLink)).scalars().first()
    assert link.opened_at is None

    client.get(f"/apply/{token}")
    db_session.refresh(link)
    assert link.opened_at is not None


def test_missing_consent_is_refused(client, captured_lead):
    _, token = captured_lead
    form = {k: v for k, v in GOOD_FORM.items() if k != "credit_check_consent"}
    page = client.post(f"/apply/{token}", data=form).text
    assert "Tick the box" in page


def test_bad_pincode_is_refused(client, captured_lead):
    _, token = captured_lead
    page = client.post(f"/apply/{token}", data={**GOOD_FORM, "pincode": "99"}).text
    assert "six digit PIN code" in page


def test_non_numeric_income_is_refused(client, captured_lead):
    _, token = captured_lead
    page = client.post(
        f"/apply/{token}", data={**GOOD_FORM, "monthly_income": "lots"}
    ).text
    assert "monthly income as a number" in page


def test_validation_error_keeps_the_applicants_answers(client, captured_lead):
    """A form that empties itself is how a real application gets abandoned."""
    _, token = captured_lead
    page = client.post(
        f"/apply/{token}", data={**GOOD_FORM, "pincode": "99"}
    ).text
    assert 'value="70000"' in page
    assert 'value="Bengaluru"' in page
    assert 'value="12 MG Road"' in page


def test_failed_validation_does_not_spend_the_link(client, captured_lead):
    _, token = captured_lead
    client.post(f"/apply/{token}", data={**GOOD_FORM, "pincode": "99"})
    # The link must still work: the applicant did nothing wrong except mistype.
    assert "Finish your application" in client.get(f"/apply/{token}").text


def test_failed_validation_records_nothing(client, captured_lead, db_session):
    _, token = captured_lead
    client.post(f"/apply/{token}", data={**GOOD_FORM, "pincode": "99"})
    rows = db_session.execute(select(ApplicationSubmission)).scalars().all()
    assert rows == []


def test_page_escapes_values_the_caller_chose(client, session_id, mailer):
    """Name and email came from someone speaking into a phone."""
    hostile = {
        "full_name": "Kavya <script>alert(1)</script>",
        "date_of_birth": "1992-08-14",
        "pan": "ZZZZZ9999Z",
    }
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **hostile},
        headers=AUTH,
    )
    client.post(
        "/v1/tools/capture_lead",
        json={
            "session_id": session_id,
            **hostile,
            "email": "kavya@example.com",
            "product_interest": "personal_loan",
        },
        headers=AUTH,
    )
    page = client.get(f"/apply/{mailer.last_token}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
