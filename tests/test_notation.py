"""Tests for hl7kit.notation: reference parsing (canonical + aliases) and
canonical formatting.
"""

from __future__ import annotations

from pathlib import Path

from hl7kit.notation import Reference, format_reference, parse_reference
from hl7kit.parser import parse_message

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


def test_parse_canonical_field():
    ref = parse_reference("PID-5")
    assert ref == Reference(segment="PID", field=5)


def test_parse_canonical_component():
    ref = parse_reference("PID-5.1")
    assert ref == Reference(segment="PID", field=5, component=1)


def test_parse_canonical_subcomponent():
    ref = parse_reference("SEG-3.1.2")
    assert ref == Reference(segment="SEG", field=3, component=1, subcomponent=2)


def test_parse_canonical_repetition():
    ref = parse_reference("PID-3[2]")
    assert ref == Reference(segment="PID", field=3, repetition=2)


def test_parse_canonical_repetition_with_component():
    ref = parse_reference("PID-3[2].1")
    assert ref == Reference(segment="PID", field=3, repetition=2, component=1)


def test_parse_segment_only():
    ref = parse_reference("MSH")
    assert ref == Reference(segment="MSH")


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def test_alias_dot_style_dots():
    """"SEG.3.1" (dot style: dots throughout) must parse the same as the
    canonical hyphen form."""
    canonical = parse_reference("PID-3.1")
    alias = parse_reference("PID.3.1")
    assert alias == canonical


def test_alias_hyphens_throughout():
    """"SEG-3-1" (hyphens throughout) must parse the same as the canonical
    dotted form."""
    canonical = parse_reference("PID-3.1")
    alias = parse_reference("PID-3-1")
    assert alias == canonical


def test_alias_mixed_field_component_subcomponent():
    canonical = parse_reference("OBX-5.1.2")
    assert parse_reference("OBX.5.1.2") == canonical
    assert parse_reference("OBX-5-1-2") == canonical


def test_alias_lowercase_segment_normalized_to_uppercase():
    ref = parse_reference("pid-5")
    assert ref.segment == "PID"


# ---------------------------------------------------------------------------
# Formatting: canonical output only, never emits '^'
# ---------------------------------------------------------------------------


def test_format_field_only():
    assert format_reference(Reference(segment="PID", field=5)) == "PID-5"


def test_format_component():
    assert format_reference(Reference(segment="PID", field=5, component=1)) == "PID-5.1"


def test_format_subcomponent():
    assert format_reference(Reference(segment="SEG", field=3, component=1, subcomponent=2)) == "SEG-3.1.2"


def test_format_repetition():
    assert format_reference(Reference(segment="PID", field=3, repetition=2)) == "PID-3[2]"


def test_format_repetition_of_one_is_implicit_not_shown():
    # Repetition 1 (or unspecified) is the default and never rendered as [1].
    assert format_reference(Reference(segment="PID", field=3, repetition=1)) == "PID-3"


def test_format_segment_only():
    assert format_reference(Reference(segment="MSH")) == "MSH"


def test_format_never_emits_caret():
    for ref in [
        Reference(segment="PID", field=5, component=1, subcomponent=2),
        Reference(segment="PID", field=3, repetition=4, component=2),
    ]:
        assert "^" not in format_reference(ref)


# ---------------------------------------------------------------------------
# Round trip: parse(format(ref)) == ref
# ---------------------------------------------------------------------------


def test_round_trip_field():
    ref = Reference(segment="PID", field=5)
    assert parse_reference(format_reference(ref)) == ref


def test_round_trip_component():
    ref = Reference(segment="PID", field=5, component=2)
    assert parse_reference(format_reference(ref)) == ref


def test_round_trip_subcomponent():
    ref = Reference(segment="OBX", field=5, component=1, subcomponent=3)
    assert parse_reference(format_reference(ref)) == ref


def test_round_trip_repetition():
    ref = Reference(segment="PID", field=3, repetition=2)
    assert parse_reference(format_reference(ref)) == ref


# ---------------------------------------------------------------------------
# Invalid input: never raises, returns None
# ---------------------------------------------------------------------------


def test_parse_empty_string_returns_none():
    assert parse_reference("") is None


def test_parse_garbage_returns_none():
    assert parse_reference("not a reference!!") is None
    assert parse_reference("123-456") is None
    assert parse_reference("PID--") is None


def test_parse_none_like_input_handled():
    # A caller might pass whitespace-only input from a UI field.
    assert parse_reference("   ") is None


# ---------------------------------------------------------------------------
# Integration with the parser: resolve real corpus references
# ---------------------------------------------------------------------------


def test_resolve_against_real_message_all_notations():
    text = (CORPUS_DIR / "adt_a01.hl7").read_bytes().decode("utf-8")
    message = parse_message(text)

    assert message.get("MSH-9.1").raw == "ADT"
    assert message.get("MSH.9.1").raw == "ADT"  # alias
    assert message.get("MSH-9-1").raw == "ADT"  # alias
    assert message.get("PID-5.2").raw == "JANE"
    assert message.get("PID-3.1").raw == "MRN100234"


def test_resolve_nonexistent_reference_returns_none():
    text = (CORPUS_DIR / "adt_a01.hl7").read_bytes().decode("utf-8")
    message = parse_message(text)
    assert message.get("ZZZ-1") is None
    assert message.get("PID-9999") is None
    assert message.get("not a reference") is None


# ---------------------------------------------------------------------------
# Segment occurrence: OBX[2]-5 addresses the SECOND OBX segment (Phase 1)
# ---------------------------------------------------------------------------


def test_parse_segment_occurrence():
    ref = parse_reference("OBX[2]-5")
    assert ref == Reference(segment="OBX", segment_occurrence=2, field=5)


def test_parse_segment_occurrence_with_component():
    ref = parse_reference("OBX[2]-5.1")
    assert ref == Reference(segment="OBX", segment_occurrence=2, field=5, component=1)


def test_parse_segment_occurrence_alone():
    ref = parse_reference("OBX[3]")
    assert ref == Reference(segment="OBX", segment_occurrence=3)


def test_parse_segment_occurrence_combined_with_field_repetition():
    """Both bracket positions at once: 2nd OBX segment, 3rd repetition of
    its field 5, component 1."""
    ref = parse_reference("OBX[2]-5[3].1")
    assert ref == Reference(
        segment="OBX", segment_occurrence=2, field=5, repetition=3, component=1
    )


def test_parse_segment_occurrence_dot_alias():
    assert parse_reference("OBX[2].5.1") == parse_reference("OBX[2]-5.1")


def test_format_segment_occurrence():
    ref = Reference(segment="OBX", segment_occurrence=2, field=5, component=1)
    assert format_reference(ref) == "OBX[2]-5.1"


def test_format_segment_occurrence_of_one_is_implicit_not_shown():
    ref = Reference(segment="OBX", segment_occurrence=1, field=5)
    assert format_reference(ref) == "OBX-5"


def test_round_trip_segment_occurrence():
    ref = Reference(segment="OBX", segment_occurrence=4, field=5, repetition=2, component=1)
    assert parse_reference(format_reference(ref)) == ref


def test_resolve_segment_occurrence_against_multi_obx_message():
    """oru_r01.hl7 has four OBX segments; each must be addressable."""
    text = (CORPUS_DIR / "oru_r01.hl7").read_bytes().decode("utf-8")
    message = parse_message(text)

    assert message.get("OBX-5").raw == "7.2"  # first OBX, implicit
    assert message.get("OBX[1]-5").raw == "7.2"  # first OBX, explicit
    assert message.get("OBX[2]-5").raw == "13.8"
    assert message.get("OBX[2]-3.2").raw == "Hemoglobin"
    assert message.get("OBX[3]-5").raw == "245"
    assert message.get("OBX[4]-8").raw == "HH"


def test_resolve_segment_occurrence_out_of_range_returns_none():
    text = (CORPUS_DIR / "oru_r01.hl7").read_bytes().decode("utf-8")
    message = parse_message(text)
    assert message.get("OBX[5]-5") is None
    assert message.get("OBX[99]") is None


def test_resolve_segment_occurrence_alone_returns_segment():
    text = (CORPUS_DIR / "oru_r01.hl7").read_bytes().decode("utf-8")
    message = parse_message(text)
    seg = message.get("OBX[2]")
    assert seg is not None
    assert seg.seg_id == "OBX"
    assert seg.raw.startswith("OBX|2|")


def test_resolve_field_repetition_still_works_alongside_segment_occurrence():
    """PID-3[2] (field repetition) must keep working, including combined
    with a segment occurrence."""
    text = (
        "MSH|^~\\&|APP|FAC|APP2|FAC2|20260702||ADT^A08|C1|P|2.5.1\r"
        "PID|1||MRN1^^^H1^MR~MRN2^^^H2^MR\r"
        "OBX|1|ST|N1||A~B\r"
        "OBX|2|ST|N2||C~D\r"
    )
    message = parse_message(text)

    assert message.get("PID-3[1].1").raw == "MRN1"
    assert message.get("PID-3[2].1").raw == "MRN2"
    assert message.get("PID-3[2].4").raw == "H2"
    assert message.get("OBX-5[2]").raw == "B"  # first OBX, 2nd repetition
    assert message.get("OBX[2]-5[1]").raw == "C"
    assert message.get("OBX[2]-5[2]").raw == "D"
    assert message.get("PID-3[3]") is None  # out of range repetition
