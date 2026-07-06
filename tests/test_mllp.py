"""Tests for hl7kit.mllp: framing helpers and the MLLP client.

The client tests run against small stub TCP servers started on ephemeral
localhost ports inside each test -- no external services needed.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from hl7kit.ack import build_ack, parse_ack
from hl7kit.mllp import END_BLOCK, START_BLOCK, frame, send_message, unframe
from hl7kit.parser import parse_message

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"


def _corpus_text(name: str) -> str:
    return (CORPUS_DIR / name).read_bytes().decode("utf-8")


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def test_frame_wraps_payload():
    assert frame(b"MSH|...") == b"\x0bMSH|...\x1c\x0d"


def test_unframe_extracts_payload():
    assert unframe(b"\x0bHELLO\x1c\x0d") == b"HELLO"


def test_unframe_tolerates_leading_and_trailing_junk():
    assert unframe(b"junk\x0bPAYLOAD\x1c\x0dtrailing") == b"PAYLOAD"


def test_unframe_incomplete_returns_none():
    assert unframe(b"") is None
    assert unframe(b"\x0bPARTIAL") is None
    assert unframe(b"no frame here") is None


def test_frame_unframe_round_trip():
    payload = _corpus_text("adt_a01.hl7").encode("utf-8")
    assert unframe(frame(payload)) == payload


# ---------------------------------------------------------------------------
# Stub servers
# ---------------------------------------------------------------------------


class StubMllpServer:
    """Tiny single-connection MLLP server: reads one frame, ACKs it via
    hl7kit.ack, closes. ``mode`` selects failure behaviors for the error
    tests."""

    def __init__(self, mode: str = "ack", ack_code: str = "AA"):
        self.mode = mode
        self.ack_code = ack_code
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            buffer = b""
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                    payload = unframe(buffer)
                    if payload is not None:
                        break
            except OSError:
                return
            self.received.append(payload)
            if self.mode == "silent":
                # Never respond; let the client time out.
                try:
                    conn.recv(65536)
                except OSError:
                    pass
                return
            if self.mode == "close_no_response":
                return
            if self.mode == "partial_frame":
                conn.sendall(START_BLOCK + b"MSA|AA| never finished")
                return
            inbound = parse_message(payload.decode("utf-8", errors="replace"))
            ack_text = build_ack(inbound, self.ack_code)
            conn.sendall(frame(ack_text.encode("utf-8")))

    def close(self) -> None:
        self._sock.close()


# ---------------------------------------------------------------------------
# Client happy path
# ---------------------------------------------------------------------------


def test_send_receives_aa_ack():
    server = StubMllpServer()
    try:
        text = _corpus_text("adt_a01.hl7")
        result = send_message("127.0.0.1", server.port, text, timeout=5)
        assert result.ok, result.error
        info = parse_ack(result.response)
        assert info.code == "AA"
        assert info.control_id == "CTRL0001"
        # Server received exactly the message we sent (already \r-terminated).
        assert server.received[0] == text.encode("utf-8")
    finally:
        server.close()


def test_send_normalizes_pasted_line_endings():
    """A message pasted with \\r\\n endings goes on the wire with bare \\r."""
    server = StubMllpServer()
    try:
        text = "MSH|^~\\&|A|B|C|D|20260702||ADT^A01|LE1|P|2.5.1\r\nEVN|A01|20260702\r\n"
        result = send_message("127.0.0.1", server.port, text, timeout=5)
        assert result.ok, result.error
        assert b"\n" not in server.received[0]
        assert server.received[0].count(b"\r") == 2
    finally:
        server.close()


def test_send_receives_ae_ack():
    server = StubMllpServer(ack_code="AE")
    try:
        result = send_message("127.0.0.1", server.port, _corpus_text("oru_r01.hl7"), timeout=5)
        assert result.ok
        assert parse_ack(result.response).code == "AE"
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Client failure modes: friendly errors, never exceptions
# ---------------------------------------------------------------------------


def test_send_connection_refused_is_friendly():
    # Grab an ephemeral port and close it so nothing is listening.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    result = send_message("127.0.0.1", dead_port, "MSH|^~\\&|A|B", timeout=5)
    assert not result.ok
    assert "refused" in result.error.lower()
    assert "Traceback" not in result.error


def test_send_timeout_is_friendly():
    server = StubMllpServer(mode="silent")
    try:
        result = send_message("127.0.0.1", server.port, _corpus_text("adt_a01.hl7"), timeout=0.5)
        assert not result.ok
        assert "timed out" in result.error.lower()
    finally:
        server.close()


def test_send_peer_closes_without_response():
    server = StubMllpServer(mode="close_no_response")
    try:
        result = send_message("127.0.0.1", server.port, _corpus_text("adt_a01.hl7"), timeout=5)
        assert not result.ok
        assert "without sending a response" in result.error
    finally:
        server.close()


def test_send_peer_closes_mid_frame():
    server = StubMllpServer(mode="partial_frame")
    try:
        result = send_message("127.0.0.1", server.port, _corpus_text("adt_a01.hl7"), timeout=5)
        assert not result.ok
        assert "complete MLLP response" in result.error
    finally:
        server.close()
