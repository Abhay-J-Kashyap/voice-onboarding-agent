# Failure modes

What breaks, what happens when it does, and what is deliberately left unhandled.

## Model failures

| Failure | Handling | Evidence |
| --- | --- | --- |
| Skips verification, jumps to consent | 409 `invalid_transition`, speakable recovery message; the call continues | S10 |
| Keeps talking after a hand-off | 409 `session_closed`; terminal sessions reject all writes | S11 |
| Exceeds the verification attempt budget | 403 `attempts_exhausted`; the counter lives in the service, not the prompt | S05 |
| Invents an interest rate or approved amount | Not preventable at the API. Mitigated: the prompt forbids stating numbers not returned by a tool, and `agent_message` is written to be spoken verbatim so there is no reason to paraphrase | — |
| Argues with a decline instead of escalating | Not preventable. Mitigated by prompt instruction and by making escalation valid from every state | S08 |

The last two are the honest gap. The service can refuse an action; it cannot
refuse a sentence. Detecting a hallucinated number requires scoring transcripts
against tool outputs after the call — see "Not yet built" below.

## Input failures

| Failure | Handling |
| --- | --- |
| Speech-to-text garbles a PAN | 422 with `outcome: "retry"` naming the field and its format, so the agent asks for the right thing (S13) |
| Caller asks for something outside policy | 422 naming the field and its acceptable range in speakable wording (S21, S22). A generic apology here once cost a live call: the agent retried the rejected value and escalated |
| PAN spoken with spaces or lower case | Normalised server side; the field accepts the raw utterance and the validator cleans it before checking shape |
| Name mangled by transcription | Token-overlap match at a 0.5 threshold, with honorifics stripped. Tolerant on name, strict on PAN and date of birth |
| Caller is a minor, or the date is absurd | Rejected at the schema boundary before any database work |

Name matching is the weakest link. Token overlap handles "Mr. Rajesh Kumar" and
word order, but not phonetic errors — "Rajesh" transcribed as "Rajish" fails.
Production would need a phonetic index tuned for Indic names. `name_similarity`
is a single function with a narrow interface precisely so that swap is contained.

## Infrastructure failures

| Failure | Handling |
| --- | --- |
| Platform retries a tool call after a timeout | Idempotency key returns the stored response; a retried verification does not consume an attempt (S12), and a retried hand-off returns the original ticket (S15) |
| Two retries race past the pre-flight check | `uq_tool_idempotency` rejects the second; the savepoint contains the collision and the whole attempt is rolled back in favour of the durable result |
| Database unreachable | `/readyz` fails so the load balancer stops routing; in-flight calls get the generic handler's "technical problem" message and should escalate |
| Unhandled exception | Caught by the catch-all handler. Returns a speakable message and never leaks an internal error to a caller |
| Service restarts mid-call | Session state is in the database, not in memory, so a subsequent tool call resumes correctly |

## Security and privacy

| Concern | Handling |
| --- | --- |
| Unauthenticated tool calls | Shared secret compared in constant time; every tool route requires it |
| PAN in logs | Masked at the point of logging by `mask_pan`; only the masked form is persisted in `request_digest` |
| Sanctions hit disclosed to the caller | Never explained. Returns the same generic "cannot verify over the phone" wording as any other block, and routes to `kyc_review` |
| Consent inferred from silence | Prompt requires an explicit yes; `granted: false` is a legitimate recorded outcome rather than a failure to retry (S07) |

## Passcode failures

| Failure | Handling |
| --- | --- |
| Caller mishears a digit | retry with attempts remaining; capped at three (S16, S17) |
| Caller never receives the code | `resend_otp` issues a new one and retires the old (S18) |
| Code expires mid-call | retry with an offer to resend |
| Code overheard on a recorded line | single use; `consumed_at` is set on first success |
| Attacker uses the agent to spam a phone | issuance capped per customer inside a rolling window |
| SMS provider outage | one bounded retry on a timeout or 5xx, then `error` and a hand-off; the challenge is not silently lost |
| Misconfigured template or sender id | 4xx fails immediately without retrying, since retrying cannot fix configuration |
| Provider slow while a caller waits | timeout 3s with one retry, a worst case of about 6.5s, inside the platform's 10s tool timeout |
| Customer has no address on the configured channel | `DELIVERY_FAILED` and a hand-off, rather than a silent no-op |
| Database disclosure | only salted SHA-256 digests are stored, so no live codes are recoverable |

The passcode is never placed in `agent_message`, so the model has no way to read
it aloud even by accident. In demo mode it appears in `data.demo_otp`, which the
Sarvam response template does not forward to the model — that setting exists so
a live demo can run without an SMS provider and must be off in production.

## Two delivery channels, one interface

`OTP_DELIVERY_CHANNEL` selects SMS or email; the rest of the system is
unaware which one is active. `IssueResult` carries both `masked_phone` and
`masked_email` and only ever populates the one that applies, so the router
decides how to phrase the message without knowing which provider ran.

SMS needs DLT registration as a business before it sends for real in India,
which an individual developer cannot complete. Email needs nothing beyond an
API key — Resend's sandbox sender works against the account holder's own
address with no domain verification — which is why it is the channel actually
used for this project's demo, with SMS built, tested, and ready for a
deployment that has done the paperwork.

## An accepted trade-off: the enumeration oracle

Distinguishing `not_registered` from `retry` is a deliberate information leak.
Before, every failure looked alike; now a caller can learn whether a given PAN
belongs to a customer. That is a real enumeration oracle, and it is the price of
not telling genuine prospects they got their own details wrong.

The mitigation is rate limiting on `verify_identity`, which is listed below as
outstanding. Until that exists, this endpoint should not be considered safe
against a determined attacker holding a valid platform key — the leak is
accepted knowingly, not overlooked.

## Known gaps

Listed rather than hidden, because a reviewer will find them anyway.

- **No rate limiting on `verify_identity` itself.** Passcode issuance is now
  rate limited per customer, which stops the agent being used to flood a
  stranger's phone. The record lookup is not: a compromised platform key could
  still enumerate PANs and learn which are customers. Production needs per-key
  limits and alerting on verification failure rates.
- ~~`create_all` instead of migrations.~~ **Fixed.** Alembic owns the schema,
  deploys run `upgrade head`, and CI fails if a model change lands without a
  matching migration. This became urgent rather than theoretical once the data
  had to survive a restart: three schema additions had already needed manual
  repair, each getting away with it only because the disk kept being wiped.
- **No transcript-level scoring.** The harness scores the tool layer
  deterministically, and cross-checks the stored record against the live
  responses. Whether the agent *said* the right thing is still scored by hand
  against `evals/rubric.md`. Automating it means diffing spoken numbers against
  tool responses per turn.

  This gap was narrower than it looked, and closing part of it exposed a real
  bug: the monthly instalment was computed correctly, returned correctly, and
  never persisted, because every check only inspected what the service said
  rather than what it wrote. The audit cross-checks in `run_evals.py` now cover
  that class of failure — sequence, state, eligibility figures, consent,
  escalation references, and PAN redaction in the stored payload.
- **Shared secret, not mTLS or signed requests.** Right weight for one trusted
  caller; wrong if the tool surface is ever exposed more widely.
- **SQLite in the default configuration.** Adequate for one instance, not for
  concurrent writers.
- **No PII retention policy.** `verbatim_response` stores what the caller said
  and is kept indefinitely. Real deployment needs a retention window and a
  deletion path.
