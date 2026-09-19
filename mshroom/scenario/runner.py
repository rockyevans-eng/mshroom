"""The scenario runner: send each step to the engine, judge what comes back.

The picture: MSHroom sends a message to the engine under test (the
*target*); the engine, possibly transforming it, forwards the result to a
port MSHroom is listening on (the *return* port); MSHroom checks it.

Invariants (each one exists to prevent a wrong verdict):

* **Steps run strictly one after another.** Step *n+1* is not sent until
  step *n*'s return window has closed, so a message that arrives during a
  step can only belong to that step or to an earlier one.
* **The runner owns its return listener.** It builds its own
  :class:`~hl7kit.mllp.MllpListener`; it never touches the app's global one.
* **Nothing is matched by guessing.** A returned message counts for a step
  only if its correlation field carries that step's token (see
  :mod:`mshroom.scenario.assertions`). Everything else is reported by name
  (``UNCORRELATED`` / ``LATE_OR_DUPLICATE``) and never counted.
* **Events that arrive between steps are not discarded.** They are handled
  at the start of the next step, where their old token exposes them as
  ``LATE_OR_DUPLICATE`` instead of letting them vanish.
* **Infrastructure trouble stops the run** (target refuses the connection,
  return port busy) and yields exit code 2; a *test* failure never does --
  the run continues with the next step so one report shows every problem.
* **Possible real data is never stored.** A returned message without a
  synthetic PID-3 is kept in memory only long enough to be judged; its
  values are redacted from failures and the result file holds only a hash.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from hl7kit.ack import parse_ack
from hl7kit.mllp import EVENT_HL7, ListenerEvent, MllpConnection, MllpListener, MllpResult
from hl7kit.parser import parse_message

from . import assertions, profiles
from .loader import hl7_timestamp, render_template
from .model import (
    FAIL_ACK_NOT_AA,
    FAIL_EXTRA_RETURN,
    FAIL_LATE_OR_DUPLICATE,
    FAIL_MISSING_RETURN,
    FAIL_NO_ACK,
    FAIL_UNCORRELATED,
    AckRecord,
    InfrastructureError,
    ReturnedMessage,
    RunResult,
    Scenario,
    Step,
    StepFailure,
    StepResult,
    make_run_id,
    make_token,
)

#: Seconds to keep listening after the expected number of returns has
#: arrived, so a duplicate or extra message is caught (``EXTRA_RETURN``)
#: rather than slipping into the next step.
EXTRA_RETURN_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class RunConfig:
    """Where to send, where to listen, and how."""

    target_host: str
    target_port: int
    listen_port: int
    listen_host: str = "0.0.0.0"
    keep_open: bool = False
    grace: float = EXTRA_RETURN_GRACE_SECONDS


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Return collection
# ---------------------------------------------------------------------------


class ReturnCollector:
    """Thread-safe inbox for listener events.

    The listener calls :meth:`on_event` from its connection threads; the
    runner thread calls :meth:`wait_new` to sleep until something arrives
    (or a deadline passes) and take everything queued so far.
    """

    def __init__(self) -> None:
        self._events: list[ListenerEvent] = []
        self._changed = threading.Condition()

    def on_event(self, event: ListenerEvent) -> None:
        with self._changed:
            self._events.append(event)
            self._changed.notify_all()

    def take(self) -> list[ListenerEvent]:
        """Remove and return every queued event (possibly none), no waiting."""
        with self._changed:
            taken, self._events = self._events, []
        return taken

    def wait_new(self, timeout: float) -> list[ListenerEvent]:
        """Wait up to *timeout* seconds for at least one event, then take all."""
        with self._changed:
            if not self._events:
                self._changed.wait(timeout)
            taken, self._events = self._events, []
        return taken


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class _Sender:
    """Sends step messages to the target, one connection per step or one
    persistent connection for the whole run (``keep_open``)."""

    def __init__(self, host: str, port: int, keep_open: bool) -> None:
        self._host, self._port, self._keep_open = host, port, keep_open
        self._connection: Optional[MllpConnection] = None

    def send(self, text: str, timeout: float) -> MllpResult:
        """Send *text*; return the :class:`~hl7kit.mllp.MllpResult` (the ACK).

        Connecting is done as its own step so that "cannot connect" (an
        infrastructure problem, raised as :class:`InfrastructureError`) is
        told apart from "connected but no ACK came" (a test failure).
        """
        if self._connection is None:
            self._connection = MllpConnection(self._host, self._port)
        elif self._connection.peer_has_closed():
            # The engine closed an idle persistent connection. Reconnect here
            # (rather than letting send() do it silently) so a refusal is
            # reported as infrastructure trouble, not as a missing ACK.
            self._connection.close()
        opened = self._connection.connect(timeout)
        if not opened.ok:
            raise InfrastructureError(opened.error)
        result = self._connection.send(text, timeout)
        if not self._keep_open:
            self.close()
        return result

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


# ---------------------------------------------------------------------------
# One step
# ---------------------------------------------------------------------------


def _failure(step_no: int, step: Step, code: str, message: str, **extra) -> StepFailure:
    return StepFailure(code=code, step=step_no, step_name=step.name, message=message, **extra)


def _record_ack(result: StepResult, step: Step, mllp_result: MllpResult) -> None:
    """Store the ACK on *result*; add NO_ACK / ACK_NOT_AA if it is not AA.

    Only AA counts as accepted. AE/AR (or a reply with no MSA) mean the
    engine did not take the message, so nothing will come back for it.
    """
    if not mllp_result.ok:
        result.ack = AckRecord(ok=False, error=mllp_result.error)
        result.failures.append(_failure(result.number, step, FAIL_NO_ACK, f"no ACK received: {mllp_result.error}"))
        return
    info = parse_ack(mllp_result.response)
    result.ack = AckRecord(ok=info.code == "AA", code=info.code, text=info.text)
    if info.code != "AA":
        result.failures.append(
            _failure(
                result.number,
                step,
                FAIL_ACK_NOT_AA,
                f"ACK code was {info.code or 'missing (reply had no MSA)'}, expected AA",
                expected="AA",
                actual=info.code or "",
            )
        )


class _StepCollector:
    """Classifies the events that arrive during one step's return window."""

    def __init__(self, scenario: Scenario, step: Step, result: StepResult, run_id: str) -> None:
        self.scenario, self.step, self.result, self.run_id = scenario, step, result, run_id
        self.correlated: list = []  # (ReturnedMessage, parsed Message)

    def handle(self, events: list[ListenerEvent]) -> None:
        for event in events:
            if event.event_class != EVENT_HL7 or event.full_message is None:
                counts = self.result.non_hl7_events
                counts[event.event_class] = counts.get(event.event_class, 0) + 1
                continue
            self._handle_hl7(event)

    def _handle_hl7(self, event: ListenerEvent) -> None:
        message = parse_message(event.full_message)
        synthetic = assertions.is_synthetic(message)
        kind, detail = assertions.classify_correlation(
            message, self.scenario.correlate_on, self.result.token, self.run_id
        )
        record = ReturnedMessage(
            text=event.full_message,
            sha256=hashlib.sha256(event.full_message.encode("utf-8")).hexdigest(),
            synthetic=synthetic,
            role=kind,
            received_at=event.timestamp.isoformat(timespec="milliseconds"),
        )
        self.result.returned.append(record)
        if kind == assertions.CORRELATED:
            self.correlated.append((record, message))
            return
        shown = (
            assertions.REDACTED
            if not synthetic
            else (assertions.field_value(message, self.scenario.correlate_on) or "")
        )
        code = FAIL_LATE_OR_DUPLICATE if kind == assertions.LATE else FAIL_UNCORRELATED
        self.result.failures.append(
            _failure(
                self.result.number,
                self.step,
                code,
                f"returned message not counted: {detail} (value {shown!r})",
                field=self.scenario.correlate_on,
                expected=self.result.token,
                actual=shown,
            )
        )


def _wait_for_returns(collector: ReturnCollector, sink: _StepCollector, step: Step, grace: float) -> None:
    """Feed *sink* until the step's return window closes.

    The window closes at ``step.timeout`` seconds. When the expected number
    of returns (>= 1) arrives sooner, listening continues for *grace* more
    seconds to catch extras; with ``returns = 0`` the whole timeout is
    observed, because "nothing came back" can only be judged at the end.
    """
    deadline = time.monotonic() + step.timeout
    pending = collector.take()  # events that arrived before/while sending
    while True:
        sink.handle(pending)
        if step.returns > 0 and len(sink.correlated) >= step.returns:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        pending = collector.wait_new(remaining)
    grace_deadline = time.monotonic() + grace
    while (remaining := grace_deadline - time.monotonic()) > 0:
        sink.handle(collector.wait_new(remaining))


def _judge_returns(result: StepResult, step: Step, sink: _StepCollector, sent_text: str) -> None:
    """Add MISSING_RETURN / EXTRA_RETURN, then run the step's profile on
    each expected return."""
    got = len(sink.correlated)
    if got < step.returns:
        result.failures.append(
            _failure(
                result.number,
                step,
                FAIL_MISSING_RETURN,
                f"expected {step.returns} returned message(s) within {step.timeout:g}s, received {got}",
                expected=str(step.returns),
                actual=str(got),
            )
        )
    elif got > step.returns:
        result.failures.append(
            _failure(
                result.number,
                step,
                FAIL_EXTRA_RETURN,
                f"expected {step.returns} returned message(s), received {got}",
                expected=str(step.returns),
                actual=str(got),
            )
        )
    sent_message = parse_message(sent_text)
    for record, message in sink.correlated[: step.returns]:
        result.failures.extend(profiles.evaluate(step, result.number, sent_message, message, not record.synthetic))


def _run_step(
    scenario: Scenario,
    step: Step,
    number: int,
    run_id: str,
    sender: _Sender,
    collector: ReturnCollector,
    config: RunConfig,
) -> StepResult:
    """Run one step. Raises :class:`InfrastructureError` if the target
    cannot be reached."""
    token = make_token(run_id, number)
    sent = render_template(step.template_text, run_id, number, scenario.patient, hl7_timestamp())
    result = StepResult(
        number=number,
        name=step.name,
        label=step.label,
        mode=step.mode,
        token=token,
        sent=sent,
        returns_expected=step.returns,
    )
    _record_ack(result, step, sender.send(sent, step.timeout))
    if result.failures:
        # No (usable) ACK: the engine did not accept the message, so waiting
        # for a return would only burn the timeout. Anything that does turn up
        # later is reported against a later step (LATE_OR_DUPLICATE).
        return result
    sink = _StepCollector(scenario, step, result, run_id)
    _wait_for_returns(collector, sink, step, config.grace)
    _judge_returns(result, step, sink, sent)
    return result


# ---------------------------------------------------------------------------
# Whole run
# ---------------------------------------------------------------------------


def run_scenario(scenario: Scenario, config: RunConfig, run_id: Optional[str] = None) -> RunResult:
    """Run *scenario* against the engine described by *config*.

    Never raises for run-time trouble: infrastructure problems come back as
    ``RunResult.error`` (exit code 2) with whatever steps completed before it.
    """
    run_id = run_id or make_run_id()
    outcome = RunResult(
        run_id=run_id,
        scenario_name=scenario.name,
        description=scenario.description,
        target=f"{config.target_host}:{config.target_port}",
        listen=f"{config.listen_host}:{config.listen_port}",
        keep_open=config.keep_open,
        started_at=_utc_now_iso(),
    )
    collector = ReturnCollector()
    listener = MllpListener(config.listen_host, config.listen_port, on_event=collector.on_event)
    try:
        listener.start()
    except OSError as exc:
        outcome.error = f"cannot listen on {config.listen_host}:{config.listen_port}: {exc.strerror or exc}"
        outcome.finished_at = _utc_now_iso()
        return outcome

    sender = _Sender(config.target_host, config.target_port, config.keep_open)
    try:
        for number, step in enumerate(scenario.steps, start=1):
            outcome.steps.append(_run_step(scenario, step, number, run_id, sender, collector, config))
    except InfrastructureError as exc:
        outcome.error = f"cannot reach target {outcome.target}: {exc}"
    finally:
        sender.close()
        listener.stop()
        # Derived from what was actually received, so it cannot disagree with
        # the per-message ``synthetic`` flags in the result.
        outcome.non_synthetic_data_seen = any(not m.synthetic for s in outcome.steps for m in s.returned)
        outcome.finished_at = _utc_now_iso()
    return outcome
