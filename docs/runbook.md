# Runbook

## Health

| Endpoint | Meaning | Use |
| --- | --- | --- |
| `GET /healthz` | Process is alive. Does not touch the database | Liveness probe |
| `GET /readyz` | Database is reachable | Readiness probe and load balancer |

Point the load balancer at `/readyz`, not `/healthz`. A process that is up but
cannot reach its database should stop receiving calls.

## Investigating a specific call

Every log line carries `trace_id` and `session_id`. The platform's call id is
forwarded as `x-trace-id`, so one identifier spans the telephony leg and every
tool call.

```bash
# All events for one call
grep '"trace_id": "<call-id>"' /var/log/app.log | jq .

# Reconstruct what happened, redacted
curl -s -H "x-api-key: $API_KEY" \
  https://<host>/v1/sessions/<session_id> | jq .
```

The audit response gives the tool sequence in order, per-call latency, passcode
challenges (issued, attempted, consumed — never the code itself), the eligibility
decision with its reasons and policy version, consent records, any lead, and any
escalation ticket.

## Common alerts

**Passcode delivery failing.**
Filter for `sms_dispatch_failed` or `email_dispatch_failed`. A 4xx status means
configuration — a bad key, an unverified recipient, an unapproved template — and
will not resolve on its own. Timeouts mean the provider is slow or down; the
service retries once and then routes callers to a human, so a sustained outage
shows up as a rise in `technical_failure` escalations rather than as silence.

**Passcode rate limits tripping.**
`issue_outcome: rate_limited` in the `tool_call` digest means a customer hit the
per-customer issuance cap. Expected during testing against the same seed record;
in production a cluster of these against one customer is worth investigating as
either a retry storm or an attempt to use the agent to flood someone's phone.

**Lead capture rate.**
A rise in `not_registered` outcomes is commercially interesting rather than
alarming — it means callers without accounts are reaching the agent. A sudden
spike from one source is different, and should be read as PAN enumeration until
shown otherwise.

**Verification failure rate climbing.**
Filter for `"message": "tool_call"` with `tool: verify_identity` and
`outcome: retry` or `blocked`. Check the `failure_reason` distribution in
`request_digest`. Mostly `name_mismatch` suggests a speech-to-text regression
rather than fraud; mostly `pan_not_found` suggests stale reference data. A sudden
spike in either from a single source is worth treating as enumeration until
proven otherwise.

**Escalation rate climbing.**
Group escalations by `reason_code`. `technical_failure` points at this service or
the platform; `low_confidence_transcription` points at audio quality or a model
change; `customer_disputes_decision` points at a policy change landing badly.

**Latency alarm.**
Tool latency is reported per request as `latency_ms`. This service should sit in
single-digit milliseconds; the eval suite records p50 around 5ms and p95 around
11ms locally. If tool latency is fine but calls feel slow, the problem is in
speech-to-text, inference, or text-to-speech, and belongs in the platform's call
analytics rather than here.

**`unhandled_exception` in the logs.**
Always a bug. The caller received a generic message and the agent should have
escalated. Pull the `trace_id`, reproduce against the audit record, and add a
scenario to `evals/scenarios.py` before fixing.

## Changing passcode policy

Every limit is in `Settings`: length, lifetime, verification attempts, resends,
and the per-customer issuance window. Change them by environment variable, not
in code, and remember the caller experiences the sum of them — a short lifetime
with a low resend cap makes a slow caller unservable.

`OTP_DEMO_MODE` must be false anywhere real. It returns the passcode in the tool
response so a demo can run without a provider; it is not a debugging aid.

## Changing the delivery channel

`OTP_DELIVERY_CHANNEL` selects `sms` or `email`. Switching to SMS in India needs
DLT registration completed first — principal entity, sender id, and an approved
template whose variables match `var_code` and `var_ttl`. Switching to a real
email provider needs `EMAIL_PROVIDER=resend` and a key; note that Resend's
sandbox sender only delivers to the account holder's own address, so seeded
records with placeholder addresses will fail with `delivery_failed`.

Both misconfigurations fail loudly at startup rather than silently on the first
customer call.

## Changing credit policy

1. Edit the rules in `app/services/eligibility.py`.
2. Bump `ELIGIBILITY_POLICY_VERSION`. Do not skip this — existing assessments are
   pinned to the version that produced them, and that is what makes an old
   decision explainable.
3. Add or update the affected tests in `tests/test_eligibility.py`.
4. Run `make test` and then the scenario suite.

## Rotating the API key

1. Set the new value in the deployment environment.
2. Update the credential in the Sarvam agent's live-action headers.
3. Restart the service. There is no dual-key window, so do this in a maintenance
   slot or add one first.

## Rolling back

The service is stateless apart from the database. Redeploy the previous image.
Schema changes are additive so far, so a rollback does not require a data
migration — this stops being true as soon as a column is dropped or renamed, at
which point Alembic and a two-phase deploy become mandatory.

## Schema changes

Alembic owns the schema. `alembic upgrade head` runs on every deploy and is the
only supported way to change a deployed database — `init_db` creates tables from
the models but can never alter an existing one, which is why it is now confined
to tests and scratch databases.

After changing a model:

```
make migration m="what changed"     # generates a revision
```

**Read the generated file before committing it.** Autogenerate is a good first
draft and a poor oracle: it does not see renames (it emits a drop plus an add,
which loses data), and it guesses at server defaults. CI fails the build if a
model change is committed without a matching migration.

To apply:

```
make migrate
```

Rolling back one revision is `alembic downgrade -1`. Verify the downgrade path
locally before relying on it in an incident — a migration that only goes forward
is a migration you cannot retreat from.

`seed()` still backfills unset fields on existing rows. That is now belt and
braces rather than the mechanism.

## Postgres

Set `DATABASE_URL` to a `postgresql+psycopg://` URL. Neon's free tier works and
requires `?sslmode=require`.

Two behaviours to expect from serverless Postgres. It scales to zero, so the
first query after an idle period pays a cold start of a few hundred
milliseconds — noticeable against a 70ms baseline, invisible against a voice
turn. And it recycles connections aggressively, which is why the engine uses a
small pool with `pool_recycle` and `pool_pre_ping`; without those, the first
call after idling fails on a connection the server already dropped.

Anything that must outlive a restart needs Postgres rather than SQLite. The
application link flow is the clearest case: the link is valid for 48 hours, so
ephemeral storage can kill a good link between sending the email and the
applicant opening it.

## Before running more than one instance

- Add per-key rate limiting
- Confirm idempotency behaviour under real concurrency — the constraint is in
  place, but it has only been exercised against SQLite
