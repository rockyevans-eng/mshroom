"""Turning a :class:`RunResult` into the human report and the JSON file.

Two outputs from the same data:

* :func:`format_report` -- a concise console report: one line per step
  (PASS/FAIL), each failure indented under its step with its code, and any
  field diffs indented under that.
* :func:`result_to_dict` / :func:`write_json` -- the complete result, for
  machines and for keeping.

Invariant (the PHI guard): a returned message's **body** goes into the JSON
only if the message is synthetic. Otherwise the file holds its SHA-256 hash
and ``"synthetic": false`` -- enough to tell two messages apart, not enough
to recover a patient's data. The sent messages come from the checked-in
templates with fictional data, so they are always stored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import EXIT_ERROR, EXIT_FAIL, FieldDiff, ReturnedMessage, RunResult, StepFailure, StepResult

#: Result file format marker; bump the trailing number when the shape changes.
SCHEMA = "mshroom-scenario-result/0"


# ---------------------------------------------------------------------------
# Human report
# ---------------------------------------------------------------------------


def _step_line(step: StepResult) -> str:
    status = "PASS" if step.passed else "FAIL"
    label = f" [{step.label}]" if step.label else ""
    return f"  {status}  {step.number}. {step.name}{label}"


def _failure_lines(failure: StepFailure) -> list[str]:
    lines = [f"        {failure.code}: {failure.message}"]
    for diff in failure.diffs:
        lines.append(f"            {diff.field}: sent {diff.expected!r}, returned {diff.actual!r}")
    return lines


def format_report(result: RunResult) -> str:
    """The console report for *result* (no trailing newline)."""
    lines = [
        f"Scenario: {result.scenario_name}  (run {result.run_id})",
        f"Target: {result.target}   Return listener: {result.listen}   "
        f"Keep-open: {'yes' if result.keep_open else 'no'}",
    ]
    for step in result.steps:
        lines.append(_step_line(step))
        for failure in step.failures:
            lines.extend(_failure_lines(failure))
    if result.non_synthetic_data_seen:
        lines.append(
            "WARNING: a returned message did not carry a synthetic patient identifier (PID-3 must start "
            "with MSHROOM-TEST-). Its content is not stored or shown; only a hash is kept."
        )
    if result.error is not None:
        lines.append(f"ERROR: {result.error}")
    passed = sum(1 for s in result.steps if s.passed)
    failed = len(result.steps) - passed
    verdict = {EXIT_ERROR: "ERROR", EXIT_FAIL: "FAIL"}.get(result.exit_code, "PASS")
    lines.append(f"Result: {verdict} -- {passed} step(s) passed, {failed} failed (exit {result.exit_code})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _diff_dict(diff: FieldDiff) -> dict[str, str]:
    return {"field": diff.field, "expected": diff.expected, "actual": diff.actual}


def _failure_dict(failure: StepFailure) -> dict[str, Any]:
    return {
        "code": failure.code,
        "step": failure.step,
        "step_name": failure.step_name,
        "message": failure.message,
        "field": failure.field,
        "expected": failure.expected,
        "actual": failure.actual,
        "diffs": [_diff_dict(d) for d in failure.diffs],
    }


def _returned_dict(message: ReturnedMessage) -> dict[str, Any]:
    """One returned message. The body is included only when synthetic."""
    return {
        "role": message.role,
        "received_at": message.received_at,
        "synthetic": message.synthetic,
        "sha256": message.sha256,
        "body": message.text if message.synthetic else None,
    }


def _step_dict(step: StepResult) -> dict[str, Any]:
    ack = step.ack
    return {
        "step": step.number,
        "name": step.name,
        "label": step.label,
        "mode": step.mode,
        "status": "PASS" if step.passed else "FAIL",
        "token": step.token,
        "sent": step.sent,
        "ack": None if ack is None else {"ok": ack.ok, "code": ack.code, "text": ack.text, "error": ack.error},
        "returns_expected": step.returns_expected,
        "returns_correlated": step.correlated_count,
        "returned": [_returned_dict(m) for m in step.returned],
        "non_hl7_events": dict(step.non_hl7_events),
        "failures": [_failure_dict(f) for f in step.failures],
    }


def result_to_dict(result: RunResult) -> dict[str, Any]:
    """The complete result as plain JSON-able data (see the module docstring
    for what is and is not stored)."""
    return {
        "schema": SCHEMA,
        "run_id": result.run_id,
        "scenario": {"name": result.scenario_name, "description": result.description},
        "target": result.target,
        "listen": result.listen,
        "keep_open": result.keep_open,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "exit_code": result.exit_code,
        "error": result.error,
        "non_synthetic_data_seen": result.non_synthetic_data_seen,
        "steps": [_step_dict(s) for s in result.steps],
    }


def write_json(result: RunResult, path: Path) -> Path:
    """Write the result to *path* (creating parent directories); return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result_to_dict(result), indent=2) + "\n", encoding="utf-8")
    return path
