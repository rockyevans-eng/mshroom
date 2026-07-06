"""Offset-preserving HL7 v2 parser.

Every node in the parse tree (:class:`Segment`, :class:`Field`,
:class:`Repetition`, :class:`Component`, :class:`Subcomponent`) carries a
``start``/``end`` character offset pair into the *normalized* message text
(see :func:`normalize_line_endings`), plus a ``raw`` string that is always
exactly ``normalized_text[start:end]``. That equality is the load-bearing
invariant this whole toolkit rests on -- it is what lets the Viewer turn a
tree click into a raw-text highlight and back.

Design notes
------------
* HL7 segments are terminated by ``\\r``. Pasted messages sometimes arrive
  with ``\\r\\n`` or bare ``\\n`` line endings; :func:`normalize_line_endings`
  converts all of these to a single canonical ``\\r`` and returns an offset
  map back to the original text so callers can translate offsets both ways.
* The **MSH quirk**: the field separator character that follows the
  3-letter segment ID is itself MSH-1, and the encoding-characters field
  that follows *that* is MSH-2. Every other field in the message is
  delimited by the field separator in the ordinary way. This module treats
  MSH (and, for the same reason, BHS/FHS batch headers) specially so that
  MSH-9 is always the message type and MSH-10 is always the message
  control ID.
* Parsing never raises for malformed or non-HL7 input. Best-effort
  structure is always produced. This is deliberate: the MLLP listener
  (later phase) will hand this parser probe traffic, truncated messages,
  and outright garbage, and it must never crash the process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_FIELD_SEPARATOR = "|"
DEFAULT_COMPONENT_SEPARATOR = "^"
DEFAULT_REPETITION_SEPARATOR = "~"
DEFAULT_ESCAPE_CHARACTER = "\\"
DEFAULT_SUBCOMPONENT_SEPARATOR = "&"

# Segment types whose header carries the encoding-characters field and
# therefore need the MSH-style special-cased field 1 / field 2 parsing.
_HEADER_SEGMENTS_WITH_ENCODING = ("MSH", "BHS", "FHS")


# ---------------------------------------------------------------------------
# Encoding characters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncodingChars:
    """The five HL7 encoding characters declared in MSH-1/MSH-2 (or defaults)."""

    field_sep: str = DEFAULT_FIELD_SEPARATOR
    component_sep: str = DEFAULT_COMPONENT_SEPARATOR
    repetition_sep: str = DEFAULT_REPETITION_SEPARATOR
    escape_char: str = DEFAULT_ESCAPE_CHARACTER
    subcomponent_sep: str = DEFAULT_SUBCOMPONENT_SEPARATOR

    @classmethod
    def default(cls) -> "EncodingChars":
        """Standard ``|^~\\&`` encoding characters."""
        return cls()

    @classmethod
    def from_field2(cls, field_sep: str, field2_raw: str) -> "EncodingChars":
        """Build from MSH-1 (field separator) and the raw MSH-2 text.

        Tolerant of a short/garbled MSH-2: any missing position falls back
        to the HL7-standard default for that position rather than raising.
        """
        defaults = cls.default()
        component_sep = field2_raw[0] if len(field2_raw) > 0 else defaults.component_sep
        repetition_sep = field2_raw[1] if len(field2_raw) > 1 else defaults.repetition_sep
        escape_char = field2_raw[2] if len(field2_raw) > 2 else defaults.escape_char
        subcomponent_sep = field2_raw[3] if len(field2_raw) > 3 else defaults.subcomponent_sep
        return cls(
            field_sep=field_sep,
            component_sep=component_sep,
            repetition_sep=repetition_sep,
            escape_char=escape_char,
            subcomponent_sep=subcomponent_sep,
        )


# ---------------------------------------------------------------------------
# Tree nodes
# ---------------------------------------------------------------------------


@dataclass
class Subcomponent:
    """Leaf node: one subcomponent of a component (or the component's own
    value, when it has no further subcomponents)."""

    index: int
    start: int
    end: int
    raw: str
    decoded: str  # escape-sequence-decoded value, for display


@dataclass
class Component:
    """One component of a field repetition."""

    index: int
    start: int
    end: int
    raw: str
    subcomponents: list[Subcomponent] = field(default_factory=list)

    def subcomp(self, index: int) -> Optional[Subcomponent]:
        """Return the 1-based subcomponent, or ``None`` if it doesn't exist."""
        for sc in self.subcomponents:
            if sc.index == index:
                return sc
        return None


@dataclass
class Repetition:
    """One repetition of a field (fields separated by ``~`` repeat)."""

    index: int
    start: int
    end: int
    raw: str
    components: list[Component] = field(default_factory=list)

    def comp(self, index: int) -> Optional[Component]:
        """Return the 1-based component, or ``None`` if it doesn't exist."""
        for c in self.components:
            if c.index == index:
                return c
        return None


@dataclass
class Field:
    """One field of a segment (1-based index, following segment-specific
    numbering -- see the MSH quirk note in the module docstring)."""

    index: int
    start: int
    end: int
    raw: str
    repetitions: list[Repetition] = field(default_factory=list)

    def rep(self, index: int = 1) -> Optional[Repetition]:
        """Return the 1-based repetition (default: the first), or ``None``."""
        for r in self.repetitions:
            if r.index == index:
                return r
        return None

    def comp(self, index: int, repetition: int = 1) -> Optional[Component]:
        """Convenience: component *index* of repetition *repetition* (default 1)."""
        r = self.rep(repetition)
        return r.comp(index) if r else None

    def value(self) -> str:
        """The raw value of the first repetition (the common case)."""
        r = self.rep(1)
        return r.raw if r else ""


@dataclass
class Segment:
    """One segment (one line, in the pre-normalization sense)."""

    seg_id: str
    start: int
    end: int
    raw: str
    fields: list[Field] = field(default_factory=list)

    def get_field(self, index: int) -> Optional[Field]:
        """Return the 1-based field, or ``None`` if it doesn't exist."""
        for f in self.fields:
            if f.index == index:
                return f
        return None


@dataclass
class Message:
    """A fully parsed HL7 v2 message."""

    original_text: str
    text: str  # normalized text (\\r line endings); all offsets refer to this
    offset_map: list[int]  # len == len(text) + 1; offset_map[i] = index into original_text
    encoding: EncodingChars
    segments: list[Segment] = field(default_factory=list)
    had_trailing_terminator: bool = False

    # -- navigation -----------------------------------------------------

    def segments_by_id(self, seg_id: str) -> list[Segment]:
        """All segments with the given 3-character ID (case-sensitive, as HL7 requires)."""
        return [s for s in self.segments if s.seg_id == seg_id]

    def segment(self, seg_id: str, occurrence: int = 1) -> Optional[Segment]:
        """The *occurrence*-th (1-based) segment with the given ID, or ``None``."""
        matches = self.segments_by_id(seg_id)
        if 1 <= occurrence <= len(matches):
            return matches[occurrence - 1]
        return None

    def get(self, reference: str) -> Optional[object]:
        """Resolve a notation string (e.g. ``"PID-5.1"``) to the node it names.

        Accepts every alias :func:`hl7kit.notation.parse_reference` accepts,
        including segment occurrence (``OBX[2]-5.1`` = field 5, component 1
        of the *second* OBX segment) and field repetition (``PID-3[2]`` =
        second repetition of PID-3). Returns the most specific node
        addressed (Segment, Field, Repetition, Component, or Subcomponent),
        or ``None`` if any part of the path doesn't exist.
        """
        from hl7kit.notation import parse_reference  # local import: avoid a cycle

        ref = parse_reference(reference)
        if ref is None:
            return None
        seg = self.segment(ref.segment, ref.segment_occurrence or 1)
        if seg is None or ref.field is None:
            return seg
        f = seg.get_field(ref.field)
        if f is None:
            return None
        rep = f.rep(ref.repetition or 1)
        if rep is None:
            return None
        if ref.component is None:
            return rep
        comp = rep.comp(ref.component)
        if comp is None:
            return None
        if ref.subcomponent is None:
            return comp
        return comp.subcomp(ref.subcomponent)

    # -- reassembly -------------------------------------------------------

    def reassemble(self) -> str:
        """Rebuild the normalized text from the parsed segments.

        For any message whose original text already used bare ``\\r``
        terminators (i.e. every real HL7 message), this is byte-identical
        to the original text. For pasted text with ``\\r\\n``/``\\n``
        endings, this reproduces the *normalized* form -- use
        :meth:`to_original_offsets` to map back if you need the original.
        """
        body = "\r".join(s.raw for s in self.segments)
        if self.had_trailing_terminator:
            body += "\r"
        return body

    def to_original_offsets(self, start: int, end: int) -> tuple[int, int]:
        """Translate a (start, end) pair in normalized-text space back to
        the original (pre-normalization) text space."""
        return self.offset_map[start], self.offset_map[end]


# ---------------------------------------------------------------------------
# Line-ending normalization
# ---------------------------------------------------------------------------


def normalize_line_endings(text: str) -> tuple[str, list[int]]:
    """Normalize ``\\r\\n`` and bare ``\\n`` to ``\\r``.

    Returns ``(normalized_text, offset_map)`` where ``offset_map`` has
    length ``len(normalized_text) + 1`` and ``offset_map[i]`` is the index
    into *text* corresponding to normalized position *i* (with
    ``offset_map[len(normalized_text)] == len(text)`` as an end sentinel).
    """
    out_chars: list[str] = []
    offset_map: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        offset_map.append(i)
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                out_chars.append("\r")
                i += 2
            else:
                out_chars.append("\r")
                i += 1
        elif ch == "\n":
            out_chars.append("\r")
            i += 1
        else:
            out_chars.append(ch)
            i += 1
    offset_map.append(n)
    return "".join(out_chars), offset_map


# ---------------------------------------------------------------------------
# Escape sequence decoding
# ---------------------------------------------------------------------------

_SIMPLE_ESCAPES = {
    "F": "field_sep",
    "S": "component_sep",
    "T": "subcomponent_sep",
    "R": "repetition_sep",
    "E": "escape_char",
}


def decode_escapes(text: str, encoding: EncodingChars) -> str:
    """Decode display escape sequences (``\\F\\``, ``\\S\\``, ``\\T\\``,
    ``\\R\\``, ``\\E\\``, ``\\.br\\``) into their literal characters.

    Any other or malformed escape sequence is passed through unchanged --
    this function never raises, since it may be handed arbitrary/garbled
    probe or malformed-message text.
    """
    esc = encoding.escape_char
    if esc not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == esc:
            end = text.find(esc, i + 1)
            if end == -1:
                # Unterminated escape: pass the rest through literally.
                out.append(text[i:])
                break
            body = text[i + 1 : end]
            if body in _SIMPLE_ESCAPES:
                out.append(getattr(encoding, _SIMPLE_ESCAPES[body]))
            elif body == ".br":
                out.append("\n")
            else:
                # Unrecognized escape (e.g. \Hxxx\, \Xdd\, \Zxxx\): keep raw.
                out.append(text[i : end + 1])
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Low-level split-with-offsets helper
# ---------------------------------------------------------------------------


def _split_with_offsets(text: str, base_offset: int, sep: str) -> list[tuple[str, int, int]]:
    """Split *text* on single-character *sep*, returning
    ``(token, absolute_start, absolute_end)`` triples in original order.

    If *sep* is falsy (shouldn't normally happen -- guarded by callers with
    a default fallback), the whole text is returned as one token.
    """
    if not sep:
        return [(text, base_offset, base_offset + len(text))]
    tokens: list[tuple[str, int, int]] = []
    start = 0
    n = len(text)
    while True:
        pos = text.find(sep, start)
        if pos == -1:
            tokens.append((text[start:], base_offset + start, base_offset + n))
            break
        tokens.append((text[start:pos], base_offset + start, base_offset + pos))
        start = pos + 1
    return tokens


# ---------------------------------------------------------------------------
# Component / subcomponent / repetition / field parsing
# ---------------------------------------------------------------------------


def _parse_subcomponents(text: str, base_offset: int, encoding: EncodingChars) -> list[Subcomponent]:
    tokens = _split_with_offsets(text, base_offset, encoding.subcomponent_sep)
    result = []
    for idx, (tok, start, end) in enumerate(tokens, start=1):
        result.append(Subcomponent(index=idx, start=start, end=end, raw=tok, decoded=decode_escapes(tok, encoding)))
    return result


def _parse_components(text: str, base_offset: int, encoding: EncodingChars) -> list[Component]:
    tokens = _split_with_offsets(text, base_offset, encoding.component_sep)
    result = []
    for idx, (tok, start, end) in enumerate(tokens, start=1):
        subcomponents = _parse_subcomponents(tok, start, encoding)
        result.append(Component(index=idx, start=start, end=end, raw=tok, subcomponents=subcomponents))
    return result


def _parse_repetitions(text: str, base_offset: int, encoding: EncodingChars) -> list[Repetition]:
    tokens = _split_with_offsets(text, base_offset, encoding.repetition_sep)
    result = []
    for idx, (tok, start, end) in enumerate(tokens, start=1):
        components = _parse_components(tok, start, encoding)
        result.append(Repetition(index=idx, start=start, end=end, raw=tok, components=components))
    return result


def _atomic_field(index: int, text: str, start: int, end: int) -> Field:
    """Build a Field that wraps its entire raw value as a single
    repetition/component/subcomponent chain, without further decomposition.

    Used for MSH-1 (the field separator character itself) and MSH-2 (the
    raw encoding characters) -- splitting either of those on the encoding
    characters they *define* would be nonsensical.
    """
    subcomp = Subcomponent(index=1, start=start, end=end, raw=text, decoded=text)
    comp = Component(index=1, start=start, end=end, raw=text, subcomponents=[subcomp])
    rep = Repetition(index=1, start=start, end=end, raw=text, components=[comp])
    return Field(index=index, start=start, end=end, raw=text, repetitions=[rep])


def _parse_fields(text: str, base_offset: int, encoding: EncodingChars, start_index: int = 1) -> list[Field]:
    tokens = _split_with_offsets(text, base_offset, encoding.field_sep)
    result = []
    for offset, (tok, start, end) in enumerate(tokens):
        idx = start_index + offset
        repetitions = _parse_repetitions(tok, start, encoding)
        result.append(Field(index=idx, start=start, end=end, raw=tok, repetitions=repetitions))
    return result


# ---------------------------------------------------------------------------
# Segment parsing
# ---------------------------------------------------------------------------


def _parse_header_segment(seg_text: str, seg_start: int, field_sep: str) -> tuple[Segment, EncodingChars]:
    """Parse an MSH/BHS/FHS segment: field 1 is the separator character
    itself, field 2 is the raw encoding-characters string, and only fields
    from 3 onward are ordinary field-separated tokens.
    """
    seg_id = seg_text[0:3]
    if len(seg_text) < 4:
        # Too short to even carry a field separator -- malformed, but must
        # not crash. No fields, defaults for encoding.
        segment = Segment(seg_id=seg_id, start=seg_start, end=seg_start + len(seg_text), raw=seg_text, fields=[])
        return segment, EncodingChars.default()

    actual_field_sep = seg_text[3]
    field1 = _atomic_field(1, actual_field_sep, seg_start + 3, seg_start + 4)

    idx2_end = seg_text.find(actual_field_sep, 4)
    if idx2_end == -1:
        # No closing separator after the encoding-characters field: treat
        # the remainder as MSH-2 and there are no further fields.
        field2_raw = seg_text[4:]
        field2 = _atomic_field(2, field2_raw, seg_start + 4, seg_start + len(seg_text))
        encoding = EncodingChars.from_field2(actual_field_sep, field2_raw)
        segment = Segment(
            seg_id=seg_id,
            start=seg_start,
            end=seg_start + len(seg_text),
            raw=seg_text,
            fields=[field1, field2],
        )
        return segment, encoding

    field2_raw = seg_text[4:idx2_end]
    field2 = _atomic_field(2, field2_raw, seg_start + 4, seg_start + idx2_end)
    encoding = EncodingChars.from_field2(actual_field_sep, field2_raw)

    remainder = seg_text[idx2_end + 1 :]
    remainder_start = seg_start + idx2_end + 1
    # Even an empty remainder legitimately parses to one empty field
    # (field 3), matching ordinary field-splitting semantics.
    rest_fields = _parse_fields(remainder, remainder_start, encoding, start_index=3)

    segment = Segment(
        seg_id=seg_id,
        start=seg_start,
        end=seg_start + len(seg_text),
        raw=seg_text,
        fields=[field1, field2] + rest_fields,
    )
    return segment, encoding


def _parse_ordinary_segment(seg_text: str, seg_start: int, encoding: EncodingChars) -> Segment:
    """Parse a non-header segment: SEG_ID + field_sep + field1 + field_sep + ..."""
    seg_id = seg_text[0:3] if len(seg_text) >= 3 else seg_text
    if len(seg_text) < 4:
        # Too short to hold a separator + any field content.
        return Segment(seg_id=seg_id, start=seg_start, end=seg_start + len(seg_text), raw=seg_text, fields=[])

    if seg_text[3] == encoding.field_sep:
        remainder = seg_text[4:]
        remainder_start = seg_start + 4
    else:
        # Malformed: no separator right after the segment ID. Best effort --
        # treat everything after the ID as field 1 content rather than
        # crashing.
        remainder = seg_text[3:]
        remainder_start = seg_start + 3

    fields = _parse_fields(remainder, remainder_start, encoding, start_index=1)
    return Segment(seg_id=seg_id, start=seg_start, end=seg_start + len(seg_text), raw=seg_text, fields=fields)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def parse_message(text: str) -> Message:
    """Parse HL7 v2 message *text* into a :class:`Message` tree.

    Never raises: malformed, truncated, or entirely non-HL7 input always
    produces a best-effort :class:`Message` rather than an exception. This
    is a deliberate contract -- the MLLP listener depends on it to survive
    hostile/garbage traffic without crashing.
    """
    normalized_text, offset_map = normalize_line_endings(text)

    had_trailing = normalized_text.endswith("\r") and len(normalized_text) > 0
    core = normalized_text[:-1] if had_trailing else normalized_text

    segment_texts: list[str] = core.split("\r") if core != "" else []

    # Determine the message-wide field separator and encoding characters
    # from the first header segment (MSH/BHS/FHS), if present.
    field_sep = DEFAULT_FIELD_SEPARATOR
    for seg_text in segment_texts:
        if seg_text[:3] in _HEADER_SEGMENTS_WITH_ENCODING and len(seg_text) >= 4:
            field_sep = seg_text[3]
            break

    segments: list[Segment] = []
    message_encoding = EncodingChars.default()
    found_header = False

    offset = 0
    for seg_text in segment_texts:
        seg_start = offset
        offset += len(seg_text) + 1  # +1 for the \r terminator (or would-be one)

        if seg_text[:3] in _HEADER_SEGMENTS_WITH_ENCODING:
            segment, encoding = _parse_header_segment(seg_text, seg_start, field_sep)
            segments.append(segment)
            if not found_header:
                message_encoding = encoding
                found_header = True
        else:
            # Use the message encoding if we've already seen a header
            # segment; otherwise fall back to defaults (with the detected
            # field separator) for segments that precede/lack one.
            enc = message_encoding if found_header else EncodingChars(field_sep=field_sep)
            segments.append(_parse_ordinary_segment(seg_text, seg_start, enc))

    return Message(
        original_text=text,
        text=normalized_text,
        offset_map=offset_map,
        encoding=message_encoding,
        segments=segments,
        had_trailing_terminator=had_trailing,
    )
