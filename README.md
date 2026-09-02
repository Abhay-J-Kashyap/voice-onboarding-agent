# Voice onboarding agent — tool and orchestration layer

A production-shaped backend for a BFSI voice agent that takes a customer through
loan onboarding over the phone: verify identity with two factors, assess
eligibility, explain terms, capture consent, and hand off to a human when it
should.

The voice pipeline runs on Sarvam's managed Voice Agents platform. **This
repository is everything the platform calls into** — the state machine, the credit
policy, the audit trail, the observability, and the evaluation harness.

```
Caller ──▶ Sarvam Voice Agents ──▶ this service ──▶ Postgres / SQLite
           (STT, LLM, TTS,          (state machine,
            barge-in, telephony)     policy, audit)
```

## The idea

A voice agent that only talks is a demo. One that completes work has to be
trusted with decisions, and a language model is the wrong place to put a
guarantee. So the split is:

- **The model owns the conversation** — wording, tone, language, judgement about
  when to give up.
- **The service owns everything that must be true** — what step comes next, what
  the credit decision is, how many attempts a caller gets, what was consented to.

The prompt describes the flow. The service enforces it. A model that skips
verification and jumps to consent gets a 409 and a recoverable message, not a
polite reminder.

## Quick start

```bash
pip install -r requirements-dev.txt
make run                                   # seeds the database and serves on :8000
```

In another shell:

```bash
make test                                  # 46 unit and integration tests
make evals                                 # 15 scenarios against the live service
```

API docs at `http://localhost:8000/docs`. Docker: `make docker`.

## Trying it end to end

```bash
API=http://localhost:8000
KEY='x-api-key: local-dev-key'

SID=$(curl -s -X POST $API/v1/sessions -H "$KEY" -H 'content-type: application/json' \
  -d '{"external_call_id":"demo-1"}' | jq -r .session_id)

curl -s -X POST $API/v1/tools/verify_identity -H "$KEY" -H 'content-type: application/json' \
  -d "{\"session_id\":\"$SID\",\"full_name\":\"Mr. Priya Sharma\",
       \"date_of_birth\":\"1994-11-03\",\"pan\":\"bcdef 2345 g\"}" | jq .

curl -s -X POST $API/v1/tools/check_eligibility -H "$KEY" -H 'content-type: application/json' \
  -d "{\"session_id\":\"$SID\",\"product_code\":\"personal_loan\",
       \"requested_amount\":1500000,\"tenure_months\":36,
       \"declared_monthly_income\":42000,\"employment_type\":\"salaried\"}" | jq .
```

The name arrives with an honorific and the PAN arrives lower-case with spaces,
the way speech-to-text actually delivers them. Both normalise. The second call
asks for ₹15,00,000 and gets a counter-offer sized to fit the caller's debt-service
headroom:

> Good news. You are eligible for 275,000 rupees over 36 months, at 14.0 percent
> a year. That works out to about 9,399 rupees a month.

Then `curl -s -H "$KEY" $API/v1/sessions/$SID | jq .` returns the redacted,
replayable record of the whole call.

## Design decisions worth explaining

**Two-factor identity, split across two states.** Matching PAN, date of birth
and name proves only that the caller knows details printed on a card. A passcode
sent to the registered mobile proves possession of the phone. Those are
different claims, so they get different states: `identity_matched` then
`identity_verified`. Eligibility is unreachable from the first, which means a
model that treats a located record as a verified caller is refused by the
service rather than corrected by the prompt.

Passcodes are stored as salted digests and never in plaintext, retired on
resend, single use, expiring, capped on attempts, and rate limited **per
customer rather than per session** — sessions are free to create, so a
per-session cap would be no cap at all.

Delivery sits behind an interface that passes the code and its lifetime rather
than a finished message, because India's DLT regime means commercial SMS is sent
as a registered template id plus variables, not as free text. Two channels are
built to the same pattern: `Msg91SmsSender` sends SMS through a DLT-registered
template, `ResendEmailSender` sends email through Resend's API. SMS needs
business registration to actually deliver in India; email does not, which is
why `OTP_DELIVERY_CHANNEL=email` is what this project's demo actually runs on.
`IssueResult` carries both a masked phone and a masked email and only ever
populates the one the active channel used, so the response layer phrases the
message correctly without knowing which provider ran. The retry budget on both
channels is deliberately tight — three second timeout, one retry, a worst case
inside the voice platform's ten second tool timeout, because a customer is on
the line while it runs.

**Server-side state machine.** `SessionState` has an explicit transition table,
and every tool call passes `require_state`. Escalation is reachable from every
live state by design — if the agent decides it is out of its depth, the service
should not argue.

**Deterministic, versioned credit policy.** `app/services/eligibility.py` is pure
functions: real amortising EMI maths, credit bands, debt-service headroom,
counter-offers when a request is unaffordable. Every decision carries a reason
list and a `policy_version`, so a decision made last month can be explained with
last month's rules. `referred` is a first-class outcome — anything the rules
cannot settle cleanly goes to a human rather than being forced into a yes or no.

**One response envelope, written to be spoken.** Every tool returns an `outcome`
the agent branches on and an `agent_message` it can say verbatim. Failures use the
same envelope as successes, including validation errors — a garbled PAN returns
"could you say that once more", because bad input usually means the caller was
misheard and the recovery is to ask again, not to fail the call.

**PII redacted at the point of logging.** `mask_pan` runs before the value reaches
a log line or an audit row, so an unredacted PAN never touches disk.

**Idempotency on every tool.** Telephony platforms retry on timeout. A retried
verification must not consume an attempt, and a retried hand-off must not open a
second ticket.

## Three bugs, and what each one changed

Both only appear when calls arrive in quick succession against a real server, so
the unit tests missed both. They are the most useful thing in this repository.

**Writes were not durable when the agent was told they were.** The database
session committed in FastAPI's dependency teardown, which runs *after* the
response reaches the caller. A fast agent could issue its next tool call against
state the previous one had not committed. Fixed by committing before returning,
with a unique constraint as the backstop for genuinely concurrent retries, and a
savepoint so a collision cannot poison the surrounding transaction.

**The replay check ran after the state guard.** A hand-off that succeeded and then
timed out would be retried against a session it had already moved to a terminal
state — and rejected as out-of-order, breaking the exact case idempotency exists
to handle. The replay check now runs first.

The generalisable rule: in a retry-safe handler, *identify the request, check
whether it already happened, then check whether it is allowed.* Both bugs have
regression tests in `tests/test_idempotency.py` and scenarios in the harness.

**A computed value was never persisted — and the harness missed it too.** The
eligibility engine calculated the monthly instalment correctly, and
`check_eligibility` returned it to the agent, which spoke it to the caller. But
`EligibilityAssessment` had no column for it, the insert never passed it, and the
audit endpoint hard-coded `monthly_instalment=0`. Every test passed. The caller
heard the right number and the permanent record held a zero.

The interesting part is why nothing caught it. Both layers only ever checked what
the service *said*, never what it *stored* — so a field that was computed right,
spoken right, and written wrong was invisible to all of them. The fix was three
lines; closing the gap was the real work.

`run_evals.py` now fetches `GET /v1/sessions/{id}` after every scenario and
cross-checks the durable record against the live responses: the tool sequence,
the final state, every eligibility figure, consent evidence, the escalation
ticket reference, and that no unredacted PAN appears in the stored payload. The
checks are derived from the scenario definition, so new scenarios are covered
automatically.

Reintroducing the bug drops the suite from 15/15 to 11/15 with 8 failing
cross-checks — which is the only way to know an eval is worth having.

**A rejection the agent could not act on.** A live call failed when a caller
asked for a ten year tenure: the schema capped it at seven, and the validation
handler returned a generic "I did not catch that" naming no field. The agent had
no way to know *which* value was wrong, retried the same rejected tenure, failed
identically, and escalated a call that never needed a human. The transcript is a
textbook avoidable failure — every tool call correct, every decision reasonable,
and the call still lost.

Two fixes. The ceiling moved to 120 months, because a decade is a real personal
loan term. More importantly, validation errors now name the offending field and
its acceptable range in speakable wording — "the repayment period needs to be
between six months and ten years" — so a recoverable input error is actually
recoverable. `FIELD_GUIDANCE` sits beside the field definitions it describes,
and a test fails if a new constrained field arrives without its recovery wording,
so the gap cannot silently reopen.

The harness gained an assertion type for this: a step can require substrings in
the spoken message, checking that a response is *actionable* rather than merely
correctly shaped. Reverting the fix drops the suite to 20/22.

## Evaluation

`evals/scenarios.py` holds 15 caller personas as data — each one a scripted tool
sequence plus the outcome policy requires, and a one-line note on what it protects
against. The runner drives them against a live service and emits a scored report
with latency percentiles.

The harness scores two layers. The **live layer** checks each response: right
outcome, right state, right data, and a speakable message. The **audit layer**
then fetches the session record and cross-checks it against what the live calls
claimed, catching values that are computed and spoken correctly but stored
wrongly.

Latest run: **22/22 scenarios**, **82/82 audit cross-checks**, p50 5.6ms, p95
7.5ms across 57 tool calls.

Ten scenarios need the service to echo the passcode back, which is only safe
locally. Run them with `OTP_DEMO_MODE=true`. Against a service without it, those
scenarios report as skipped rather than failing, so the same suite is meaningful
in both configurations.

Scenarios cover clean approval, counter-offer, hard decline, misheard-then-corrected
identity, exhausted attempts, sanctions hit, refused consent, disputed decision,
immediate hand-off, out-of-order tool calls, post-escalation writes, timeout
retries, malformed speech-to-text output, and suspicious income declarations.

Adding a regression case for a production bug means appending a dict.

Conversational quality — whether the agent *said* the right thing — is scored by
hand against `evals/rubric.md`. That gap is deliberate and documented: the service
can refuse an action, but it cannot refuse a sentence.

## Layout

```
app/
  main.py              app factory, exception handlers
  models.py            ORM models, session state machine
  schemas.py           request/response contracts, input validation
  observability.py     structured logging, trace propagation, PII masking
  services/
    sessions.py        state guards, idempotency, audit
    kyc.py             identity matching
    otp.py             passcode issuance, verification, rate limiting
    sms.py             SMS delivery; console and MSG91 implementations
    email.py           email delivery; console and Resend implementations
    eligibility.py     credit policy engine
    handoff.py         consent records, escalation routing
  routers/
    tools.py           the six agent-facing tools
    admin.py           health checks, session audit
agent/
  system_prompt.md     the Sarvam agent prompt
  tools.json           live-action tool schemas
evals/
  scenarios.py         15 personas as data
  run_evals.py         runner and report generator
  rubric.md            manual conversational scoring
docs/
  architecture.md      design rationale
  failure-modes.md     what breaks, and what is knowingly unhandled
  runbook.md           operating the thing
```

## Deploying

SQLite is the default so the service runs with zero infrastructure. For anything
real, set `DATABASE_URL` to Postgres — the data layer is plain SQLAlchemy 2.0 with
no SQLite-specific SQL.

Known gaps are listed honestly in `docs/failure-modes.md`: no rate limiting,
`create_all` instead of Alembic migrations, no transcript-level automated scoring,
and no PII retention policy. Each has a stated fix.

## Connecting the voice agent

1. Deploy this service somewhere with a public HTTPS URL.
2. Create a blank agent in the Sarvam Voice Agents console.
3. Paste `agent/system_prompt.md` into the prompt field.
4. Register the six tools from `agent/tools.json` as live actions, with the API
   key in the header and the platform's call id forwarded as `x-trace-id` so
   telephony and tool logs share one identifier.
5. Place a test call, then `GET /v1/sessions/{id}` to see exactly what happened.
