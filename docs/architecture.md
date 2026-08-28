# Architecture

## Shape of the system

```
  Caller (phone / browser)
          │
          ▼
  Sarvam Voice Agents          managed: STT, turn-taking, barge-in,
  (speech + LLM + TTS)         TTS, telephony, call recording
          │
          │  HTTPS tool calls, x-api-key + x-trace-id
          ▼
  This service (FastAPI)
    ├── state machine          what the agent is allowed to do next
    ├── policy engine          what the answer is
    ├── audit store            what actually happened
    └── structured logs        how to find out why
          │
          ▼
  SQLite (dev) / Postgres (prod)
```

## Division of responsibility

The split is the central design decision. The model is good at conversation and
bad at guarantees, so it owns conversation and nothing else.

| Concern | Owner | Why |
| --- | --- | --- |
| Turn-taking, interruption, speech | Sarvam platform | Solved infrastructure; rebuilding it adds no value here |
| Wording, tone, language switching | Model | This is what it is genuinely good at |
| Which tool to call, and when to give up | Model, guided by prompt | Judgement under ambiguity |
| Whether that call is *allowed* | This service | Guarantees cannot live in a prompt |
| Credit decisions and terms | This service | Must be deterministic, versioned, defensible |
| Attempt limits, consent evidence | This service | Regulatory artefacts, not conversational state |

The rule of thumb: if getting it wrong would be a compliance incident rather than
an awkward sentence, it does not live in the prompt.

## Why the state machine is server side

`app/models.py` defines `SessionState` and an explicit transition table. Every
tool call passes `require_state` before doing work.

A prompt can describe the flow. It cannot guarantee it. Models skip steps under
pressure from a caller, lose track across long calls, and can be talked into
"just this once". Scenarios S10 and S11 in the eval suite exercise exactly this:
consent before verification, and continuing after a hand-off. Both are refused
with a 409 and a recoverable `agent_message`, so the call continues gracefully
rather than crashing.

Escalation is deliberately reachable from every live state. If the agent decides
it is out of its depth, the service should not argue with it.

## Why the policy engine is deterministic

`app/services/eligibility.py` is pure functions over a customer record. No model
call, no randomness, no network. Given the same inputs it returns the same
decision, and every decision carries:

- an explicit `reasons` list, not a score
- a `policy_version`, so a decision made last month can be explained with last
  month's rules

`referred` is a first-class outcome. Anything the rules cannot settle cleanly —
notably a declared income that diverges sharply from the record — goes to a human
rather than being forced into an approval or a decline.

## Request lifecycle

1. `TracingMiddleware` adopts the platform's `x-trace-id` or mints one, and
   starts the latency timer.
2. `require_api_key` checks the shared secret in constant time.
3. Pydantic validates the payload. Malformed input returns `outcome: "retry"`
   with a speakable message, because bad input usually means the caller was
   misheard, and the recovery is to ask again rather than to fail the call.
4. The handler loads the session, checks for a replay, guards the state, and
   delegates to a service.
5. `_finalize` writes the audit row, commits, and returns.

## Two ordering bugs, and what they teach

Both were found by the scenario harness rather than the unit tests, because both
only appear when calls arrive in quick succession against a real server.

**Writes were not durable when the agent was told they were.** The session
committed in FastAPI's dependency teardown, which runs after the response reaches
the caller. A fast agent could issue its next tool call against state the
previous call had not committed. Fixed by committing inside `_finalize` before
returning, with the `uq_tool_idempotency` constraint as a backstop for genuinely
concurrent retries. The savepoint in `record_tool_call` keeps a collision from
poisoning the surrounding transaction.

**The replay check ran after the state guard.** A hand-off that succeeded and
then timed out would be retried against a session it had already moved to a
terminal state, and rejected as out-of-order — breaking the exact case
idempotency exists to handle. The replay check now precedes the guard in all four
handlers.

The general lesson is in the ordering of a retry-safe handler: *identify the
request, check whether it already happened, then check whether it is allowed.*

## Observability

One structured JSON line per event, with `trace_id` and `session_id` on every
line. PAN is masked by `mask_pan` at the point of logging, not at ingestion, so
an unredacted value never reaches disk.

`GET /v1/sessions/{id}` returns the redacted, replayable record of a call: the
full tool sequence with per-call latency, the eligibility decision with reasons,
consent records, and any escalation. It serves support and evaluation equally.

## Data model

- **Reference data** (`customers`) stands in for core banking or a CRM.
- **Audit data** (`onboarding_sessions`, `tool_calls`, `eligibility_assessments`,
  `consent_records`, `escalations`) is append-only by convention. Consent and
  escalation rows are never updated or deleted.

## Deployment notes

SQLite is the default so the service runs with no infrastructure. It is genuinely
adequate for a single-instance demo, and inadequate for production: concurrent
writers serialise, and the WAL file does not survive a container restart on
ephemeral storage.

Moving to Postgres is a `DATABASE_URL` change. The data access layer is plain
SQLAlchemy 2.0 with no SQLite-specific SQL; the only conditional code is the
pragma listener in `app/db.py`, which no-ops on other backends. Before running
multi-instance, add Alembic migrations in place of `create_all`.
