"""Reading, comparing and checking HL7 messages for the scenario runner.

Everything here is a pure function over parsed messages (no sockets, no
files), which is why it is easy to test on its own. Four jobs:

* :func:`field_value` -- resolve a reference such as ``PID-5.1`` to text.
* :func:`check_expectation` -- one ``[[step.expect]]`` test on a message.
* :func:`diff_messages` -- the field-level passthrough comparison.
* :func:`classify_correlation` / :func:`is_synthetic` -- the two questions
  the runner asks about every message that comes back ("whose is it?" and
  "is it safe to store?").

Invariants:

* **Redaction.** Anything that puts a *returned* message's value into a
  result (``actual`` in a diff, an expectation failure, an uncorrelated
  value) takes a ``redact`` flag. When it is true the value is replaced by
  :data:`REDACTED`: a message that did not carry a synthetic patient
  identifier might be real patient data and must never be copied into a
  report or a JSON file.
* **Field-level comparison uses the whole field.** ``PID-5`` compares every
  component and repetition of field 5, so a change to any part of the field
  is caught; ``PID-5.1`` compares only that component.
* Missing and empty are the same. ``PID|1`` and ``PID|1|`` are equal for
  comparison purposes -- trailing empty fields carry no information in HL7,
  and engines routinely add or drop them.
"""

from __future__ import annotations

import re
from typing import Optional

from hl7kit.notation import Reference, parse_reference
from hl7kit.parser import Message, Segment

from .model import SYNTHETIC_PREFIX, TOKEN_RE, Expectation, FieldDiff

#: Shown in place of a value taken from a message that may be real data.
REDACTED = "<redacted: non-synthetic data>"

#: Results of :func:`classify_correlation`.
CORRELATED = "correlated"
LATE = "late"
UNCORRELATED = "uncorrelated"


# ---------------------------------------------------------------------------
# Reading fields
# ---------------------------------------------------------------------------


def field_value(message: Message, reference: str) -> Optional[str]:
    """Text of the node *reference* names, or ``None`` if it does not exist.

    A reference to a whole field with no repetition or component (``PID-5``)
    returns the field's complete raw text, repetitions and all; anything
    more specific (``PID-5.1``, ``PID-3[2]``) returns exactly that node.
    """
    ref = parse_reference(reference)
    if ref is None:
        return None
    if ref.field is not None and ref.repetition is None and ref.component is None:
        segment = message.segment(ref.segment, ref.segment_occurrence or 1)
        node = segment.get_field(ref.field) if segment is not None else None
    else:
        node = message.get(reference)
    raw = getattr(node, "raw", None)
    return raw if isinstance(raw, str) else None


def is_synthetic(message: Message) -> bool:
    """True if every PID-3 repetition starts with the synthetic prefix.

    This is the storage guard: a message failing it is treated as possible
    real data. A message with **no** PID-3 at all also fails -- with no
    identifier we cannot show it is synthetic, so we assume it is not.
    """
    segment = message.segment("PID")
    pid3 = segment.get_field(3) if segment is not None else None
    if pid3 is None or not pid3.repetitions:
        return False
    return all(rep.raw.startswith(SYNTHETIC_PREFIX) for rep in pid3.repetitions)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def classify_correlation(message: Message, correlate_on: str, token: str, run_id: str) -> tuple[str, str]:
    """Decide whose message this is. Returns ``(kind, detail)``.

    * ``CORRELATED`` -- the correlation field holds *this step's* token.
    * ``LATE`` -- it holds a token that is not this step's: a different step
      of this run (an old message arriving late, or a duplicate) or a step of
      another run. *detail* says which.
    * ``UNCORRELATED`` -- no run token at all (the engine replaced or
      dropped the control ID). Deliberately never matched by guessing: an
      unattributable message must be reported, not quietly accepted.
    """
    value = field_value(message, correlate_on) or ""
    found = TOKEN_RE.search(value)
    if found is None:
        return UNCORRELATED, f"{correlate_on} carries no run token"
    if found.group(0) == token:
        return CORRELATED, ""
    if found.group("run") == run_id:
        return LATE, f"token belongs to step {found.group('step')} of this run"
    return LATE, "token belongs to a different run"


# ---------------------------------------------------------------------------
# Expectations (mode = "assert", or extra checks in any mode)
# ---------------------------------------------------------------------------


def check_expectation(message: Message, expectation: Expectation, redact: bool = False) -> Optional[FieldDiff]:
    """Run one expectation against *message*; ``None`` means it passed.

    On failure returns a :class:`FieldDiff` whose ``expected`` is a phrase
    describing the test and ``actual`` the value found (or ``<absent>``).
    """
    actual = field_value(message, expectation.field)
    text = actual or ""
    shown = REDACTED if redact else (actual if actual is not None else "<absent>")

    if expectation.equals is not None:
        if text == expectation.equals:
            return None
        return FieldDiff(expectation.field, f"equals {expectation.equals!r}", shown)
    if expectation.regex is not None:
        if re.fullmatch(expectation.regex, text):
            return None
        return FieldDiff(expectation.field, f"matches /{expectation.regex}/", shown)
    if expectation.present is not None:
        if bool(text) == expectation.present:
            return None
        return FieldDiff(expectation.field, "present" if expectation.present else "absent", shown)
    return None  # loader guarantees one test is set; nothing to check


# ---------------------------------------------------------------------------
# Passthrough comparison
# ---------------------------------------------------------------------------


def parse_ignore_entry(entry: str) -> Optional[Reference]:
    """Parse one ``ignore`` entry: ``MSH-7`` (a field) or ``PV1`` (a whole
    segment). Returns ``None`` if it is neither (loader turns that into an
    error). Component-level entries are refused: the comparison is
    field-level, so ignoring half a field would be misleading."""
    ref = parse_reference(entry)
    if ref is None or ref.component is not None or ref.repetition is not None:
        return None
    return ref


def _is_ignored(seg_id: str, occurrence: int, field_no: Optional[int], ignore: list[Reference]) -> bool:
    """True if an ignore entry covers this segment (``field_no=None``) or
    this field of it. An entry without an occurrence covers every
    occurrence of the segment."""
    for ref in ignore:
        if ref.segment != seg_id:
            continue
        if ref.segment_occurrence is not None and ref.segment_occurrence != occurrence:
            continue
        if ref.field is None or ref.field == field_no:
            return True
    return False


def _keyed_segments(message: Message) -> list[tuple[tuple[str, int], Segment]]:
    """Segments in message order, each keyed ``(segment id, occurrence)``.

    The occurrence number is what lets two OBX segments be compared with
    their counterparts rather than with each other.
    """
    seen: dict[str, int] = {}
    keyed = []
    for segment in message.segments:
        seen[segment.seg_id] = seen.get(segment.seg_id, 0) + 1
        keyed.append(((segment.seg_id, seen[segment.seg_id]), segment))
    return keyed


def _label(seg_id: str, occurrence: int, field_no: Optional[int] = None) -> str:
    """Canonical reference text, e.g. ``PID-5`` or ``OBX[2]-5``."""
    text = seg_id if occurrence == 1 else f"{seg_id}[{occurrence}]"
    return text if field_no is None else f"{text}-{field_no}"


def _field_texts(segment: Segment) -> dict[int, str]:
    return {f.index: f.raw for f in segment.fields}


def _diff_segment(
    key: tuple[str, int], sent: Segment, returned: Segment, ignore: list[Reference], redact: bool
) -> list[FieldDiff]:
    """Field-by-field comparison of two segments with the same key."""
    seg_id, occurrence = key
    sent_fields, returned_fields = _field_texts(sent), _field_texts(returned)
    diffs = []
    for number in sorted(set(sent_fields) | set(returned_fields)):
        if _is_ignored(seg_id, occurrence, number, ignore):
            continue
        expected, actual = sent_fields.get(number, ""), returned_fields.get(number, "")
        if expected != actual:
            diffs.append(FieldDiff(_label(seg_id, occurrence, number), expected, REDACTED if redact else actual))
    return diffs


def diff_messages(
    sent: Message, returned: Message, ignore: tuple[str, ...] = (), redact: bool = False
) -> list[FieldDiff]:
    """Field-level differences between *sent* and *returned*, minus *ignore*.

    Segments are paired by ``(id, occurrence)``. A segment on only one side
    is one diff for the whole segment. If both sides have the same segments
    but in a different order, that is reported as one ``(segment order)``
    diff -- reordered segments change meaning even when every field matches.
    """
    ignored = [ref for ref in (parse_ignore_entry(e) for e in ignore) if ref is not None]
    sent_keyed = _keyed_segments(sent)
    returned_keyed = dict(_keyed_segments(returned))
    sent_by_key = dict(sent_keyed)

    diffs: list[FieldDiff] = []
    for key, segment in sent_keyed:
        if _is_ignored(key[0], key[1], None, ignored):
            continue
        other = returned_keyed.get(key)
        if other is None:
            diffs.append(FieldDiff(_label(*key), "segment present", "segment absent"))
        else:
            diffs.extend(_diff_segment(key, segment, other, ignored, redact))
    for key in returned_keyed:
        if key not in sent_by_key and not _is_ignored(key[0], key[1], None, ignored):
            diffs.append(FieldDiff(_label(*key), "segment absent", "segment present"))

    # Order check only when the two sides hold the same segments (otherwise the
    # missing/extra diffs above already explain the difference).
    sent_order = [k for k, _ in sent_keyed if not _is_ignored(k[0], k[1], None, ignored)]
    returned_order = [k for k in returned_keyed if not _is_ignored(k[0], k[1], None, ignored)]
    if sent_order != returned_order and sorted(sent_order) == sorted(returned_order):
        diffs.append(
            FieldDiff(
                "(segment order)",
                ",".join(_label(*k) for k in sent_order),
                ",".join(_label(*k) for k in returned_order),
            )
        )
    return diffs
