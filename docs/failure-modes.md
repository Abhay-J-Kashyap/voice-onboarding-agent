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
| Speech-to-text garbles a PAN | 422 with `outcome: "retry"` and "could you say that once more" — recoverable, not fatal (S13) |
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

## Known gaps

Listed rather than hidden, because a reviewer will find them anyway.

- **No rate limiting.** A compromised platform key could enumerate PANs against
  `verify_identity`. Production needs per-key limits and alerting on verification
  failure rates.
- **`create_all` instead of migrations.** Fine for a single-instance demo,
  wrong the moment a second instance or a schema change appears. Alembic is the
  fix.
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