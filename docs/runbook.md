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

The audit response gives the tool sequence in order, per-call latency, the
eligibility decision with its reasons and policy version, consent records, and
any escalation ticket.

## Common alerts

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

## Before running more than one instance

- Replace SQLite with Postgres (`DATABASE_URL`)
- Replace `create_all` with Alembic migrations
- Add per-key rate limiting
- Confirm idempotency behaviour under real concurrency — the constraint is in
  place, but it has only been exercised against SQLite
