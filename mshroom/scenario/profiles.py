"""Comparison profiles: how one returned message is judged against its step.

A *profile* is the answer to "what counts as correct for this step?". Two
exist today, selected by a step's ``mode``:

* ``passthrough`` (the default) -- the engine must hand back exactly what
  was sent, except for fields the engine legitimately rewrites (the
  ``ignore`` list, :data:`DEFAULT_PASSTHROUGH_IGNORE` unless the step sets
  its own). Any other difference is one ``PASSTHROUGH_DIFF`` failure that
  lists every differing field.
* ``assert`` -- no whole-message comparison; only the step's
  ``[[step.expect]]`` entries are checked. This is the shape later
  transformation exercises take ("PID-5.1 must now be upper case").

Expectations are honoured in *both* modes, so a passthrough step may also
pin down a field explicitly.

Invariants:

* A profile only judges messages that are already correlated to the step
  (the runner has proved whose they are); it never decides correlation.
* Profiles are pure: they read parsed messages and return failures.
* Adding a profile means adding an entry to :data:`PROFILES` and to the
  loader's accepted modes (which reads that same table), nothing else.
"""

from __future__ import annotations

from typing import Callable

from hl7kit.parser import Message

from .assertions import check_expectation, diff_messages
from .model import (
    FAIL_ASSERTION_FAILED,
    FAIL_PASSTHROUGH_DIFF,
    MODE_ASSERT,
    MODE_PASSTHROUGH,
    Step,
    StepFailure,
)

#: Fields an engine normally rewrites on the way through: sending
#: application (MSH-3) and facility (MSH-4), receiving application (MSH-5)
#: and facility (MSH-6), and the message timestamp (MSH-7). Comparing them
#: would make every honest passthrough "fail".
DEFAULT_PASSTHROUGH_IGNORE = ("MSH-3", "MSH-4", "MSH-5", "MSH-6", "MSH-7")


def _expectation_failures(step: Step, step_no: int, returned: Message, redact: bool) -> list[StepFailure]:
    """One ``ASSERTION_FAILED`` per expectation that does not hold."""
    failures = []
    for expectation in step.expect:
        problem = check_expectation(returned, expectation, redact)
        if problem is not None:
            failures.append(
                StepFailure(
                    code=FAIL_ASSERTION_FAILED,
                    step=step_no,
                    step_name=step.name,
                    message=f"{problem.field}: expected {problem.expected}, got {problem.actual!r}",
                    field=problem.field,
                    expected=problem.expected,
                    actual=problem.actual,
                )
            )
    return failures


def _passthrough(step: Step, step_no: int, sent: Message, returned: Message, redact: bool) -> list[StepFailure]:
    """Whole-message comparison, then any explicit expectations."""
    failures = []
    diffs = diff_messages(sent, returned, step.ignore, redact)
    if diffs:
        names = ", ".join(d.field for d in diffs)
        failures.append(
            StepFailure(
                code=FAIL_PASSTHROUGH_DIFF,
                step=step_no,
                step_name=step.name,
                message=f"returned message differs from sent in {len(diffs)} field(s): {names}",
                field=diffs[0].field,
                diffs=diffs,
            )
        )
    return failures + _expectation_failures(step, step_no, returned, redact)


def _assert_only(step: Step, step_no: int, sent: Message, returned: Message, redact: bool) -> list[StepFailure]:
    """Expectations only; *sent* is unused (kept so every profile has the
    same signature)."""
    return _expectation_failures(step, step_no, returned, redact)


#: mode name -> profile function ``(step, step_no, sent, returned, redact)``.
PROFILES: dict[str, Callable[[Step, int, Message, Message, bool], list[StepFailure]]] = {
    MODE_PASSTHROUGH: _passthrough,
    MODE_ASSERT: _assert_only,
}


def evaluate(step: Step, step_no: int, sent: Message, returned: Message, redact: bool) -> list[StepFailure]:
    """Judge one correlated *returned* message under the step's mode.

    *redact* is true when the returned message is not provably synthetic;
    failures then carry :data:`~mshroom.scenario.assertions.REDACTED` in
    place of its values.
    """
    return PROFILES[step.mode](step, step_no, sent, returned, redact)
