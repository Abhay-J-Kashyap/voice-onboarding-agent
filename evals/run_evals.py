"""Run the scenario suite against a live service and emit a scored report.

    python -m evals.run_evals --base-url http://localhost:8000

The harness scores two layers, and the second exists because of a bug the first
one missed.

**Live layer.** Each scenario drives a scripted tool sequence and checks the
response: right outcome, right state, right data, and a speakable message.

**Audit layer.** After the calls finish, the harness fetches
``GET /v1/sessions/{id}`` and cross-checks the durable record against what the
live responses claimed. This catches the class of bug where a value is computed
correctly, spoken correctly, and then never written down — which is exactly what
happened with ``monthly_instalment``: the eligibility engine returned the right
EMI, ``check_eligibility`` returned it to the agent, and the audit row stored
nothing. Everything green, and the record permanently wrong.

The audit checks are derived from the scenario definition rather than declared
per-scenario, so a new scenario is cross-checked automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from evals.scenarios import SCENARIOS, Scenario

#: Fields the live eligibility response and the audit record must agree on.
ELIGIBILITY_FIELDS = (
    "decision",
    "approved_amount",
    "interest_rate",
    "monthly_instalment",
    "policy_version",
)


@dataclass
class StepResult:
    tool: str
    passed: bool
    latency_ms: float
    detail: str = ""


@dataclass
class AuditCheck:
    """One assertion comparing the durable record to the live responses."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario: Scenario
    steps: list[StepResult]
    audit_checks: list[AuditCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps) and all(
            c.passed for c in self.audit_checks
        )

    @property
    def first_failure(self) -> str:
        for step in self.steps:
            if not step.passed:
                return f"{step.tool}: {step.detail}"
        for check in self.audit_checks:
            if not check.passed:
                return f"audit/{check.name}: {check.detail}"
        return ""


def expected_recorded_tools(scenario: Scenario) -> list[str]:
    """Tool names that should appear in the audit trail, in order.

    Two kinds of call are absent by design: those rejected before any work
    happened (a non-200 status, so no audit row), and replays of an
    already-seen ``(tool, idempotency_key)`` pair, which short-circuit to the
    stored response without recording a second row.
    """
    recorded: list[str] = []
    seen_keys: set[tuple[str, str]] = set()
    for step in scenario.steps:
        if step.expect_status != 200:
            continue
        key = step.payload.get("idempotency_key")
        if key is not None:
            pair = (step.tool, key)
            if pair in seen_keys:
                continue
            seen_keys.add(pair)
        recorded.append(step.tool)
    return recorded


def scenario_pans(scenario: Scenario) -> set[str]:
    """Raw PANs the scenario submitted, normalised the way the service would."""
    pans = set()
    for step in scenario.steps:
        raw = step.payload.get("pan")
        if raw:
            pans.add("".join(c for c in raw if c.isalnum()).upper())
    return pans


def check_audit(
    audit: dict[str, Any],
    scenario: Scenario,
    responses: list[tuple[str, int, dict[str, Any]]],
) -> list[AuditCheck]:
    """Cross-check the durable record against what the live calls returned.

    Only responses from calls that actually succeeded are compared. A tool call
    rejected for being out of order still returns a populated ``data`` block —
    the error code — and must not be mistaken for a result the service was ever
    supposed to persist.
    """
    checks: list[AuditCheck] = []
    ok_responses = [(tool, body) for tool, status, body in responses if status == 200]

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append(AuditCheck(name=name, passed=ok, detail=detail))

    # 1. The audit trail contains exactly the calls that did work, in order.
    expected = expected_recorded_tools(scenario)
    actual = [c["tool_name"] for c in audit.get("tool_calls", [])]
    add(
        "tool_sequence",
        actual == expected,
        f"recorded {actual} != expected {expected}",
    )

    # 2. Final state matches the last state the agent was told about.
    live_states = [r.get("session_state") for _, r in ok_responses if r.get("session_state")]
    if live_states:
        add(
            "final_state",
            audit.get("state") == live_states[-1],
            f"audit state {audit.get('state')!r} != last live state {live_states[-1]!r}",
        )

    # 3. Eligibility: every field the agent spoke must be the field stored.
    #    This is the check that would have caught the monthly_instalment bug.
    live_eligibility = next(
        (
            r["data"]
            for tool, r in ok_responses
            if tool == "check_eligibility" and r.get("data")
        ),
        None,
    )
    if live_eligibility is not None:
        stored = audit.get("eligibility")
        if not stored:
            add("eligibility_persisted", False, "no eligibility row in audit record")
        else:
            mismatches = [
                f"{field_name}: audit {stored.get(field_name)!r} != live "
                f"{live_eligibility.get(field_name)!r}"
                for field_name in ELIGIBILITY_FIELDS
                if stored.get(field_name) != live_eligibility.get(field_name)
            ]
            add("eligibility_matches_live", not mismatches, "; ".join(mismatches))

            # A stored EMI of zero against a non-zero approved amount is the
            # specific shape of the original bug, so name it explicitly.
            if stored.get("approved_amount"):
                add(
                    "emi_persisted_non_zero",
                    bool(stored.get("monthly_instalment")),
                    f"approved {stored.get('approved_amount')} but EMI "
                    f"{stored.get('monthly_instalment')!r}",
                )

    # 4. Consent evidence exists for every consent the agent recorded.
    live_consents = [
        r for tool, r in ok_responses if tool == "record_consent" and r.get("data")
    ]
    if live_consents:
        stored_flags = [c["granted"] for c in audit.get("consents", [])]
        live_flags = [r["data"].get("granted") for r in live_consents]
        add(
            "consent_persisted",
            stored_flags == live_flags,
            f"audit {stored_flags} != live {live_flags}",
        )

    # 5. The escalation ticket the caller was given is the ticket on file.
    live_escalation = next(
        (
            r["data"]
            for tool, r in ok_responses
            if tool == "escalate" and r.get("data", {}).get("ticket_ref")
        ),
        None,
    )
    if live_escalation is not None:
        stored = audit.get("escalation")
        if not stored:
            add("escalation_persisted", False, "no escalation row in audit record")
        else:
            add(
                "escalation_matches_live",
                stored.get("ticket_ref") == live_escalation.get("ticket_ref")
                and stored.get("queue") == live_escalation.get("queue"),
                f"audit {stored.get('ticket_ref')}/{stored.get('queue')} != live "
                f"{live_escalation.get('ticket_ref')}/{live_escalation.get('queue')}",
            )

    # 6. No unredacted PAN anywhere in the record. Redaction is a property of
    #    the stored artefact, not just of the log line that produced it.
    blob = json.dumps(audit)
    leaked = sorted(pan for pan in scenario_pans(scenario) if pan in blob)
    add("no_pan_leak", not leaked, f"unmasked PAN in audit payload: {leaked}")

    return checks


def run_scenario(client: httpx.Client, scenario: Scenario) -> ScenarioResult:
    """Open a fresh session, drive the tool sequence, then audit the result."""
    start = client.post("/v1/sessions", json={"external_call_id": f"eval-{scenario.id}"})
    start.raise_for_status()
    session_id = start.json()["session_id"]

    results: list[StepResult] = []
    responses: list[tuple[str, int, dict[str, Any]]] = []

    for step in scenario.steps:
        payload = {"session_id": session_id, **step.payload}
        began = time.perf_counter()
        response = client.post(f"/v1/tools/{step.tool}", json=payload)
        latency_ms = round((time.perf_counter() - began) * 1000, 2)

        failures: list[str] = []
        if response.status_code != step.expect_status:
            failures.append(
                f"status {response.status_code} != expected {step.expect_status}"
            )

        body = response.json()
        responses.append((step.tool, response.status_code, body))

        outcome = body.get("outcome")
        if outcome != step.expect_outcome:
            failures.append(f"outcome '{outcome}' != expected '{step.expect_outcome}'")

        if step.expect_state and body.get("session_state") != step.expect_state:
            failures.append(
                f"state '{body.get('session_state')}' != expected '{step.expect_state}'"
            )

        data = body.get("data") or {}
        for key, expected in step.expect_data.items():
            if data.get(key) != expected:
                failures.append(f"data.{key}={data.get(key)!r} != {expected!r}")

        # Every response must carry something the agent can actually say.
        if not body.get("agent_message"):
            failures.append("missing agent_message")

        results.append(
            StepResult(
                tool=step.tool,
                passed=not failures,
                latency_ms=latency_ms,
                detail="; ".join(failures),
            )
        )

    audit_response = client.get(f"/v1/sessions/{session_id}")
    if audit_response.status_code != 200:
        audit_checks = [
            AuditCheck(
                "audit_reachable",
                False,
                f"GET /v1/sessions returned {audit_response.status_code}",
            )
        ]
    else:
        audit_checks = check_audit(audit_response.json(), scenario, responses)

    return ScenarioResult(scenario=scenario, steps=results, audit_checks=audit_checks)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(pct / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def render_report(results: list[ScenarioResult]) -> str:
    latencies = [s.latency_ms for r in results for s in r.steps]
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    all_checks = [c for r in results for c in r.audit_checks]
    checks_passed = sum(1 for c in all_checks if c.passed)

    lines = [
        "# Evaluation report",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "## Summary",
        "",
        f"- Scenarios passed: **{passed}/{total}** ({passed / total:.0%})",
        f"- Tool calls executed: {len(latencies)}",
        f"- Audit cross-checks passed: **{checks_passed}/{len(all_checks)}**",
        f"- Latency p50: {percentile(latencies, 50):.1f} ms",
        f"- Latency p95: {percentile(latencies, 95):.1f} ms",
    ]
    if latencies:
        lines.append(f"- Latency max: {max(latencies):.1f} ms")

    lines += [
        "",
        "## Scenarios",
        "",
        "| ID | Persona | Protects against | Result | First divergence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        status = "pass" if result.passed else "FAIL"
        lines.append(
            f"| {result.scenario.id} | {result.scenario.persona} "
            f"| {result.scenario.intent} | {status} | {result.first_failure or '—'} |"
        )

    lines += [
        "",
        "## Audit cross-checks",
        "",
        "Each scenario's durable record is compared against what the live tool",
        "calls returned. A green live run with a red column here means a value",
        "was computed and spoken correctly but stored wrongly.",
        "",
        "| Check | Passed | Verifies |",
        "| --- | --- | --- |",
    ]
    descriptions = {
        "tool_sequence": "Audit trail holds exactly the calls that did work, in order",
        "final_state": "Stored state matches the last state the agent was told",
        "eligibility_persisted": "An eligibility decision was written at all",
        "eligibility_matches_live": "Every spoken figure equals the stored figure",
        "emi_persisted_non_zero": "A non-zero approval stores a non-zero instalment",
        "consent_persisted": "Consent evidence matches what was recorded live",
        "escalation_persisted": "An escalation ticket was written at all",
        "escalation_matches_live": "The caller's ticket reference is the one on file",
        "no_pan_leak": "No unredacted PAN appears in the stored record",
        "audit_reachable": "The audit endpoint responded",
    }
    by_name: dict[str, list[AuditCheck]] = {}
    for check in all_checks:
        by_name.setdefault(check.name, []).append(check)
    for name, checks in sorted(by_name.items()):
        ok = sum(1 for c in checks if c.passed)
        lines.append(
            f"| `{name}` | {ok}/{len(checks)} | {descriptions.get(name, '')} |"
        )

    failed_checks = [
        (r.scenario.id, c) for r in results for c in r.audit_checks if not c.passed
    ]
    if failed_checks:
        lines += ["", "### Failed cross-checks", ""]
        for scenario_id, check in failed_checks:
            lines.append(f"- **{scenario_id}** `{check.name}`: {check.detail}")

    lines += [
        "",
        "## Latency by tool",
        "",
        "| Tool | Calls | p50 (ms) | p95 (ms) |",
        "| --- | --- | --- | --- |",
    ]
    by_tool: dict[str, list[float]] = {}
    for result in results:
        for step in result.steps:
            by_tool.setdefault(step.tool, []).append(step.latency_ms)
    for tool, values in sorted(by_tool.items()):
        lines.append(
            f"| {tool} | {len(values)} | {percentile(values, 50):.1f} "
            f"| {percentile(values, 95):.1f} |"
        )

    lines += [
        "",
        "> Latencies cover the tool service only. End-to-end spoken latency is",
        "> dominated by speech-to-text, model inference and text-to-speech, and is",
        "> measured separately from the voice platform's call analytics.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="local-dev-key")
    parser.add_argument("--output", default="evals/report.md")
    parser.add_argument(
        "--json", dest="json_output", default=None, help="Optional JSON results path"
    )
    args = parser.parse_args()

    with httpx.Client(
        base_url=args.base_url,
        headers={"x-api-key": args.api_key},
        timeout=10.0,
    ) as client:
        try:
            client.get("/readyz").raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Service not reachable at {args.base_url}: {exc}", file=sys.stderr)
            return 2

        results = [run_scenario(client, scenario) for scenario in SCENARIOS]

    report = render_report(results)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(report)

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {
                        "id": r.scenario.id,
                        "passed": r.passed,
                        "steps": [
                            {
                                "tool": s.tool,
                                "passed": s.passed,
                                "latency_ms": s.latency_ms,
                                "detail": s.detail,
                            }
                            for s in r.steps
                        ],
                        "audit_checks": [
                            {"name": c.name, "passed": c.passed, "detail": c.detail}
                            for c in r.audit_checks
                        ],
                    }
                    for r in results
                ],
                handle,
                indent=2,
            )

    failed = [r for r in results if not r.passed]
    print(report)
    if failed:
        print(f"\n{len(failed)} scenario(s) failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())