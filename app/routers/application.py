"""The web half of the application flow.

These are the only routes in the service that serve HTML and the only ones not
behind the platform API key: the applicant is a member of the public holding a
link, not the voice platform holding a shared secret. The token in the URL is
the entire credential, which is why `links.resolve` checks expiry and prior use
on every request rather than trusting that a URL which once worked still should.

Every failure renders the same page. An applicant gains nothing from learning
whether a link expired or never existed, and someone probing tokens would gain
a way to tell a real one from a guess.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import templates
from app.db import get_db
from app.models import ApplicationSubmission
from app.observability import log_event, mask_pan
from app.services import links

router = APIRouter(tags=["application"], include_in_schema=False)

#: Rejections are deliberately indistinguishable from one another.
DEAD_LINK = {
    "heading": "This link is no longer active",
    "explanation": (
        "It may have expired, or it may already have been used to submit an "
        "application."
    ),
}


def _dead_link_response() -> HTMLResponse:
    # 200 rather than 404: this is a real page telling a real person what to do
    # next, and a status code would not change that. It also declines to
    # confirm whether the token exists.
    return HTMLResponse(templates.unavailable(**DEAD_LINK), status_code=200)


def _no_store(response: HTMLResponse) -> HTMLResponse:
    """Keep the page out of shared caches and browser history stores.

    The URL carries the credential and the page shows personal details, so
    neither belongs in an intermediary's cache.
    """
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@router.get("/apply/{token}", response_class=HTMLResponse)
def show_application(
    token: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render the form, or the dead-link page."""
    resolution = links.resolve(db, token)

    if resolution.status is not links.LinkStatus.VALID:
        log_event(
            "application_link_rejected",
            level=logging.INFO,
            reason=resolution.status.value,
        )
        return _no_store(_dead_link_response())

    links.mark_opened(db, resolution.link)
    db.commit()

    lead = resolution.lead
    log_event(
        "application_link_opened",
        session_id=resolution.link.session_id,
        reference=lead.reference,
    )
    return _no_store(
        HTMLResponse(
            templates.application_form(
                token=token,
                full_name=lead.full_name,
                reference=lead.reference,
                masked_pan=mask_pan(lead.pan) or "",
                product=lead.product_interest,
            )
        )
    )


@router.post("/apply/{token}", response_class=HTMLResponse)
def submit_application(
    token: str,
    db: Session = Depends(get_db),
    employment_type: str = Form(""),
    monthly_income: str = Form(""),
    address_line: str = Form(""),
    city: str = Form(""),
    pincode: str = Form(""),
    credit_check_consent: str = Form(""),
) -> HTMLResponse:
    """Validate and record a submission, then retire the link.

    Fields arrive as strings and are validated here rather than by Pydantic,
    because a form needs to be re-rendered with the applicant's own answers
    still in it — a 422 that empties the page is how people abandon
    applications.
    """
    resolution = links.resolve(db, token)
    if resolution.status is not links.LinkStatus.VALID:
        log_event(
            "application_submit_rejected",
            level=logging.INFO,
            reason=resolution.status.value,
        )
        return _no_store(_dead_link_response())

    lead = resolution.lead
    submitted = {
        "employment_type": employment_type.strip(),
        "monthly_income": monthly_income.strip(),
        "address_line": address_line.strip(),
        "city": city.strip(),
        "pincode": pincode.strip(),
    }
    errors: list[str] = []

    if submitted["employment_type"] not in {"salaried", "self_employed"}:
        errors.append("Choose whether you are salaried or self-employed.")

    income = 0
    if not submitted["monthly_income"].isdigit():
        errors.append("Enter your monthly income as a number.")
    else:
        income = int(submitted["monthly_income"])
        if income <= 0:
            errors.append("Enter your monthly income as a number.")
        elif income > 10_000_000:
            errors.append("Enter a monthly income below one crore.")

    if len(submitted["address_line"]) < 5:
        errors.append("Enter your address.")
    if not submitted["city"]:
        errors.append("Enter your city.")

    pin = "".join(c for c in submitted["pincode"] if c.isdigit())
    if len(pin) != 6:
        errors.append("Enter a six digit PIN code.")

    if credit_check_consent != "yes":
        errors.append("Tick the box to allow the credit check.")

    if errors:
        return _no_store(
            HTMLResponse(
                templates.application_form(
                    token=token,
                    full_name=lead.full_name,
                    reference=lead.reference,
                    masked_pan=mask_pan(lead.pan) or "",
                    product=lead.product_interest,
                    errors=errors,
                    values=submitted,
                ),
                status_code=400,
            )
        )

    db.add(
        ApplicationSubmission(
            lead_id=lead.id,
            session_id=resolution.link.session_id,
            employment_type=submitted["employment_type"],
            monthly_income=income,
            address_line=submitted["address_line"],
            city=submitted["city"],
            pincode=pin,
            credit_check_consent=True,
        )
    )
    # Retire the link in the same transaction as the submission it authorised.
    # Consuming it separately would leave a window where a resubmission could
    # land twice, or where a crash loses the record but spends the link.
    links.consume(db, resolution.link)
    db.commit()

    log_event(
        "application_submitted",
        session_id=resolution.link.session_id,
        reference=lead.reference,
        employment_type=submitted["employment_type"],
    )
    return _no_store(HTMLResponse(templates.submitted(reference=lead.reference)))
