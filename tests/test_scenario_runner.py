"""Tests for the scenario runner (``python -m mshroom run``, EXPERIMENTAL).

Real sockets on 127.0.0.1, no mocks. The engine under test is replaced by
:class:`StubEngine`, a small in-process MLLP server that ACKs the sender and
then forwards -- unchanged, transformed, twice, or not at all, as each test's
*behavior* function decides -- to the runner's return port. That covers the
whole loop the runner exists for: send, ACK, return, judge.

The runner is driven through ``mshroom.scenario.cli.main`` (exactly what the
command line calls), and results are read back from the JSON file it writes,
so these tests also pin down the shape of that file.

Timeouts are short (0.5 s per step) and the extra-return grace window is
shortened by the ``fast_grace`` fixture, keeping the file quick.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pytest

from hl7kit.ack import build_ack
from hl7kit.mllp import extract_frames, frame, send_message
from hl7kit.parser import parse_message
from mshroom.scenario import assertions, cli, runner
from mshroom.scenario.loader import load_scenario, render_template
from mshroom.scenario.model import ScenarioError, make_run_id

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = REPO_ROOT / "corpus" / "scenarios" / "adt_lifecycle" / "scenario.toml"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A currently free loopback port (small race, acceptable in tests)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def set_field(text: str, segment: str, field_no: int, value: str) -> str:
    """Return *text* with one field of the first *segment* replaced -- the
    stub engine's "transformation". Works on the raw ``|``-split segment, so
    it is only for simple fields (MSH counts field 1 as the separator)."""
    segments = text.split("\r")
    for i, seg in enumerate(segments):
        if seg.startswith(segment + "|"):
            parts = seg.split("|")
            index = field_no - 1 if segment == "MSH" else field_no
            parts[index] = value
            segments[i] = "|".join(parts)
            break
    return "\r".join(segments)


@dataclass
class Plan:
    """What the stub engine does with one inbound message."""

    ack: Optional[str] = "AA"  # None = send no ACK at all
    forward: Optional[list[str]] = None  # None = forward the message unchanged
    junk: bytes = b""  # raw bytes to fire at the return port (non-HL7 traffic)


Behavior = Callable[[str, int], Plan]


class StubEngine:
    """A tiny fake interface engine.

    Accepts MLLP connections on its own port, and for every framed message
    calls ``behavior(text, n)`` (n = 1-based count of messages received) to
    learn what to do. It records the texts and the number of connections
    accepted (the keep-open test reads that).
    """

    def __init__(self, return_port: int, behavior: Optional[Behavior] = None) -> None:
        self.return_port = return_port
        self.behavior: Behavior = behavior or (lambda text, n: Plan())
        self.received: list[str] = []
        self.connections = 0
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._running = True
        self._threads = [threading.Thread(target=self._accept_loop, daemon=True)]
        self._threads[0].start()

    def stop(self) -> None:
        self._running = False
        self._server.close()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _serve(self, conn: socket.socket) -> None:
        buffer = b""
        conn.settimeout(0.2)
        while self._running:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            payloads, buffer = extract_frames(buffer + chunk)
            for payload in payloads:
                self._handle(conn, payload.decode("utf-8"))

    def _handle(self, conn: socket.socket, text: str) -> None:
        self.received.append(text)
        plan = self.behavior(text, len(self.received))
        if plan.ack is not None:
            conn.sendall(frame(build_ack(parse_message(text), plan.ack).encode("utf-8")))
        if plan.junk:
            with socket.create_connection(("127.0.0.1", self.return_port), timeout=2) as sock:
                sock.sendall(plan.junk)
        for outgoing in [text] if plan.forward is None else plan.forward:
            send_message("127.0.0.1", self.return_port, outgoing, timeout=2)


@pytest.fixture(autouse=True)
def fast_grace(monkeypatch):
    """Shorten the extra-return grace window (default 1 s) for every test."""
    monkeypatch.setattr(runner, "EXTRA_RETURN_GRACE_SECONDS", 0.3)


@pytest.fixture
def engines():
    """Factory for stub engines; all are stopped at teardown."""
    made: list[StubEngine] = []

    def make(return_port: int, behavior: Optional[Behavior] = None) -> StubEngine:
        engine = StubEngine(return_port, behavior)
        made.append(engine)
        return engine

    yield make
    for engine in made:
        engine.stop()


TEMPLATE = (
    "MSH|^~\\&|MSHROOM|TESTFAC|ENGINE|TESTFAC|{{now}}||ADT^A01^ADT_A01|{{token}}|P|2.5.1\n"
    "EVN|A01|{{now}}\n"
    "PID|1||{{patient.identifier}}^^^MSHROOMTEST^MR||{{patient.family}}^{{patient.given}}||19540106|M\n"
    "PV1|1|I|WARD^301^A^TESTFAC\n"
)


def write_scenario(
    directory: Path,
    steps: str = "",
    header: str = 'synthetic = true\ncorrelate_on = "MSH-10"',
    patient: str = 'identifier = "MSHROOM-TEST-0001"\nfamily = "Holmes"\ngiven = "Sherlock"',
    template: str = TEMPLATE,
) -> Path:
    """Write a scenario.toml (+ template.hl7) into *directory*. *steps* is the
    TOML for the ``[[step]]`` blocks; the default is one plain passthrough step."""
    (directory / "template.hl7").write_text(template, encoding="utf-8")
    steps = steps or '[[step]]\nname = "only"\ntemplate = "template.hl7"\ntimeout = 0.5\n'
    path = directory / "scenario.toml"
    path.write_text(
        f'[scenario]\nname = "test"\ndescription = "test scenario"\n{header}\n\n[patient]\n{patient}\n\n{steps}',
        encoding="utf-8",
    )
    return path


def step_block(name: str = "s", extra: str = "", timeout: float = 0.5) -> str:
    return f'[[step]]\nname = "{name}"\ntemplate = "template.hl7"\ntimeout = {timeout}\n{extra}\n'


def run_cli(
    scenario: Path,
    target_port: int,
    listen_port: int,
    tmp_path: Path,
    capsys,
    extra: Optional[list[str]] = None,
) -> tuple[int, dict, str]:
    """Run ``cli.main`` and return ``(exit code, result JSON, stdout)``.
    The JSON is ``{}`` if no result file was written."""
    json_path = tmp_path / "result.json"
    argv = [
        str(scenario),
        "--target",
        f"127.0.0.1:{target_port}",
        "--listen",
        str(listen_port),
        "--listen-host",
        "127.0.0.1",
        "--json",
        str(json_path),
        *(extra or []),
    ]
    code = cli.main(argv)
    out = capsys.readouterr().out
    result = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
    return code, result, out


def failure_codes(step: dict) -> list[str]:
    return [f["code"] for f in step["failures"]]


def run_with_engine(engines, tmp_path, capsys, scenario, behavior=None, extra=None):
    """Start a stub engine for *behavior*, run *scenario*, return (code, result, out, engine)."""
    listen_port = _free_port()
    engine = engines(listen_port, behavior)
    code, result, out = run_cli(scenario, engine.port, listen_port, tmp_path, capsys, extra)
    return code, result, out, engine


# ---------------------------------------------------------------------------
# The shipped scenario, end to end
# ---------------------------------------------------------------------------


def test_shipped_lifecycle_passthrough_passes(engines, tmp_path, capsys):
    code, result, out, engine = run_with_engine(engines, tmp_path, capsys, SHIPPED)
    assert code == 0, out
    assert [s["status"] for s in result["steps"]] == ["PASS"] * 4
    assert [s["name"] for s in result["steps"]] == ["register", "admit", "transfer", "discharge"]
    assert len(engine.received) == 4
    assert result["exit_code"] == 0 and result["error"] is None
    assert "PASS" in out and "FAIL" not in out.replace("FAILED", "")


def test_shipped_templates_render_valid_adt(tmp_path):
    """Every shipped template renders to a v2.5.1 ADT with EVN/PID/PV1, the
    token in MSH-10 and the fictional patient's synthetic identifier."""
    scenario = load_scenario(SHIPPED)
    triggers = []
    for number, step in enumerate(scenario.steps, start=1):
        text = render_template(
            step.template_text, "MSHR20260101000000abcdef", number, scenario.patient, "20260101000000"
        )
        message = parse_message(text)
        assert [s.seg_id for s in message.segments] == ["MSH", "EVN", "PID", "PV1"]
        assert assertions.field_value(message, "MSH-12") == "2.5.1"
        assert assertions.field_value(message, "MSH-10") == f"MSHR20260101000000abcdef-{number}"
        assert assertions.is_synthetic(message)
        triggers.append(assertions.field_value(message, "MSH-9.2"))
    assert triggers == ["A04", "A01", "A02", "A03"]


def test_shipped_pv1_fields_sit_in_their_documented_positions():
    """PV1-2 class, PV1-6 prior location (transfer), PV1-19 visit, PV1-45 discharge time."""
    scenario = load_scenario(SHIPPED)
    rendered = {
        s.name: parse_message(
            render_template(s.template_text, "MSHR20260101000000abcdef", 1, scenario.patient, "20260101000000")
        )
        for s in scenario.steps
    }
    assert assertions.field_value(rendered["admit"], "PV1-19") == "MSHROOM-TEST-V0001"
    assert assertions.field_value(rendered["transfer"], "PV1-6.2") == "301"
    assert assertions.field_value(rendered["discharge"], "PV1-45") == "20260101000000"
    assert assertions.field_value(rendered["register"], "PV1-2") == "O"


# ---------------------------------------------------------------------------
# Passthrough / assert judgement
# ---------------------------------------------------------------------------


def test_transformed_field_is_a_passthrough_diff_naming_step_and_field(engines, tmp_path, capsys):
    def behavior(text, n):
        return Plan(forward=[set_field(text, "PID", 5, "CHANGED^NAME")] if n == 2 else None)

    code, result, out, _ = run_with_engine(engines, tmp_path, capsys, SHIPPED, behavior)
    assert code == 1
    statuses = [s["status"] for s in result["steps"]]
    assert statuses == ["PASS", "FAIL", "PASS", "PASS"]
    failure = result["steps"][1]["failures"][0]
    assert failure["code"] == "PASSTHROUGH_DIFF"
    assert failure["step"] == 2 and failure["step_name"] == "admit"
    assert failure["diffs"] == [{"field": "PID-5", "expected": "HOLMES^SHERLOCK", "actual": "CHANGED^NAME"}]
    assert "PASSTHROUGH_DIFF" in out and "PID-5" in out and "admit" in out


def test_routing_and_timestamp_fields_are_ignored_by_default(engines, tmp_path, capsys):
    def behavior(text, n):
        rewritten = set_field(text, "MSH", 3, "SOMEENGINE")
        rewritten = set_field(rewritten, "MSH", 4, "OTHERFAC")
        rewritten = set_field(rewritten, "MSH", 7, "20991231235959")
        return Plan(forward=[rewritten])

    code, _, out, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 0, out


def test_custom_ignore_list_replaces_the_default(engines, tmp_path, capsys):
    """With ignore = [] even MSH-3 differences are reported."""
    scenario = write_scenario(tmp_path, step_block(extra="ignore = []"))
    behavior = lambda text, n: Plan(forward=[set_field(text, "MSH", 3, "SOMEENGINE")])  # noqa: E731
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, scenario, behavior)
    assert code == 1
    assert result["steps"][0]["failures"][0]["diffs"][0]["field"] == "MSH-3"


def test_assert_mode_expect_equals_passes(engines, tmp_path, capsys):
    steps = step_block(extra='mode = "assert"\n[[step.expect]]\nfield = "PID-5.1"\nequals = "HOLMES"')
    behavior = lambda text, n: Plan(forward=[set_field(text, "PID", 5, "HOLMES^Sherlock")])  # noqa: E731
    code, result, out, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path, steps), behavior)
    assert code == 0, out
    assert result["steps"][0]["mode"] == "assert"


def test_assert_mode_expect_equals_fails_with_field_expected_actual(engines, tmp_path, capsys):
    steps = step_block(extra='mode = "assert"\n[[step.expect]]\nfield = "PID-5.1"\nequals = "HOLMES"')
    code, result, out, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path, steps))
    assert code == 1
    failure = result["steps"][0]["failures"][0]
    assert failure["code"] == "ASSERTION_FAILED"
    assert failure["field"] == "PID-5.1"
    assert failure["expected"] == "equals 'HOLMES'"
    assert failure["actual"] == "Holmes"
    assert "ASSERTION_FAILED" in out


def test_assert_mode_ignores_unrelated_differences(engines, tmp_path, capsys):
    """assert mode does not compare the whole message: PID-8 changed, only PID-5.1 is checked."""
    steps = step_block(extra='mode = "assert"\n[[step.expect]]\nfield = "PID-5.1"\nequals = "Holmes"')
    behavior = lambda text, n: Plan(forward=[set_field(text, "PID", 8, "F")])  # noqa: E731
    code, _, out, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path, steps), behavior)
    assert code == 0, out


def test_expect_regex_and_present(engines, tmp_path, capsys):
    extra = (
        'mode = "assert"\n'
        '[[step.expect]]\nfield = "PID-5"\nregex = "Holmes\\\\^S.*"\n'
        '[[step.expect]]\nfield = "PV1-3"\npresent = true\n'
        '[[step.expect]]\nfield = "PV1-9"\npresent = false\n'
        '[[step.expect]]\nfield = "PID-5.2"\nregex = "nope"\n'
    )
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path, step_block(extra=extra)))
    assert code == 1
    failures = result["steps"][0]["failures"]
    assert [f["field"] for f in failures] == ["PID-5.2"]  # only the deliberately wrong one


# ---------------------------------------------------------------------------
# Return-count and correlation failures
# ---------------------------------------------------------------------------


def test_engine_returns_nothing_is_missing_return(engines, tmp_path, capsys):
    behavior = lambda text, n: Plan(forward=[])  # noqa: E731
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 1
    failure = result["steps"][0]["failures"][0]
    assert failure["code"] == "MISSING_RETURN"
    assert failure["step"] == 1 and failure["expected"] == "1" and failure["actual"] == "0"


def test_engine_returns_twice_is_extra_return(engines, tmp_path, capsys):
    behavior = lambda text, n: Plan(forward=[text, text])  # noqa: E731
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 1
    assert failure_codes(result["steps"][0]) == ["EXTRA_RETURN"]


def test_rewritten_control_id_is_uncorrelated_never_matched(engines, tmp_path, capsys):
    behavior = lambda text, n: Plan(forward=[set_field(text, "MSH", 10, "ENGINE-REWROTE-THIS")])  # noqa: E731
    code, result, out, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 1
    step = result["steps"][0]
    assert "UNCORRELATED" in failure_codes(step)
    assert "MISSING_RETURN" in failure_codes(step)  # the real return never showed up
    assert step["returns_correlated"] == 0
    assert step["returned"][0]["role"] == "uncorrelated"
    assert "UNCORRELATED" in out


def test_previous_steps_token_is_late_or_duplicate(engines, tmp_path, capsys):
    scenario = write_scenario(tmp_path, step_block("first") + step_block("second"))

    def behavior(text, n):
        # On the second message the engine re-sends the FIRST message instead.
        return Plan(forward=[engine_first[0]] if n == 2 else None)

    engine_first: list[str] = []

    def recording(text, n):
        if n == 1:
            engine_first.append(text)
        return behavior(text, n)

    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, scenario, recording)
    assert code == 1
    first, second = result["steps"]
    assert first["status"] == "PASS"
    late = [f for f in second["failures"] if f["code"] == "LATE_OR_DUPLICATE"]
    assert len(late) == 1 and late[0]["step"] == 2
    assert "step 1 of this run" in late[0]["message"]
    assert "MISSING_RETURN" in failure_codes(second)


def test_token_from_another_run_is_late_or_duplicate(engines, tmp_path, capsys):
    other_run = make_run_id()
    behavior = lambda text, n: Plan(forward=[set_field(text, "MSH", 10, f"{other_run}-1")])  # noqa: E731
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 1
    late = [f for f in result["steps"][0]["failures"] if f["code"] == "LATE_OR_DUPLICATE"]
    assert late and "different run" in late[0]["message"]


def test_ack_ae_is_ack_not_aa(engines, tmp_path, capsys):
    behavior = lambda text, n: Plan(ack="AE", forward=[])  # noqa: E731
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 1
    step = result["steps"][0]
    assert failure_codes(step) == ["ACK_NOT_AA"]  # no MISSING_RETURN noise on top
    assert step["ack"]["code"] == "AE" and step["ack"]["ok"] is False


def test_no_ack_is_no_ack(engines, tmp_path, capsys):
    behavior = lambda text, n: Plan(ack=None, forward=[])  # noqa: E731
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 1
    step = result["steps"][0]
    assert failure_codes(step) == ["NO_ACK"]
    assert step["ack"]["ok"] is False and step["ack"]["error"]


def test_failed_step_does_not_stop_later_steps(engines, tmp_path, capsys):
    scenario = write_scenario(tmp_path, step_block("first") + step_block("second"))
    behavior = lambda text, n: Plan(ack="AE", forward=[]) if n == 1 else Plan()  # noqa: E731
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, scenario, behavior)
    assert code == 1
    assert [s["status"] for s in result["steps"]] == ["FAIL", "PASS"]


def test_returns_zero_filter_passes_when_nothing_comes_back(engines, tmp_path, capsys):
    scenario = write_scenario(tmp_path, step_block(extra="returns = 0"))
    behavior = lambda text, n: Plan(forward=[])  # noqa: E731
    code, result, out, _ = run_with_engine(engines, tmp_path, capsys, scenario, behavior)
    assert code == 0, out
    assert result["steps"][0]["returns_expected"] == 0


def test_returns_zero_fails_when_engine_forwards_anyway(engines, tmp_path, capsys):
    scenario = write_scenario(tmp_path, step_block(extra="returns = 0"))
    code, result, _, _ = run_with_engine(engines, tmp_path, capsys, scenario)
    assert code == 1
    assert failure_codes(result["steps"][0]) == ["EXTRA_RETURN"]


def test_non_hl7_traffic_on_return_port_is_counted_not_judged(engines, tmp_path, capsys):
    behavior = lambda text, n: Plan(junk=b"GET / HTTP/1.1\r\n\r\n")  # noqa: E731
    code, result, out, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 0, out
    assert result["steps"][0]["non_hl7_events"] == {"HTTP_PROBE": 1}


# ---------------------------------------------------------------------------
# Keep-open
# ---------------------------------------------------------------------------


def test_keep_open_uses_one_connection_for_all_steps(engines, tmp_path, capsys):
    code, result, out, engine = run_with_engine(engines, tmp_path, capsys, SHIPPED, extra=["--keep-open"])
    assert code == 0, out
    assert result["keep_open"] is True
    assert engine.connections == 1
    assert len(engine.received) == 4


def test_default_mode_uses_a_connection_per_step(engines, tmp_path, capsys):
    _, _, _, engine = run_with_engine(engines, tmp_path, capsys, SHIPPED)
    assert engine.connections == 4


# ---------------------------------------------------------------------------
# Infrastructure errors (exit 2)
# ---------------------------------------------------------------------------


def test_return_port_in_use_is_exit_2(engines, tmp_path, capsys):
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        busy = squatter.getsockname()[1]
        code, result, out = run_cli(write_scenario(tmp_path), _free_port(), busy, tmp_path, capsys)
    assert code == 2
    assert result["exit_code"] == 2 and "cannot listen" in result["error"]
    assert "ERROR" in out


def test_target_refused_is_exit_2(tmp_path, capsys):
    # Windows reports a refused loopback connection only after ~2 s of SYN
    # retries, so this step needs a longer timeout than the others.
    scenario = write_scenario(tmp_path, step_block(timeout=8))
    code, result, out = run_cli(scenario, _free_port(), _free_port(), tmp_path, capsys)
    assert code == 2
    assert "cannot reach target" in result["error"]
    assert "refused" in result["error"].lower()
    assert result["steps"] == []
    assert "ERROR" in out


def test_target_unresolvable_is_exit_2(tmp_path, capsys):
    scenario = write_scenario(tmp_path)
    code = cli.main(
        [str(scenario), "--target", "no-such-host.invalid:6000", "--listen", str(_free_port()),
         "--listen-host", "127.0.0.1", "--json", str(tmp_path / "r.json")]
    )  # fmt: skip
    assert code == 2
    assert "cannot reach target" in json.loads((tmp_path / "r.json").read_text())["error"]


def test_bad_target_argument_is_exit_2(tmp_path, capsys):
    code = cli.main([str(write_scenario(tmp_path)), "--target", "not-a-target", "--listen", "1"])
    assert code == 2
    assert "HOST:PORT" in capsys.readouterr().err


@pytest.mark.parametrize("header", ['synthetic = false\ncorrelate_on = "MSH-10"', 'correlate_on = "MSH-10"'])
def test_scenario_not_marked_synthetic_is_refused_exit_2(tmp_path, capsys, header):
    code = cli.main([str(write_scenario(tmp_path, header=header)), "--target", "127.0.0.1:1", "--listen", "0"])
    assert code == 2
    assert "synthetic" in capsys.readouterr().err


def test_unknown_placeholder_is_exit_2(tmp_path, capsys):
    scenario = write_scenario(tmp_path, template=TEMPLATE.replace("{{now}}", "{{nowish}}", 1))
    code = cli.main([str(scenario), "--target", "127.0.0.1:1", "--listen", "0"])
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown placeholder" in err and "nowish" in err


def test_unknown_patient_key_placeholder_is_exit_2(tmp_path, capsys):
    scenario = write_scenario(tmp_path, template=TEMPLATE.replace("{{patient.given}}", "{{patient.middle}}"))
    assert cli.main([str(scenario), "--target", "127.0.0.1:1", "--listen", "0"]) == 2
    assert "patient.middle" in capsys.readouterr().err


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"patient": 'identifier = "12345"'}, "MSHROOM-TEST-"),
        ({"patient": 'identifier = "MSHROOM-TEST-1"\nfamily = "A|B"'}, "delimiter"),
        ({"template": TEMPLATE.replace("{{token}}", "FIXEDID")}, "token"),
        ({"template": TEMPLATE.replace("{{patient.identifier}}", "REALMRN")}, "PID-3"),
        ({"steps": '[[step]]\nname = "s"\ntemplate = "template.hl7"\nretruns = 2\n'}, "unknown key"),
        ({"steps": '[[step]]\nname = "s"\ntemplate = "missing.hl7"\n'}, "cannot read template"),
        ({"steps": step_block(extra='mode = "assert"')}, "expect"),
        ({"steps": step_block(extra='mode = "magic"')}, "mode"),
        ({"steps": step_block(extra='[[step.expect]]\nfield = "PID-5"\nequals = "a"\nregex = "b"')}, "exactly one"),
    ],
)
def test_bad_scenarios_are_load_errors(tmp_path, kwargs, fragment):
    with pytest.raises(ScenarioError, match=fragment):
        load_scenario(write_scenario(tmp_path, **kwargs))


def test_invalid_toml_is_a_load_error(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("[scenario\nname = ", encoding="utf-8")
    with pytest.raises(ScenarioError, match="not valid TOML"):
        load_scenario(path)


def test_run_command_dispatches_without_importing_pywebview():
    """``python -m mshroom run`` reaches the runner (exit 2 for a missing
    scenario file) and never imports pywebview or starts the web app."""
    code = (
        "import sys\n"
        "from mshroom.__main__ import main\n"
        "rc = main(['run', 'no-such-file.toml', '--target', '127.0.0.1:1', '--listen', '0'])\n"
        "assert 'webview' not in sys.modules, 'pywebview was imported'\n"
        "assert 'app.main' not in sys.modules, 'the web app was imported'\n"
        "sys.exit(rc)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert "cannot read scenario file" in proc.stderr


# ---------------------------------------------------------------------------
# PHI / synthetic guard and JSON
# ---------------------------------------------------------------------------


def test_non_synthetic_return_is_hashed_not_stored_and_warned(engines, tmp_path, capsys):
    secret = "REALMRN-987654"
    behavior = lambda text, n: Plan(forward=[set_field(text, "PID", 3, f"{secret}^^^HOSP^MR")])  # noqa: E731
    code, result, out, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path), behavior)
    assert code == 1  # PID-3 also differs from what was sent
    assert result["non_synthetic_data_seen"] is True
    returned = result["steps"][0]["returned"][0]
    assert returned["synthetic"] is False and returned["body"] is None
    assert len(returned["sha256"]) == 64
    assert "WARNING" in out
    # The identifier must not leak anywhere: not the JSON, not the console report.
    assert secret not in json.dumps(result)
    assert secret not in out
    assert result["steps"][0]["failures"][0]["diffs"][0]["actual"] == assertions.REDACTED


def test_synthetic_return_body_is_stored(engines, tmp_path, capsys):
    _, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path))
    returned = result["steps"][0]["returned"][0]
    assert returned["synthetic"] is True and "MSHROOM-TEST-0001" in returned["body"]
    assert result["non_synthetic_data_seen"] is False


def test_json_result_shape(engines, tmp_path, capsys):
    _, result, _, _ = run_with_engine(engines, tmp_path, capsys, write_scenario(tmp_path))
    assert result["schema"] == "mshroom-scenario-result/0"
    assert set(result) == {
        "schema", "run_id", "scenario", "target", "listen", "keep_open", "started_at",
        "finished_at", "exit_code", "error", "non_synthetic_data_seen", "steps",
    }  # fmt: skip
    assert result["scenario"] == {"name": "test", "description": "test scenario"}
    step = result["steps"][0]
    assert set(step) == {
        "step", "name", "label", "mode", "status", "token", "sent", "ack", "returns_expected",
        "returns_correlated", "returned", "non_hl7_events", "failures",
    }  # fmt: skip
    assert step["token"] == f"{result['run_id']}-1"
    assert step["ack"]["code"] == "AA" and step["ack"]["ok"] is True
    assert step["token"] in step["sent"] and step["sent"].endswith("\r")
    assert set(step["returned"][0]) == {"role", "received_at", "synthetic", "sha256", "body"}


def test_result_written_to_runs_directory_by_default(engines, tmp_path, capsys, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    scenario = write_scenario(tmp_path)
    listen_port = _free_port()
    engine = engines(listen_port)
    code = cli.main(
        [
            str(scenario),
            "--target",
            f"127.0.0.1:{engine.port}",
            "--listen",
            str(listen_port),
            "--listen-host",
            "127.0.0.1",
        ]
    )
    assert code == 0
    files = list((workdir / "runs").glob("MSHR*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["run_id"] == files[0].stem


def test_timeout_option_sets_default_step_timeout(tmp_path):
    scenario = write_scenario(tmp_path, '[[step]]\nname = "s"\ntemplate = "template.hl7"\n')
    assert load_scenario(scenario, default_timeout=2.5).steps[0].timeout == 2.5


# ---------------------------------------------------------------------------
# Pure comparison helpers
# ---------------------------------------------------------------------------


def _msg(*segments: str):
    return parse_message("\r".join(segments) + "\r")


def test_diff_reports_missing_extra_and_reordered_segments():
    a = _msg("MSH|^~\\&|A|B|C|D|1||ADT^A01|X|P|2.5.1", "PID|1||ID1", "PV1|1|I")
    missing = _msg("MSH|^~\\&|A|B|C|D|1||ADT^A01|X|P|2.5.1", "PID|1||ID1")
    assert [d.field for d in assertions.diff_messages(a, missing)] == ["PV1"]
    assert [d.field for d in assertions.diff_messages(missing, a)] == ["PV1"]
    reordered = _msg("MSH|^~\\&|A|B|C|D|1||ADT^A01|X|P|2.5.1", "PV1|1|I", "PID|1||ID1")
    assert [d.field for d in assertions.diff_messages(a, reordered)] == ["(segment order)"]


def test_diff_treats_trailing_empty_fields_as_equal_and_ignore_covers_segments():
    a = _msg("MSH|^~\\&|A|B|C|D|1||ADT^A01|X|P|2.5.1", "PID|1||ID1")
    b = _msg("MSH|^~\\&|A|B|C|D|1||ADT^A01|X|P|2.5.1", "PID|1||ID1||")
    assert assertions.diff_messages(a, b) == []
    c = _msg("MSH|^~\\&|A|B|C|D|1||ADT^A01|X|P|2.5.1", "PID|1||ID2")
    assert [d.field for d in assertions.diff_messages(a, c)] == ["PID-3"]
    assert assertions.diff_messages(a, c, ignore=("PID",)) == []
    assert assertions.diff_messages(a, c, ignore=("PID-3",)) == []


def test_diff_pairs_repeated_segments_by_occurrence():
    a = _msg("MSH|^~\\&|A|B|C|D|1||ORU^R01|X|P|2.5.1", "OBX|1|ST|A||1", "OBX|2|ST|B||2")
    b = _msg("MSH|^~\\&|A|B|C|D|1||ORU^R01|X|P|2.5.1", "OBX|1|ST|A||1", "OBX|2|ST|B||9")
    assert [d.field for d in assertions.diff_messages(a, b)] == ["OBX[2]-5"]
