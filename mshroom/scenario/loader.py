"""Scenario loading: TOML file + HL7 templates in, validated :class:`Scenario` out.

EXPERIMENTAL: the file format (``docs/scenarios.md``) may still change.

This module is the only place that reads scenario files. It is strict on
purpose, because the run that follows sends network traffic and a typo that
silently changed behaviour (a misspelled ``returns`` falling back to 1)
would produce a confusing report much later:

* Unknown keys are errors, not ignored.
* ``synthetic = true`` is **required**. A scenario that does not say so is
  refused; the runner never sends anything it was not told is fictional.
* The ``[patient]`` identifier must start with ``MSHROOM-TEST-``, and no
  patient value may contain an HL7 delimiter or a line break (either would
  corrupt every message built from it).
* Every ``{{placeholder}}`` in every template must be known. The templates
  are also *dry-run rendered* here, so a template that does not put
  ``{{token}}`` into the correlation field, or whose PID-3 is not
  synthetic, fails at load time (exit 2) instead of mid-run.

Every problem is raised as :class:`ScenarioError` with a plain-English message
naming the file (and step) at fault; the CLI turns that into exit code 2.
"""

from __future__ import annotations

import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hl7kit.notation import parse_reference
from hl7kit.parser import normalize_line_endings, parse_message

from .assertions import field_value, is_synthetic, parse_ignore_entry
from .model import (
    SYNTHETIC_PREFIX,
    TOKEN_RE,
    Expectation,
    Scenario,
    ScenarioError,
    Step,
    make_run_id,
    make_token,
)
from .profiles import DEFAULT_PASSTHROUGH_IGNORE, PROFILES

#: Default seconds a step waits for its returned message(s).
DEFAULT_STEP_TIMEOUT = 10.0

#: Characters a patient value must not contain: the HL7 field, component,
#: repetition, escape and subcomponent separators, plus line breaks.
_FORBIDDEN_IN_VALUES = set("|^~\\&\r\n")

_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}")

_SCENARIO_KEYS = {"name", "description", "synthetic", "correlate_on"}
_STEP_KEYS = {"name", "template", "label", "returns", "timeout", "mode", "ignore", "expect"}
_EXPECT_KEYS = {"field", "equals", "regex", "present"}
_FIXED_PLACEHOLDERS = {"token", "run_id", "step", "now"}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def hl7_timestamp(moment: Optional[datetime] = None) -> str:
    """HL7 timestamp ``yyyyMMddHHmmss`` (UTC), the value of ``{{now}}``."""
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")


def _lookup(name: str, values: dict[str, str], patient: dict[str, str]) -> Optional[str]:
    """Value of placeholder *name*, or ``None`` when it is not a known one."""
    if name in _FIXED_PLACEHOLDERS:
        return values[name]
    if name.startswith("patient."):
        return patient.get(name[len("patient.") :])
    return None


def render_template(template_text: str, run_id: str, step_no: int, patient: dict[str, str], now: str) -> str:
    """Fill in a template and return the message ready for the wire.

    Substitutes ``{{token}}``, ``{{run_id}}``, ``{{step}}``, ``{{now}}`` and
    ``{{patient.<key>}}``. An unknown placeholder raises
    :class:`ScenarioError` (a template typo must never reach the wire as
    literal ``{{...}}`` text). Line endings are normalized to ``\\r`` --
    HL7's segment terminator, whatever the template file used -- blank
    lines are dropped, and exactly one trailing ``\\r`` is kept.
    """
    values = {"token": make_token(run_id, step_no), "run_id": run_id, "step": str(step_no), "now": now}

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = _lookup(name, values, patient)
        if value is None:
            raise ScenarioError(f"unknown placeholder {{{{{name}}}}} in template")
        return value

    filled = _PLACEHOLDER_RE.sub(substitute, template_text)
    normalized, _ = normalize_line_endings(filled)
    segments = [line for line in normalized.split("\r") if line.strip()]
    return "\r".join(segments) + "\r"


def _check_template(step: Step, correlate_on: str, patient: dict[str, str], where: str) -> None:
    """Dry-run render *step*'s template and check the things that would
    otherwise only fail mid-run. Raises :class:`ScenarioError`."""
    dummy_run = make_run_id()
    try:
        text = render_template(step.template_text, dummy_run, 1, patient, hl7_timestamp())
    except ScenarioError as exc:
        raise ScenarioError(f"{where}: template {step.template_file}: {exc}") from None
    message = parse_message(text)
    if message.segment("MSH") is None:
        raise ScenarioError(f"{where}: template {step.template_file} has no MSH segment")
    found = TOKEN_RE.search(field_value(message, correlate_on) or "")
    if found is None or found.group(0) != make_token(dummy_run, 1):
        raise ScenarioError(
            f"{where}: template {step.template_file} does not put {{{{token}}}} in {correlate_on} "
            "(the field the runner uses to match returned messages to steps)"
        )
    if message.segment("PID") is not None and not is_synthetic(message):
        raise ScenarioError(
            f"{where}: template {step.template_file} has a PID-3 that does not start with {SYNTHETIC_PREFIX}"
        )


# ---------------------------------------------------------------------------
# TOML sections
# ---------------------------------------------------------------------------


def _reject_unknown(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ScenarioError(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _require_str(table: dict[str, Any], key: str, where: str, default: Optional[str] = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or (default is None and not value.strip()):
        raise ScenarioError(f"{where}: '{key}' is required and must be a non-empty string")
    return value


def _load_patient(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ScenarioError("[patient] table is required (fictional demographics; see docs/scenarios.md)")
    patient: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ScenarioError(f"[patient] {key}: values must be strings")
        if _FORBIDDEN_IN_VALUES & set(value):
            raise ScenarioError(f"[patient] {key}: value contains an HL7 delimiter or line break")
        patient[key] = value
    if not patient.get("identifier", "").startswith(SYNTHETIC_PREFIX):
        raise ScenarioError(f"[patient] identifier is required and must start with {SYNTHETIC_PREFIX}")
    return patient


def _load_expectation(raw: Any, where: str) -> Expectation:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: expect entries must be tables")
    _reject_unknown(raw, _EXPECT_KEYS, where)
    field_ref = _require_str(raw, "field", where)
    ref = parse_reference(field_ref)
    if ref is None or ref.field is None:
        raise ScenarioError(f"{where}: field {field_ref!r} is not a field reference like PID-5.1")
    tests = [key for key in ("equals", "regex", "present") if key in raw]
    if len(tests) != 1:
        raise ScenarioError(f"{where}: give exactly one of equals / regex / present (got {len(tests)})")
    if "equals" in raw and not isinstance(raw["equals"], str):
        raise ScenarioError(f"{where}: equals must be a string")
    if "present" in raw and not isinstance(raw["present"], bool):
        raise ScenarioError(f"{where}: present must be true or false")
    if "regex" in raw:
        if not isinstance(raw["regex"], str):
            raise ScenarioError(f"{where}: regex must be a string")
        try:
            re.compile(raw["regex"])
        except re.error as exc:
            raise ScenarioError(f"{where}: invalid regex: {exc}") from None
    return Expectation(field=field_ref, equals=raw.get("equals"), regex=raw.get("regex"), present=raw.get("present"))


def _load_ignore(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_PASSTHROUGH_IGNORE
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ScenarioError(f'{where}: ignore must be a list of field references, e.g. ["MSH-7"]')
    for entry in raw:
        if parse_ignore_entry(entry) is None:
            raise ScenarioError(f"{where}: ignore entry {entry!r} must be a field (MSH-7) or a segment (PV1)")
    return tuple(raw)


def _load_step(raw: Any, number: int, base_dir: Path, default_timeout: float) -> Step:
    where = f"step {number}"
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: must be a table")
    _reject_unknown(raw, _STEP_KEYS, where)
    name = _require_str(raw, "name", where, default=f"step {number}")
    where = f"step {number} ({name})"
    template_file = _require_str(raw, "template", where)
    template_path = base_dir / template_file
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"{where}: cannot read template {template_file}: {exc.strerror or exc}") from None

    returns = raw.get("returns", 1)
    if isinstance(returns, bool) or not isinstance(returns, int) or returns < 0:
        raise ScenarioError(f"{where}: returns must be a whole number >= 0")
    timeout = raw.get("timeout", default_timeout)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ScenarioError(f"{where}: timeout must be a number of seconds > 0")
    mode = raw.get("mode", "passthrough")
    if mode not in PROFILES:
        raise ScenarioError(f"{where}: mode must be one of {', '.join(sorted(PROFILES))} (got {mode!r})")

    expect_raw = raw.get("expect", [])
    if not isinstance(expect_raw, list):
        raise ScenarioError(f"{where}: expect must be an array of tables ([[step.expect]])")
    expect = tuple(_load_expectation(item, f"{where} expect") for item in expect_raw)
    if mode == "assert" and (not expect or returns < 1):
        raise ScenarioError(f"{where}: mode 'assert' needs returns >= 1 and at least one [[step.expect]]")

    return Step(
        name=name,
        template_file=template_file,
        template_text=template_text,
        label=_require_str(raw, "label", where, default=""),
        returns=returns,
        timeout=float(timeout),
        mode=mode,
        ignore=_load_ignore(raw.get("ignore"), where),
        expect=expect,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_scenario(path: str | Path, default_timeout: float = DEFAULT_STEP_TIMEOUT) -> Scenario:
    """Load and fully validate the scenario file at *path*.

    Template paths are resolved relative to the scenario file's directory.
    *default_timeout* applies to steps that do not set their own ``timeout``.
    Raises :class:`ScenarioError` for anything wrong; on success the scenario
    is safe to run.
    """
    scenario_path = Path(path)
    try:
        with scenario_path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ScenarioError(f"cannot read scenario file {scenario_path.name}: {exc.strerror or exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ScenarioError(f"{scenario_path.name} is not valid TOML: {exc}") from None

    header = data.get("scenario")
    if not isinstance(header, dict):
        raise ScenarioError(f"{scenario_path.name}: [scenario] table is required")
    _reject_unknown(header, _SCENARIO_KEYS, "[scenario]")
    if header.get("synthetic") is not True:
        raise ScenarioError(
            "refusing to run: [scenario] must set synthetic = true (MSHroom only sends data declared fictional)"
        )
    correlate_on = _require_str(header, "correlate_on", "[scenario]", default="MSH-10")
    ref = parse_reference(correlate_on)
    if ref is None or ref.field is None:
        raise ScenarioError(f"[scenario] correlate_on {correlate_on!r} is not a field reference like MSH-10")

    patient = _load_patient(data.get("patient"))
    raw_steps = data.get("step")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScenarioError("at least one [[step]] is required")

    steps = tuple(_load_step(raw, n, scenario_path.parent, default_timeout) for n, raw in enumerate(raw_steps, start=1))
    for number, step in enumerate(steps, start=1):
        _check_template(step, correlate_on, patient, f"step {number} ({step.name})")

    return Scenario(
        name=_require_str(header, "name", "[scenario]"),
        description=_require_str(header, "description", "[scenario]", default=""),
        synthetic=True,
        correlate_on=correlate_on,
        patient=patient,
        steps=steps,
        source_name=scenario_path.name,
    )
