"""HL7 field reference notation: parsing (with aliases) and canonical formatting.

Canonical display form: ``SEG-3.1.2`` -- hyphen before the field number,
dots before component and subcomponent numbers, and an optional bracketed
repetition right after the field number: ``PID-3[2]`` or ``PID-3[2].1``.
The canonical formatter never emits ``^`` (the HL7 component separator) in
a reference string -- that would be confusable with the wire format.

Two aliases are accepted on input (but never produced on output):

* ``SEG.3.1``  -- dot-style, dots throughout instead of a hyphen before
  the field number (matches how some interface engines write references).
* ``SEG-3-1``  -- hyphens throughout.

**Segment repetition** (multiple segments sharing an ID, e.g. more than one
OBX in a message) is addressed with a bracket directly after the segment
ID, *before* the field separator: ``OBX[2]-5.1`` means "field 5,
subcomponent 1, of the *second* OBX segment". This is deliberately a
different bracket position from field repetition (``PID-3[2]``, bracket
right after the field number) so the two can never be confused, and both
can combine: ``OBX[2]-5[3].1`` addresses the 3rd repetition of field 5 in
the 2nd OBX. Segment occurrence 1 (or unspecified) is the implicit default
and is never emitted by the formatter, matching field-repetition rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# seg           : 2-3 alphanumerics, e.g. MSH, PID, ZZ1
# seg repeat    : optional [digits] immediately after the segment ID --
#                 which occurrence of a repeated segment (e.g. 2nd OBX)
# field sep     : '-' or '.'
# field         : digits
# repetition    : optional [digits] immediately after the field number --
#                 which repetition of that field
# component sep : '-' or '.'
# component     : digits
# subcomp sep   : '-' or '.'
# subcomponent  : digits
_REFERENCE_RE = re.compile(
    r"""^
    (?P<seg>[A-Za-z][A-Za-z0-9]{1,2})
    (?:\[(?P<segrep>\d+)\])?
    (?:[-.](?P<field>\d+))?
    (?:\[(?P<rep>\d+)\])?
    (?:[-.](?P<comp>\d+))?
    (?:[-.](?P<subcomp>\d+))?
    $""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class Reference:
    """A parsed segment/field/component/subcomponent reference.

    ``segment_occurrence`` and ``repetition`` are ``None`` when not
    specified in the input (callers that need a concrete occurrence/
    repetition should treat ``None`` as 1, the first one -- that default is
    intentionally left to the caller so that round-tripping
    ``format_reference(parse_reference(s))`` doesn't silently invent a
    ``[1]`` the user didn't type).
    """

    segment: str
    segment_occurrence: Optional[int] = None
    field: Optional[int] = None
    repetition: Optional[int] = None
    component: Optional[int] = None
    subcomponent: Optional[int] = None


def parse_reference(text: str) -> Optional[Reference]:
    """Parse a reference string, accepting the canonical form and both
    documented aliases. Returns ``None`` (never raises) if *text* doesn't
    look like a reference at all.
    """
    if not text:
        return None
    match = _REFERENCE_RE.match(text.strip())
    if not match:
        return None
    groups = match.groupdict()
    return Reference(
        segment=groups["seg"].upper(),
        segment_occurrence=int(groups["segrep"]) if groups["segrep"] is not None else None,
        field=int(groups["field"]) if groups["field"] is not None else None,
        repetition=int(groups["rep"]) if groups["rep"] is not None else None,
        component=int(groups["comp"]) if groups["comp"] is not None else None,
        subcomponent=int(groups["subcomp"]) if groups["subcomp"] is not None else None,
    )


def format_reference(ref: Reference) -> str:
    """Format a :class:`Reference` in canonical notation.

    Canonical rules: an optional bracketed segment occurrence right after
    the segment ID (omitted when 1 or unspecified), hyphen before the
    field number, brackets for a field repetition greater than 1
    (repetition 1, or no repetition specified, is never shown -- it's the
    implicit default), dots before component and subcomponent numbers.
    Never emits ``^``.
    """
    out = ref.segment
    if ref.segment_occurrence is not None and ref.segment_occurrence != 1:
        out += f"[{ref.segment_occurrence}]"
    if ref.field is None:
        return out
    out += f"-{ref.field}"
    if ref.repetition is not None and ref.repetition != 1:
        out += f"[{ref.repetition}]"
    if ref.component is not None:
        out += f".{ref.component}"
        if ref.subcomponent is not None:
            out += f".{ref.subcomponent}"
    return out
