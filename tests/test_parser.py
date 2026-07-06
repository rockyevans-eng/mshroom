"""Tests for hl7kit.parser and hl7kit.dictionary.

Covers, per the Phase 0 acceptance criteria in docs/hl7_workbench/BUILD_PLAN.md:
  * round-trip parse -> reassemble is byte-identical for every golden corpus file
  * every tree node's (start, end) slice of the raw text equals that node's raw value
  * MSH numbering (MSH-9 = message type, MSH-10 = message control ID)
  * hand-authored value checks from each corpus file's *.expected.json
  * malformed and probe inputs never raise unhandled exceptions
  * the field-name dictionary never raises for unknown segments/fields
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl7kit import dictionary
from hl7kit.parser import EncodingChars, decode_escapes, parse_message

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
MALFORMED_DIR = CORPUS_DIR / "malformed"
PROBES_DIR = CORPUS_DIR / "probes"

GOLDEN_FILES = sorted(CORPUS_DIR.glob("*.hl7"))
MALFORMED_FILES = sorted(MALFORMED_DIR.glob("*.hl7"))
PROBE_FILES = sorted(PROBES_DIR.glob("*"))

assert GOLDEN_FILES, "golden corpus is empty -- did generation run?"
assert MALFORMED_FILES, "malformed set is empty -- did generation run?"
assert PROBE_FILES, "probe set is empty -- did generation run?"


def _read_text(path: Path) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw.decode("utf-8")


def _walk_nodes(message):
    """Yield every node in the tree (Segment, Field, Repetition, Component, Subcomponent)."""
    for segment in message.segments:
        yield segment
        for f in segment.fields:
            yield f
            for r in f.repetitions:
                yield r
                for c in r.components:
                    yield c
                    for sc in c.subcomponents:
                        yield sc


# ---------------------------------------------------------------------------
# Golden corpus: round-trip, offsets, MSH numbering, hand-authored checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda p: p.name)
def test_round_trip_is_byte_identical(path: Path):
    text = _read_text(path)
    message = parse_message(text)
    assert message.reassemble() == text


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda p: p.name)
def test_every_node_offset_slice_equals_raw(path: Path):
    text = _read_text(path)
    message = parse_message(text)
    assert message.text == text  # golden corpus already uses bare \r, no normalization needed
    for node in _walk_nodes(message):
        assert message.text[node.start : node.end] == node.raw, (
            f"{type(node).__name__} index={getattr(node, 'index', getattr(node, 'seg_id', '?'))} "
            f"offsets ({node.start}, {node.end}) do not match raw {node.raw!r}"
        )


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda p: p.name)
def test_msh_numbering(path: Path):
    """MSH-1 is the field separator, MSH-2 is the encoding characters, and
    critically MSH-9 = message type / MSH-10 = message control ID -- the
    classic HL7 parser bug this whole module exists to get right."""
    text = _read_text(path)
    message = parse_message(text)
    msh_seg = message.segment("MSH")
    assert msh_seg is not None

    field1 = msh_seg.get_field(1)
    field2 = msh_seg.get_field(2)
    assert field1.raw == "|"
    assert len(field2.raw) == 4  # standard "^~\&"
    assert message.encoding.field_sep == "|"
    assert message.encoding.component_sep == field2.raw[0]
    assert message.encoding.repetition_sep == field2.raw[1]
    assert message.encoding.escape_char == field2.raw[2]
    assert message.encoding.subcomponent_sep == field2.raw[3]

    expected = json.loads((CORPUS_DIR / (path.stem + ".expected.json")).read_text(encoding="utf-8"))
    message_type_field = msh_seg.get_field(9)
    control_id_field = msh_seg.get_field(10)
    assert message_type_field.raw == expected["message_type"]
    assert control_id_field.raw == expected["control_id"]


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda p: p.name)
def test_segment_order_matches_expected(path: Path):
    text = _read_text(path)
    message = parse_message(text)
    expected = json.loads((CORPUS_DIR / (path.stem + ".expected.json")).read_text(encoding="utf-8"))
    assert [s.seg_id for s in message.segments] == expected["segment_ids_in_order"]


@pytest.mark.parametrize("path", GOLDEN_FILES, ids=lambda p: p.name)
def test_expected_checks(path: Path):
    """Hand-authored spot checks (segment/field/component values) recorded
    in each corpus file's *.expected.json, independent of the generator's
    own self-verification step."""
    text = _read_text(path)
    message = parse_message(text)
    expected = json.loads((CORPUS_DIR / (path.stem + ".expected.json")).read_text(encoding="utf-8"))
    for check in expected["checks"]:
        node = message.get(check["ref"])
        assert node is not None, f"reference {check['ref']!r} did not resolve"
        assert node.raw == check["expected_raw"], f"{check['ref']!r}: {node.raw!r} != {check['expected_raw']!r}"


def test_oru_r01_has_multiple_obx():
    message = parse_message(_read_text(CORPUS_DIR / "oru_r01.hl7"))
    obx_segments = message.segments_by_id("OBX")
    assert len(obx_segments) == 4
    values = [seg.get_field(5).raw for seg in obx_segments]
    assert values == ["7.2", "13.8", "245", "5.9"]


def test_z_segment_does_not_error():
    """Unknown/Z-segments must render (parse) without error -- numbers only,
    per BUILD_PLAN section 4."""
    text = "MSH|^~\\&|A|B|C|D|20260101000000||ADT^A01|CTRL1|P|2.5.1\rZZ1|1|foo^bar|baz\r"
    message = parse_message(text)
    zseg = message.segment("ZZ1")
    assert zseg is not None
    assert zseg.get_field(1).raw == "1"
    assert zseg.get_field(2).comp(1).raw == "foo"
    assert zseg.get_field(2).comp(2).raw == "bar"
    # Dictionary must not error for a Z-segment; it should just return None.
    assert dictionary.field_name("ZZ1", 1) is None


# ---------------------------------------------------------------------------
# Malformed inputs: must never raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", MALFORMED_FILES, ids=lambda p: p.name)
def test_malformed_never_raises(path: Path):
    text = _read_text(path)
    message = parse_message(text)  # must not raise
    assert message is not None
    assert isinstance(message.segments, list)


def test_bad_msh_falls_back_to_defaults():
    text = _read_text(MALFORMED_DIR / "bad_msh.hl7")
    message = parse_message(text)
    msh_seg = message.segment("MSH")
    assert msh_seg is not None
    # Encoding chars field was garbled/short ("^~" -- 2 chars, no closing
    # separator) -- component/repetition recovered, escape/subcomponent
    # fell back to HL7 defaults.
    assert message.encoding.component_sep == "^"
    assert message.encoding.repetition_sep == "~"
    assert message.encoding.escape_char == "\\"
    assert message.encoding.subcomponent_sep == "&"
    # The PID segment after it must still parse without error.
    pid = message.segment("PID")
    assert pid is not None
    assert pid.get_field(3).comp(1).raw == "MRN999999"


def test_truncated_message_parses_partial_last_segment():
    text = _read_text(MALFORMED_DIR / "truncated.hl7")
    message = parse_message(text)
    assert message.segment("MSH") is not None
    pid = message.segment("PID")
    assert pid is not None
    # Cut off mid-name; field 5 should just contain the partial text.
    assert pid.get_field(5).raw == "DOE^JANE^MAR"


def test_wrong_encoding_chars_are_read_from_message_not_hardcoded():
    text = _read_text(MALFORMED_DIR / "wrong_encoding_chars.hl7")
    message = parse_message(text)
    assert message.encoding.field_sep == "@"
    assert message.encoding.component_sep == "#"
    assert message.encoding.repetition_sep == "$"
    assert message.encoding.escape_char == "%"
    assert message.encoding.subcomponent_sep == "!"
    msh_seg = message.segment("MSH")
    # MSH-9 split on the *declared* component separator ('#'), not '^'.
    assert msh_seg.get_field(9).comp(1).raw == "ADT"
    assert msh_seg.get_field(9).comp(2).raw == "A01"
    assert message.reassemble() == text


# ---------------------------------------------------------------------------
# Probe inputs: must never raise, even on binary garbage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PROBE_FILES, ids=lambda p: p.name)
def test_probes_never_raise(path: Path):
    with open(path, "rb") as fh:
        raw = fh.read()
    # A real listener would decode permissively before ever reaching the
    # parser; emulate that here rather than requiring the parser to accept
    # bytes directly (it's a text-level module).
    text = raw.decode("utf-8", errors="replace")
    message = parse_message(text)  # must not raise
    assert message is not None


def test_empty_input_does_not_raise():
    message = parse_message("")
    assert message.segments == []


def test_lone_cr_does_not_raise():
    message = parse_message("\r")
    assert message is not None


# ---------------------------------------------------------------------------
# Line-ending normalization
# ---------------------------------------------------------------------------


def test_crlf_normalizes_to_cr_and_offsets_map_back():
    original = "MSH|^~\\&|A|B|C|D|20260101000000||ADT^A01|CTRL1|P|2.5.1\r\nPID|1||123^^^A^MR||DOE^JOHN\r\n"
    message = parse_message(original)
    assert "\r\n" not in message.text
    assert message.text.count("\r") == 2
    pid = message.segment("PID")
    assert pid.get_field(3).raw == "123^^^A^MR"
    # Offsets refer to normalized text; translate back to original and
    # confirm the slice still matches the original text's corresponding
    # (differently-positioned) characters.
    field3 = pid.get_field(3)
    orig_start, orig_end = message.to_original_offsets(field3.start, field3.end)
    assert original[orig_start:orig_end] == field3.raw


def test_bare_lf_normalizes_to_cr():
    original = "MSH|^~\\&|A|B|C|D|20260101000000||ADT^A01|CTRL1|P|2.5.1\nPID|1||123^^^A^MR||DOE^JOHN\n"
    message = parse_message(original)
    assert message.text.count("\r") == 2
    assert message.segment("PID") is not None


# ---------------------------------------------------------------------------
# Escape sequence decoding
# ---------------------------------------------------------------------------


def test_decode_escapes_simple_sequences():
    enc = EncodingChars.default()
    assert decode_escapes(r"a\F\b", enc) == "a|b"
    assert decode_escapes(r"a\S\b", enc) == "a^b"
    assert decode_escapes(r"a\T\b", enc) == "a&b"
    assert decode_escapes(r"a\R\b", enc) == "a~b"
    assert decode_escapes(r"a\E\b", enc) == "a\\b"
    assert decode_escapes(r"line1\.br\line2", enc) == "line1\nline2"


def test_decode_escapes_unterminated_does_not_raise():
    enc = EncodingChars.default()
    assert decode_escapes(r"abc\F", enc) == r"abc\F"  # passed through, no crash


def test_decode_escapes_unrecognized_sequence_passed_through():
    enc = EncodingChars.default()
    text = "\\Zcustom\\"  # \Zcustom\ -- unrecognized escape, passed through as-is
    assert decode_escapes(text, enc) == text


def test_subcomponent_field_decoded_value_present():
    text = 'MSH|^~\\&|A|B|C|D|20260101000000||ADT^A01|CTRL1|P|2.5.1\rNTE|1||line1\\.br\\line2\r'
    message = parse_message(text)
    nte = message.segment("NTE")
    comment = nte.get_field(3)
    subcomp = comment.rep(1).comp(1).subcomp(1)
    assert subcomp.raw == r"line1\.br\line2"
    assert subcomp.decoded == "line1\nline2"


# ---------------------------------------------------------------------------
# Repetitions
# ---------------------------------------------------------------------------


def test_field_repetition_splits_correctly():
    text = "MSH|^~\\&|A|B|C|D|20260101000000||ADT^A01|CTRL1|P|2.5.1\rPID|1||111^^^A^MR~222^^^B^MR||DOE^JOHN\r"
    message = parse_message(text)
    pid = message.segment("PID")
    field3 = pid.get_field(3)
    assert len(field3.repetitions) == 2
    assert field3.rep(1).comp(1).raw == "111"
    assert field3.rep(2).comp(1).raw == "222"


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------


def test_dictionary_known_fields():
    assert dictionary.field_name("PID", 3) == "Patient Identifier List"
    assert dictionary.field_name("PID", 5) == "Patient Name"
    assert dictionary.field_name("MSH", 9) == "Message Type"
    assert dictionary.field_name("MSH", 10) == "Message Control ID"
    assert dictionary.field_name("OBX", 3) == "Observation Identifier"
    assert dictionary.field_name("OBR", 4) == "Universal Service Identifier"


def test_dictionary_unknown_segment_returns_none():
    assert dictionary.field_name("ZZZ", 1) is None
    assert dictionary.field_name("ZZZ", 999) is None


def test_dictionary_unknown_field_number_returns_none():
    assert dictionary.field_name("PID", 9999) is None


def test_dictionary_lowercase_segment_id_is_normalized():
    assert dictionary.field_name("pid", 5) == dictionary.field_name("PID", 5)


def test_dictionary_known_segments_covers_build_plan_list():
    expected_segments = {
        "MSH", "EVN", "PID", "PV1", "PV2", "NK1", "ORC", "OBR", "OBX",
        "TXA", "MFI", "MFE", "SCH", "AL1", "DG1", "IN1", "NTE",
    }
    assert expected_segments.issubset(set(dictionary.known_segments()))
