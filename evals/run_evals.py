"""Run the scenario suite against a live service and emit a scored report.

    python -m evals.run_evals --base-url http://localhost:8000

The point of this harness is that a change to the policy engine, the state
machine, or the prompt can be regression-tested in seconds instead of by
placing test calls. It reports per-scenario pass/fail with the first divergence,
plus latency percentiles across every tool call it made.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from evals.scenarios import SCENARIOS, Scenario


@dataclass
class StepResult:
    tool: str
    passed: bool
    latency_ms: float
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario: Scenario
    steps: list[StepResult]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)

    @property
    def first_failure(self) -> str:
        for step in self.steps:
            if not step.passed:
                return f"{step.tool}: {step.detail}"
        return ""


def run_scenario(client: httpx.Client, scenario: Scenario) -> ScenarioResult:
    """Open a fresh session and drive the scripted tool sequence through it."""
    start = client.post("/v1/sessions", json={"external_call_id": f"eval-{scenario.id}"})
    start.raise_for_status()
    session_id = start.json()["session_id"]

    results: list[StepResult] = []
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

    return ScenarioResult(scenario=scenario, steps=results)


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

    lines = [
        "# Evaluation report",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "## Summary",
        "",
        f"- Scenarios passed: **{passed}/{total}** ({passed / total:.0%})",
        f"- Tool calls executed: {len(latencies)}",
        f"- Latency p50: {percentile(latencies, 50):.1f} ms",
        f"- Latency p95: {percentile(latencies, 95):.1f} ms",
        f"- Latency max: {max(latencies):.1f} ms" if latencies else "",
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
    return "\n".join(line for line in lines if line is not None)


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
