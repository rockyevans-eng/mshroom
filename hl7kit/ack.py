"""HL7 v2 ACK generation and parsing.

Two jobs, both small:

* :func:`build_ack` -- given a parsed inbound :class:`~hl7kit.parser.Message`,
  produce the text of an acknowledgment message (``ACK^<trigger>``) with the
  requested code in MSA-1 (``AA`` accept, ``AE`` error, ``AR`` reject) and the
  inbound message's control ID (MSH-10) echoed in MSA-2. Used by the Listener
  (Phase 2) and by the test suite's stub MLLP server.
* :func:`parse_ack` -- given raw ACK text received from a remote system,
  extract the interesting bits (MSA-1 code, MSA-2 echoed control ID, MSA-3
  text) alongside the full parse tree. Used by the Sender to display the
  response.

Neither function raises on malformed input: :func:`build_ack` falls back to
empty strings for fields it can't read from the inbound message, and
:func:`parse_ack` returns an :class:`AckInfo` with ``code=None`` when the
response has no MSA segment at all (which the UI reports as "not an ACK").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from hl7kit.parser import Message, parse_message

#: MSA-1 codes this module knows how to emit.
VALID_ACK_CODES = ("AA", "AE", "AR")


def _get_raw(message: Message, reference: str) -> str:
    """Raw text of a referenced node, or '' when absent (never raises)."""
    node = message.get(reference)
    raw = getattr(node, "raw", None)
    return raw if isinstance(raw, str) else ""


def build_ack(
    inbound: Message,
    code: str = "AA",
    text: str = "",
    sending_app: str = "HL7WORKBENCH",
    sending_facility: str = "LAB",
    control_id: Optional[str] = None,
) -> str:
    """Build the text of an ACK for *inbound*.

    ``code`` must be one of ``AA``/``AE``/``AR`` (MSA-1). ``text`` becomes
    MSA-3 (free-text note, e.g. a parse-error description for AE). MSH-10 of
    the inbound message is echoed into MSA-2, per the standard; the sending/
    receiving application+facility pairs are swapped from the inbound MSH.

    Returns a complete HL7 message string with ``\\r`` segment terminators
    (no trailing terminator). Never raises: any inbound field that can't be
    read is simply left empty in the ACK.
    """
    if code not in VALID_ACK_CODES:
        raise ValueError(f"ACK code must be one of {VALID_ACK_CODES}, got {code!r}")

    inbound_control_id = _get_raw(inbound, "MSH-10")
    inbound_trigger = _get_raw(inbound, "MSH-9.2")
    # Reply goes back to whoever sent the inbound message.
    receiving_app = _get_raw(inbound, "MSH-3")
    receiving_facility = _get_raw(inbound, "MSH-4")
    version = _get_raw(inbound, "MSH-12") or "2.5.1"
    processing_id = _get_raw(inbound, "MSH-11") or "P"

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    ack_control_id = control_id if control_id is not None else f"ACK{timestamp}"
    msg_type = f"ACK^{inbound_trigger}" if inbound_trigger else "ACK"

    msh = (
        f"MSH|^~\\&|{sending_app}|{sending_facility}|{receiving_app}|{receiving_facility}"
        f"|{timestamp}||{msg_type}|{ack_control_id}|{processing_id}|{version}"
    )
    msa = f"MSA|{code}|{inbound_control_id}"
    if text:
        msa += f"|{text}"
    return msh + "\r" + msa


@dataclass(frozen=True)
class AckInfo:
    """The interesting parts of a received ACK, plus its full parse tree.

    ``code`` is ``None`` when the response contains no MSA segment (i.e. it
    isn't an ACK at all); everything else is best-effort empty-string.
    """

    code: Optional[str]  # MSA-1: AA / AE / AR (or CA/CE/CR), None if no MSA
    control_id: str  # MSA-2: control ID of the message being acknowledged
    text: str  # MSA-3: free-text note, often empty
    message: Message  # full parse tree of the ACK, for the mini-tree view

    @property
    def is_accept(self) -> bool:
        """True for AA/CA (the "all good" codes)."""
        return self.code in ("AA", "CA")


def parse_ack(text: str) -> AckInfo:
    """Parse received ACK text into an :class:`AckInfo`. Never raises."""
    message = parse_message(text)
    msa = message.segment("MSA")
    if msa is None:
        return AckInfo(code=None, control_id="", text="", message=message)
    return AckInfo(
        code=_get_raw(message, "MSA-1") or None,
        control_id=_get_raw(message, "MSA-2"),
        text=_get_raw(message, "MSA-3"),
        message=message,
    )
