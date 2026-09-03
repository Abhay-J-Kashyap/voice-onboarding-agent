"""The web page an applicant lands on from the emailed link.

Rendered as strings rather than through a template engine: there are two pages,
they are small, and adding Jinja to a service that otherwise returns JSON would
be more machinery than the problem needs.

The design job here is trust before anything else. Someone has just come off a
phone call and received an email with a link — the same shape as every phishing
attempt they have been warned about. So the page leads with what was said on the
call: their reference, their name, the masked PAN they read out. A stranger
could not know those, and showing them is the fastest way to prove this is a
continuation rather than a trap. Everything else on the page is deliberately
quiet so that recognition is the first thing that happens.

All interpolated values are escaped: several of them are strings a caller spoke
into a phone, which is to say strings an attacker could choose.
"""

from __future__ import annotations

from html import escape

# Serif for the headings, because the vernacular here is paperwork — the forms
# and letters a lender sends — and a serif says "document" in a way a geometric
# sans does not. System sans for everything interactive, where legibility on a
# mid-range phone in bad light matters more than personality.
_STYLE = """
:root {
  --ink: #14243A;
  --ink-soft: #4A5A70;
  --paper: #FBFAF7;
  --rule: #DFDCD4;
  --verified: #1F6F5C;
  --alert: #A6432E;
  --focus: #2C6FB5;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
.wrap { max-width: 34rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
.mark {
  font: 600 0.9rem/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: 0.01em;
  color: var(--ink-soft);
  padding-bottom: 1.75rem;
}
h1 {
  font-family: Iowan Old Style, Charter, Georgia, "Times New Roman", serif;
  font-size: 1.75rem;
  line-height: 1.2;
  font-weight: 600;
  margin: 0 0 0.5rem;
}
.lede { color: var(--ink-soft); margin: 0 0 2rem; }

/* The recap is the hero: proof we are who we say we are. */
.recap {
  border: 1px solid var(--rule);
  border-left: 3px solid var(--verified);
  background: #fff;
  padding: 1.25rem 1.25rem 0.75rem;
  margin-bottom: 2.25rem;
}
.recap h2 {
  font: 600 0.95rem/1.3 inherit;
  margin: 0 0 0.9rem;
  color: var(--ink-soft);
}
dl { margin: 0; }
.row {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.55rem 0;
  border-top: 1px solid var(--rule);
}
.row:first-of-type { border-top: 0; }
dt { color: var(--ink-soft); }
dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }

fieldset { border: 0; padding: 0; margin: 0 0 1.75rem; }
legend {
  font-family: Iowan Old Style, Charter, Georgia, serif;
  font-size: 1.15rem;
  font-weight: 600;
  padding: 0 0 0.35rem;
}
.field { margin-bottom: 1.15rem; }
label { display: block; margin-bottom: 0.35rem; font-weight: 500; }
.hint { color: var(--ink-soft); font-size: 0.875rem; margin: 0.3rem 0 0; }
input[type=text], input[type=number], select {
  width: 100%;
  padding: 0.7rem 0.75rem;
  border: 1px solid var(--rule);
  border-radius: 3px;
  background: #fff;
  color: var(--ink);
  font: inherit;
}
input:focus-visible, select:focus-visible, button:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.consent {
  display: flex;
  gap: 0.7rem;
  align-items: flex-start;
  border: 1px solid var(--rule);
  background: #fff;
  padding: 1rem;
}
.consent input { margin-top: 0.25rem; flex: 0 0 auto; }
.consent label { font-weight: 400; margin: 0; }
button {
  width: 100%;
  padding: 0.85rem;
  border: 0;
  border-radius: 3px;
  background: var(--ink);
  color: #fff;
  font: 600 1rem inherit;
  cursor: pointer;
}
button:hover { background: #0d1928; }
.errors {
  border: 1px solid var(--alert);
  border-left: 3px solid var(--alert);
  background: #fff;
  padding: 0.9rem 1.1rem;
  margin-bottom: 1.75rem;
  color: var(--alert);
}
.errors ul { margin: 0.4rem 0 0; padding-left: 1.1rem; }
.foot {
  margin-top: 2.5rem;
  padding-top: 1.25rem;
  border-top: 1px solid var(--rule);
  color: var(--ink-soft);
  font-size: 0.875rem;
}
.done .tick {
  font-size: 2rem;
  color: var(--verified);
  line-height: 1;
  margin-bottom: 0.75rem;
}
@media (prefers-reduced-motion: no-preference) {
  .done { animation: rise 320ms ease-out both; }
  @keyframes rise {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: none; }
  }
}
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<!-- The URL is the credential, so it must not travel to third parties in a
     Referer header, and there is nothing here worth embedding elsewhere. -->
<meta name="referrer" content="no-referrer">
<title>{escape(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<div class="mark">Meridian Finance</div>
{body}
</div>
</body>
</html>"""


def application_form(
    *,
    token: str,
    full_name: str,
    reference: str,
    masked_pan: str,
    product: str,
    errors: list[str] | None = None,
    values: dict[str, str] | None = None,
) -> str:
    """The form itself, with the call recap above it."""
    values = values or {}
    product_label = (
        "Personal loan" if product == "personal_loan" else "Credit card"
    )

    error_block = ""
    if errors:
        items = "".join(f"<li>{escape(e)}</li>" for e in errors)
        error_block = (
            '<div class="errors" role="alert">'
            "<strong>Check these before continuing</strong>"
            f"<ul>{items}</ul></div>"
        )

    def keep(name: str) -> str:
        return escape(values.get(name, ""))

    def selected(name: str, option: str) -> str:
        return " selected" if values.get(name) == option else ""

    sal = selected("employment_type", "salaried")
    emp = selected("employment_type", "self_employed")

    body = f"""
<h1>Finish your application</h1>
<p class="lede">A few more details and we can pick up where the call left off.</p>

<section class="recap">
  <h2>From your call with us</h2>
  <dl>
    <div class="row"><dt>Reference</dt><dd>{escape(reference)}</dd></div>
    <div class="row"><dt>Name</dt><dd>{escape(full_name)}</dd></div>
    <div class="row"><dt>PAN</dt><dd>{escape(masked_pan)}</dd></div>
    <div class="row"><dt>Applying for</dt><dd>{product_label}</dd></div>
  </dl>
</section>

{error_block}

<form method="post" action="/apply/{escape(token)}">
  <fieldset>
    <legend>Your work</legend>
    <div class="field">
      <label for="employment_type">Employment</label>
      <select id="employment_type" name="employment_type" required>
        <option value="">Choose one</option>
        <option value="salaried"{sal}>Salaried</option>
        <option value="self_employed"{emp}>Self-employed</option>
      </select>
    </div>
    <div class="field">
      <label for="monthly_income">Monthly income</label>
      <input type="number" id="monthly_income" name="monthly_income" min="1"
             inputmode="numeric" value="{keep('monthly_income')}" required>
      <p class="hint">In rupees, before deductions.</p>
    </div>
  </fieldset>

  <fieldset>
    <legend>Where you live</legend>
    <div class="field">
      <label for="address_line">Address</label>
      <input type="text" id="address_line" name="address_line"
             autocomplete="street-address" value="{keep('address_line')}" required>
    </div>
    <div class="field">
      <label for="city">City</label>
      <input type="text" id="city" name="city" autocomplete="address-level2"
             value="{keep('city')}" required>
    </div>
    <div class="field">
      <label for="pincode">PIN code</label>
      <input type="text" id="pincode" name="pincode" inputmode="numeric"
             autocomplete="postal-code" value="{keep('pincode')}" required>
    </div>
  </fieldset>

  <fieldset>
    <legend>Credit check</legend>
    <div class="consent">
      <input type="checkbox" id="credit_check_consent" name="credit_check_consent"
             value="yes" required>
      <label for="credit_check_consent">
        I agree that Meridian Finance may obtain my credit report to assess
        this application.
      </label>
    </div>
  </fieldset>

  <button type="submit">Submit application</button>
</form>

<p class="foot">
  This link works once and expires 48 hours after we sent it. We will never ask
  for your passwords, card numbers, or one-time codes.
</p>
"""
    return _page("Finish your application", body)


def submitted(*, reference: str) -> str:
    body = f"""
<div class="done">
  <div class="tick" aria-hidden="true">&#10003;</div>
  <h1>Application submitted</h1>
  <p class="lede">
    We have everything we need for now. Your reference is
    <strong>{escape(reference)}</strong>.
  </p>
  <p>
    Someone will review your details and contact you about the identity
    documents we still need. You can close this page.
  </p>
</div>
"""
    return _page("Application submitted", body)


def unavailable(*, heading: str, explanation: str) -> str:
    """One page for every dead link.

    Expired, already used, and never existed all render identically. An
    applicant does not benefit from the distinction, and telling them would let
    someone probing tokens tell a real one from a guess.
    """
    body = f"""
<h1>{escape(heading)}</h1>
<p class="lede">{escape(explanation)}</p>
<p>
  Call us on 1800 000 000 with your reference number and we can send you a
  fresh link or finish the application over the phone.
</p>
"""
    return _page(heading, body)
