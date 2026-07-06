"""hl7kit -- a small, offset-preserving HL7 v2 parsing toolkit.

This package is the parser core for HL7 Workbench. Unlike off-the-shelf
parsers (python-hl7, hl7apy), every node produced here carries the
character (start, end) offsets it occupies in the original message text.
That is what lets the Viewer highlight the raw text when you click a tree
node, and vice versa.

Contents:
    parser.py      Structural parser: Message -> Segment -> Field ->
                    Repetition -> Component -> Subcomponent, all with offsets.
    notation.py     Canonical reference notation ("PID-3.1") plus parsing of
                    common aliases ("PID.3.1", "PID-3-1"), field repetitions
                    ("PID-3[2]") and segment occurrences ("OBX[2]-5.1").
    dictionary.py   Static field-name lookup (segment + field number ->
                    human name) for the common segments used in the lab.
    mllp.py         MLLP framing + the client used by the Sender (the
                    Listener's server half arrives in Phase 2).
    ack.py          Build AA/AE/AR ACKs for a parsed message; parse
                    received ACKs.
"""

from hl7kit.parser import parse_message, Message, Segment, Field, Repetition, Component, Subcomponent, EncodingChars

__all__ = [
    "parse_message",
    "Message",
    "Segment",
    "Field",
    "Repetition",
    "Component",
    "Subcomponent",
    "EncodingChars",
]
