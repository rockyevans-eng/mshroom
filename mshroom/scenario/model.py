"""Plain data types for the scenario runner (EXPERIMENTAL, format v0).

Two families of dataclasses live here and nothing else -- no I/O, no
sockets, no parsing:

* **Definition types** (:class:`Scenario`, :class:`Step`,
  :class:`Expectation`) -- what a scenario file *says*. They are built by
  :mod:`mshroom.scenario.loader` and are immutable once built.
* **Result types** (:class:`RunResult`, :class:`StepResult`,
  :class:`StepFailure`, :class:`FieldDiff`, :class:`ReturnedMessage`) --
  what a run *found*. They are filled in by :mod:`mshroom.scenario.runner`
  and turned into text / JSON by :mod:`mshroom.scenario.report`.

Invariants other modules rely on:

* Every failure has a *code* from the ``FAIL_*`` constants below and is
  attributed to exactly one step (``StepFailure.step``). Nothing is ever
  reported as a failure without a code, and no code is reported without a
  step, so a person reading the report can always answer "which step, and
  what kind of problem".
* :func:`make_token` and :data:`TOKEN_RE` are a pair: every token the
  runner puts on the wire matches the regex, and the regex matches nothing
  else in practice. That is what lets the runner recognise "a message from
  another step or another run" without keeping a list of old tokens.
* A run's exit code is derived (:attr:`RunResult.exit_code`), never stored,
  so it cannot disagree with the step results.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from dataclasses import field as dc_field  # aliased: several dataclasses below have a *data* field named ``field``
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Every patient identifier in a scenario (and every message MSHroom is
#: willing to store in a result file) must start with this prefix. It is the
#: single marker that separates synthetic test data from possible real data.
SYNTHETIC_PREFIX = "MSHROOM-TEST-"

#: Step comparison modes (see :mod:`mshroom.scenario.profiles`).
MODE_PASSTHROUGH = "passthrough"
MODE_ASSERT = "assert"

#: Failure codes (one per kind of problem; see the runner for when each fires).
FAIL_NO_ACK = "NO_ACK"
FAIL_ACK_NOT_AA = "ACK_NOT_AA"
FAIL_MISSING_RETURN = "MISSING_RETURN"
FAIL_EXTRA_RETURN = "EXTRA_RETURN"
FAIL_UNCORRELATED = "UNCORRELATED"
FAIL_LATE_OR_DUPLICATE = "LATE_OR_DUPLICATE"
FAIL_ASSERTION_FAILED = "ASSERTION_FAILED"
FAIL_PASSTHROUGH_DIFF = "PASSTHROUGH_DIFF"

#: Exit codes of ``python -m mshroom run``.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

#: Run identifiers look like ``MSHR20260919101500a1b2c3``: a fixed prefix, a
#: UTC timestamp, and six random hex digits (so two runs in the same second
#: still differ). Fixed length is deliberate -- see :data:`TOKEN_RE`.
_RUN_ID_PATTERN = r"MSHR\d{14}[0-9a-f]{6}"

#: Matches a run token anywhere inside a string. Group ``run`` is the run id,
#: group ``step`` the 1-based step number. Searching (not full-matching) lets
#: the runner still correlate an engine that decorates the control ID (for
#: example by appending a suffix); the passthrough diff then reports the
#: decoration as a change to that field.
TOKEN_RE = re.compile(rf"(?P<run>{_RUN_ID_PATTERN})-(?P<step>\d+)")


def make_run_id(now: Optional[datetime] = None) -> str:
    """A fresh run id, e.g. ``MSHR20260919101500a1b2c3``."""
    moment = now or datetime.now(timezone.utc)
    return f"MSHR{moment.strftime('%Y%m%d%H%M%S')}{secrets.token_hex(3)}"


def make_token(run_id: str, step_no: int) -> str:
    """The correlation token for one step of one run: ``<run_id>-<step_no>``.

    Unique per (run, step); the template places it in the correlation field
    (usually MSH-10) so the returned message can be matched to its step.
    """
    return f"{run_id}-{step_no}"


# ---------------------------------------------------------------------------
# Definition types (what a scenario file says)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    """One check on a returned message: exactly one of the three tests is set.

    ``equals`` -- the field's text must be exactly this string.
    ``regex``  -- the field's text must match this pattern *completely*
    (``re.fullmatch``; write ``.*`` yourself if you mean "contains").
    ``present`` -- ``True``: the field must be non-empty; ``False``: it must
    be absent or empty.
    """

    field: str
    equals: Optional[str] = None
    regex: Optional[str] = None
    present: Optional[bool] = None


@dataclass(frozen=True)
class Step:
    """One message to send and what must come back for it.

    ``template_text`` is the raw template (still containing ``{{...}}``
    placeholders); the runner renders it per run. ``label`` is free text for
    humans (registered / admitted / ...) and never influences behaviour.
    """

    name: str
    template_file: str
    template_text: str
    label: str = ""
    returns: int = 1
    timeout: float = 10.0
    mode: str = MODE_PASSTHROUGH
    ignore: tuple[str, ...] = ()
    expect: tuple[Expectation, ...] = ()


@dataclass(frozen=True)
class Scenario:
    """A whole scenario: identity, fictional patient, ordered steps."""

    name: str
    description: str
    synthetic: bool
    correlate_on: str
    patient: dict[str, str]
    steps: tuple[Step, ...]
    source_name: str = ""  # scenario file's base name (never a full path)


# ---------------------------------------------------------------------------
# Result types (what a run found)
# ---------------------------------------------------------------------------


@dataclass
class FieldDiff:
    """One field that differs between the sent and the returned message."""

    field: str  # e.g. "PID-5" or "OBX[2]-5"
    expected: str
    actual: str


@dataclass
class StepFailure:
    """One named problem, attributed to one step."""

    code: str
    step: int  # 1-based step number
    step_name: str
    message: str
    field: Optional[str] = None
    expected: Optional[str] = None
    actual: Optional[str] = None
    diffs: list[FieldDiff] = dc_field(default_factory=list)


@dataclass
class ReturnedMessage:
    """One HL7 message that arrived on the return listener during a step.

    ``text`` is kept in memory for comparison but is only ever written to a
    result file when ``synthetic`` is true (see the report module).
    ``role`` says how the runner used it: ``correlated`` (counted as this
    step's return), ``late`` (belongs to another step/run) or
    ``uncorrelated`` (carries no run token at all).
    """

    text: str
    sha256: str
    synthetic: bool
    role: str
    received_at: str


@dataclass
class AckRecord:
    """The immediate ACK for a sent message (or why there wasn't one)."""

    ok: bool
    code: Optional[str] = None
    text: str = ""
    error: str = ""


@dataclass
class StepResult:
    """Everything observed for one step."""

    number: int
    name: str
    label: str
    mode: str
    token: str
    sent: str
    returns_expected: int
    ack: Optional[AckRecord] = None
    returned: list[ReturnedMessage] = dc_field(default_factory=list)
    non_hl7_events: dict[str, int] = dc_field(default_factory=dict)
    failures: list[StepFailure] = dc_field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def correlated_count(self) -> int:
        return sum(1 for m in self.returned if m.role == "correlated")


@dataclass
class RunResult:
    """The outcome of one scenario run.

    ``error`` is set for infrastructure/usage problems (return port busy,
    target unreachable); it forces exit code 2 regardless of step results.
    """

    run_id: str
    scenario_name: str
    description: str
    target: str
    listen: str
    keep_open: bool
    started_at: str
    finished_at: str = ""
    steps: list[StepResult] = dc_field(default_factory=list)
    error: Optional[str] = None
    non_synthetic_data_seen: bool = False

    @property
    def exit_code(self) -> int:
        if self.error is not None:
            return EXIT_ERROR
        return EXIT_PASS if all(s.passed for s in self.steps) else EXIT_FAIL


class ScenarioError(Exception):
    """A problem with the scenario/template/arguments themselves (exit 2).

    Raised by the loader with a plain-English message that names the file
    (and, where useful, the step) at fault.
    """


class InfrastructureError(Exception):
    """A problem talking to the outside world mid-run (exit 2), such as the
    target refusing the connection. Carries the friendly network message."""
