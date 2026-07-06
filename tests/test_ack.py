"""Tests for hl7kit.ack: ACK generation (AA/AE/AR) and ACK parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from hl7kit.ack import AckInfo, build_ack, parse_ack
from hl7kit.parser import parse_message

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"


def _load(name: str):
    text = (CORPUS_DIR / name).read_bytes().decode("utf-8")
    return parse_message(text)


# ---------------------------------------------------------------------------
# build_ack
# ---------------------------------------------------------------------------


def test_build_aa_ack_echoes_control_id():
    inbound = _load("adt_a01.hl7")
    ack_text = build_ack(inbound, "AA")
    ack = parse_message(ack_text)
    assert ack.get("MSA-1").raw == "AA"
    assert ack.get("MSA-2").raw == inbound.get("MSH-10").raw == "CTRL0001"


def test_build_ack_message_type_carries_inbound_trigger():
    inbound = _load("adt_a01.hl7")  # ADT^A01
    ack = parse_message(build_ack(inbound, "AA"))
    assert ack.get("MSH-9.1").raw == "ACK"
    assert ack.get("MSH-9.2").raw == "A01"


def test_build_ack_swaps_sending_and_receiving():
    inbound = _load("adt_a01.hl7")
    ack = parse_message(build_ack(inbound, "AA", sending_app="HL7WORKBENCH"))
    # Reply is addressed back to the inbound message's sender.
    assert ack.get("MSH-5").raw == inbound.get("MSH-3").raw  # REGISTRATION
    assert ack.get("MSH-6").raw == inbound.get("MSH-4").raw  # GENHOSP
    assert ack.get("MSH-3").raw == "HL7WORKBENCH"


def test_build_ae_ack_with_error_text():
    inbound = _load("oru_r01.hl7")
    ack = parse_message(build_ack(inbound, "AE", text="Parse failure in OBX 2"))
    assert ack.get("MSA-1").raw == "AE"
    assert ack.get("MSA-3").raw == "Parse failure in OBX 2"


def test_build_ar_ack():
    inbound = _load("orm_o01.hl7")
    ack = parse_message(build_ack(inbound, "AR"))
    assert ack.get("MSA-1").raw == "AR"


def test_build_ack_invalid_code_raises():
    inbound = _load("adt_a01.hl7")
    with pytest.raises(ValueError):
        build_ack(inbound, "XX")


def test_build_ack_explicit_control_id():
    inbound = _load("adt_a01.hl7")
    ack = parse_message(build_ack(inbound, "AA", control_id="MYACK42"))
    assert ack.get("MSH-10").raw == "MYACK42"


def test_build_ack_survives_garbage_inbound():
    """A parse of pure garbage still yields an ACK (with empty echoes)."""
    inbound = parse_message("this is not HL7 at all")
    ack_text = build_ack(inbound, "AE", text="not HL7")
    ack = parse_message(ack_text)
    assert ack.get("MSA-1").raw == "AE"
    assert ack.get("MSA-2").raw == ""  # nothing to echo


# ---------------------------------------------------------------------------
# parse_ack
# ---------------------------------------------------------------------------


def test_parse_ack_round_trip():
    inbound = _load("mdm_t02.hl7")
    info = parse_ack(build_ack(inbound, "AA"))
    assert isinstance(info, AckInfo)
    assert info.code == "AA"
    assert info.is_accept
    assert info.control_id == inbound.get("MSH-10").raw


def test_parse_ack_error_code_not_accept():
    inbound = _load("adt_a03.hl7")
    info = parse_ack(build_ack(inbound, "AE", text="boom"))
    assert info.code == "AE"
    assert not info.is_accept
    assert info.text == "boom"


def test_parse_ack_no_msa_segment():
    info = parse_ack("MSH|^~\\&|A|B|C|D|20260702||ADT^A01|X1|P|2.5.1")
    assert info.code is None
    assert not info.is_accept


def test_parse_ack_garbage_never_raises():
    info = parse_ack("\x16\x03\x01 utter garbage \x00\x00")
    assert info.code is None
    assert info.message is not None
