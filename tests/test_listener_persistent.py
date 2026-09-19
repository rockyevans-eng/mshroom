"""Tests for persistent MLLP connections (many messages on one socket).

Real interface engines open one connection and keep sending messages over
it. These tests pin down that behavior with real TCP sockets against a real
MllpListener on an ephemeral localhost port, plus a lock ensuring that the
hostile-traffic classification (TLS/HTTP/junk/scan probes) still works
while a legitimate session is in progress.

Events are collected in a plain list (``on_event=events.append``) rather
than the SQLite capture log: these tests care about *which events the
Listener emits*, not about how they are stored. Idle timeouts are small so
the whole file stays fast.
"""

from __future__ import annotations

import socket
import time
import weakref
from collections import Counter

import pytest

from hl7kit.ack import parse_ack
from hl7kit.mllp import (
    END_BLOCK,
    EVENT_HL7,
    EVENT_HTTP_PROBE,
    EVENT_JUNK,
    EVENT_NON_HL7_PAYLOAD,
    EVENT_SCAN_PROBE,
    EVENT_TIMEOUT,
    EVENT_TLS_PROBE,
    START_BLOCK,
    MllpListener,
    extract_frames,
    frame,
    unframe,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hl7(n: int) -> bytes:
    """A small, obviously fictional HL7 message with control ID CTRL<n>."""
    return (
        f"MSH|^~\\&|TESTSENDER|TESTFAC|TESTRECV|TESTFAC|20260101000000||ADT^A01|CTRL{n:04d}|P|2.5.1\r"
        "PID|1||MSHROOM-TEST-0001||FICTIONAL^TESTPATIENT\r"
    ).encode("utf-8")


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.01):
    """Poll *predicate* until truthy or *timeout* elapses; return its last
    value. Handler threads run in the background, so events land a moment
    after the socket call that caused them."""
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


# Bytes already received from a socket but not yet consumed as an ACK. The
# server may send several ACKs back-to-back, and one recv() can return more
# than one of them, so leftovers must carry over to the next _read_ack call.
_pending_ack_bytes: "weakref.WeakKeyDictionary[socket.socket, bytes]" = weakref.WeakKeyDictionary()


def _read_ack(sock: socket.socket) -> str:
    """Return the text of the next complete MLLP frame received on *sock*."""
    buffer = _pending_ack_bytes.get(sock, b"")
    while True:
        payloads, _ = extract_frames(buffer)
        if payloads:
            # Keep everything after the first frame for the next call.
            first = payloads[0]
            _pending_ack_bytes[sock] = buffer[len(frame(first)) :]
            return first.decode("utf-8")
        chunk = sock.recv(65536)
        assert chunk, "connection closed before a complete ACK arrived"
        buffer += chunk


def _classes(events) -> list[str]:
    return [e.event_class for e in list(events)]


def _assert_closed_by_server(sock: socket.socket) -> None:
    """The server closed its end: recv() sees EOF (or a reset, which is
    what Windows reports if the close raced unread data)."""
    try:
        assert sock.recv(65536) == b""
    except ConnectionResetError:
        pass


@pytest.fixture
def make_listener():
    """Factory: ``listener, events = make_listener(idle_timeout=..., ...)``.
    Every listener made is stopped at teardown."""
    made: list[MllpListener] = []

    def _make(idle_timeout: float = 5.0, max_bytes: int = 1_048_576):
        events: list = []
        listener = MllpListener(
            host="127.0.0.1",
            port=0,
            on_event=events.append,
            idle_timeout=idle_timeout,
            max_bytes=max_bytes,
        )
        listener.start()
        made.append(listener)
        return listener, events

    yield _make
    for listener in made:
        listener.stop()


def _connect(listener: MllpListener) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", listener.actual_port), timeout=5.0)
    # Send small writes immediately instead of letting the OS batch them;
    # the fragmentation tests want the bytes to travel separately.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


# ---------------------------------------------------------------------------
# extract_frames: the pure helper
# ---------------------------------------------------------------------------


def test_extract_frames_empty_buffer():
    assert extract_frames(b"") == ([], b"")


def test_extract_frames_one_complete_frame():
    assert extract_frames(frame(b"MSH|a")) == ([b"MSH|a"], b"")


def test_extract_frames_several_frames():
    buffer = frame(b"ONE") + frame(b"TWO") + frame(b"THREE")
    assert extract_frames(buffer) == ([b"ONE", b"TWO", b"THREE"], b"")


def test_extract_frames_keeps_partial_trailing_frame():
    payloads, remainder = extract_frames(frame(b"ONE") + START_BLOCK + b"TW")
    assert payloads == [b"ONE"]
    assert remainder == START_BLOCK + b"TW"


def test_extract_frames_lone_end_byte_is_still_partial():
    """A buffer ending in 0x1C (without its 0x0D yet) is an unfinished frame."""
    payloads, remainder = extract_frames(START_BLOCK + b"ONE" + b"\x1c")
    assert payloads == []
    assert remainder == START_BLOCK + b"ONE" + b"\x1c"


def test_extract_frames_skips_cr_lf_between_frames():
    buffer = frame(b"ONE") + b"\r\n\n" + frame(b"TWO") + b"\r"
    assert extract_frames(buffer) == ([b"ONE", b"TWO"], b"")


def test_extract_frames_returns_junk_as_remainder():
    payloads, remainder = extract_frames(frame(b"ONE") + b"junk" + frame(b"TWO"))
    assert payloads == [b"ONE"]
    assert remainder == b"junk" + frame(b"TWO")


def test_unframe_is_unchanged_by_extract_frames():
    """unframe() still returns only the first frame and tolerates leading junk."""
    assert unframe(b"junk" + frame(b"ONE") + frame(b"TWO")) == b"ONE"


# ---------------------------------------------------------------------------
# Many messages on one connection
# ---------------------------------------------------------------------------


def test_back_to_back_frames_in_one_send(make_listener):
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(b"".join(frame(_hl7(n)) for n in range(1, 4)))
        acks = [parse_ack(_read_ack(sock)) for _ in range(3)]
    finally:
        sock.close()

    assert [a.code for a in acks] == ["AA", "AA", "AA"]
    assert [a.control_id for a in acks] == ["CTRL0001", "CTRL0002", "CTRL0003"]
    assert _wait_until(lambda: len(events) == 3)
    assert _classes(events) == [EVENT_HL7] * 3
    assert [e.msh10 for e in events] == ["CTRL0001", "CTRL0002", "CTRL0003"]


def test_100_frames_sequentially_on_one_connection(make_listener):
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        for n in range(1, 101):
            sock.sendall(frame(_hl7(n)))
            ack = parse_ack(_read_ack(sock))
            assert ack.code == "AA"
            assert ack.control_id == f"CTRL{n:04d}"
    finally:
        sock.close()

    assert _wait_until(lambda: len(events) == 100)
    assert _classes(events) == [EVENT_HL7] * 100
    assert [e.msh10 for e in events] == [f"CTRL{n:04d}" for n in range(1, 101)]


def test_frame_sent_one_byte_at_a_time(make_listener):
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        for byte in frame(_hl7(7)):
            sock.sendall(bytes([byte]))
        ack = parse_ack(_read_ack(sock))
    finally:
        sock.close()

    assert ack.code == "AA"
    assert ack.control_id == "CTRL0007"
    assert _wait_until(lambda: len(events) == 1)
    assert events[0].full_message == _hl7(7).decode("utf-8")


def test_frame_split_across_end_marker(make_listener):
    """0x1C and 0x0D arrive in different network reads."""
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(START_BLOCK + _hl7(1) + END_BLOCK[:1])  # ... 0x1C
        time.sleep(0.1)  # let the server read the partial marker on its own
        sock.sendall(END_BLOCK[1:])  # 0x0D
        ack = parse_ack(_read_ack(sock))
    finally:
        sock.close()

    assert ack.code == "AA"
    assert _wait_until(lambda: len(events) == 1)
    assert _classes(events) == [EVENT_HL7]


def test_second_frame_arrives_in_a_later_chunk(make_listener):
    """Frame 1 complete plus the first half of frame 2; the rest follows later."""
    listener, events = make_listener()
    second = frame(_hl7(2))
    half = len(second) // 2
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)) + second[:half])
        first_ack = parse_ack(_read_ack(sock))
        time.sleep(0.1)
        sock.sendall(second[half:])
        second_ack = parse_ack(_read_ack(sock))
    finally:
        sock.close()

    assert (first_ack.control_id, second_ack.control_id) == ("CTRL0001", "CTRL0002")
    assert _wait_until(lambda: len(events) == 2)
    assert _classes(events) == [EVENT_HL7, EVENT_HL7]


def test_cr_lf_between_frames_is_tolerated(make_listener):
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)) + b"\r\n" + frame(_hl7(2)) + b"\n\r")
        acks = [parse_ack(_read_ack(sock)) for _ in range(2)]
        # Filler at the very end of the buffer must not upset the next read.
        sock.sendall(frame(_hl7(3)))
        acks.append(parse_ack(_read_ack(sock)))
    finally:
        sock.close()

    assert [a.control_id for a in acks] == ["CTRL0001", "CTRL0002", "CTRL0003"]
    assert _wait_until(lambda: len(events) == 3)
    assert _classes(events) == [EVENT_HL7] * 3


def test_ae_message_gets_ae_and_connection_stays_usable(make_listener):
    """Starts MSH but has no MSH-9/MSH-10 -> AE; the next frame is still served."""
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(frame(b"MSH|^~\\&|A|B"))
        assert parse_ack(_read_ack(sock)).code == "AE"
        sock.sendall(frame(_hl7(2)))
        good = parse_ack(_read_ack(sock))
    finally:
        sock.close()

    assert good.code == "AA"
    assert _wait_until(lambda: len(events) == 2)
    assert [e.ack_code for e in events] == ["AE", "AA"]
    assert _classes(events) == [EVENT_HL7, EVENT_HL7]


def test_each_event_holds_only_its_own_frame(make_listener):
    """With frames batched in one read, every event's raw bytes are its own frame."""
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)) + frame(_hl7(2)))
        _read_ack(sock)
        _read_ack(sock)
    finally:
        sock.close()

    assert _wait_until(lambda: len(events) == 2)
    assert events[0].first_bytes == frame(_hl7(1))[:256]
    assert events[1].first_bytes == frame(_hl7(2))[:256]


# ---------------------------------------------------------------------------
# How a persistent session ends
# ---------------------------------------------------------------------------


def test_clean_close_after_messages_emits_no_extra_event(make_listener):
    listener, events = make_listener(idle_timeout=0.3)
    sock = _connect(listener)
    try:
        for n in range(1, 4):
            sock.sendall(frame(_hl7(n)))
            _read_ack(sock)
    finally:
        sock.close()

    assert _wait_until(lambda: len(events) == 3)
    time.sleep(0.6)  # long enough for any spurious SCAN_PROBE/JUNK/TIMEOUT to show up
    assert _classes(events) == [EVENT_HL7] * 3


def test_idle_between_messages_closes_quietly(make_listener):
    """Idle past the timeout with nothing buffered: server closes, no TIMEOUT event."""
    listener, events = make_listener(idle_timeout=0.3)
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)))
        _read_ack(sock)
        time.sleep(0.8)
        _assert_closed_by_server(sock)
    finally:
        sock.close()

    assert _classes(events) == [EVENT_HL7]


def test_idle_timeout_restarts_after_every_frame(make_listener):
    """Total session time exceeds idle_timeout, but no single gap does."""
    listener, events = make_listener(idle_timeout=0.5)
    sock = _connect(listener)
    try:
        for n in range(1, 7):
            sock.sendall(frame(_hl7(n)))
            assert parse_ack(_read_ack(sock)).code == "AA"
            time.sleep(0.2)  # 6 * 0.2s = 1.2s total, well past 0.5s
    finally:
        sock.close()

    assert _wait_until(lambda: len(events) == 6)
    assert _classes(events) == [EVENT_HL7] * 6


def test_partial_frame_then_idle_is_timeout(make_listener):
    listener, events = make_listener(idle_timeout=0.3)
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)))
        _read_ack(sock)
        sock.sendall(START_BLOCK + b"MSH|^~\\&|only the start")
        assert _wait_until(lambda: len(events) == 2, timeout=3.0)
    finally:
        sock.close()

    assert _classes(events) == [EVENT_HL7, EVENT_TIMEOUT]
    assert b"only the start" in events[1].first_bytes


def test_close_mid_frame_is_junk(make_listener):
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)))
        _read_ack(sock)
        sock.sendall(START_BLOCK + b"MSH|^~\\&|never finished")
    finally:
        sock.close()

    assert _wait_until(lambda: len(events) == 2)
    assert _classes(events) == [EVENT_HL7, EVENT_JUNK]


def test_oversize_incomplete_frame_after_a_good_one_is_junk(make_listener):
    listener, events = make_listener(max_bytes=1000)
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)))
        _read_ack(sock)
        sock.sendall(START_BLOCK + b"MSH" + b"X" * 5000)  # never completes
        assert _wait_until(lambda: len(events) == 2)
        _assert_closed_by_server(sock)
    finally:
        sock.close()

    assert _classes(events) == [EVENT_HL7, EVENT_JUNK]


def test_junk_mid_stream_is_junk_event_and_closes(make_listener):
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)))
        assert parse_ack(_read_ack(sock)).code == "AA"
        sock.sendall(b"this is not MLLP")
        assert _wait_until(lambda: len(events) == 2)
        _assert_closed_by_server(sock)
    finally:
        sock.close()

    # The earlier message is unaffected; the junk is reported with its bytes.
    assert _classes(events) == [EVENT_HL7, EVENT_JUNK]
    assert events[0].ack_code == "AA"
    assert events[1].first_bytes == b"this is not MLLP"


def test_junk_between_frames_in_one_read_serves_earlier_frames(make_listener):
    """Frame, junk, frame in a single send: the first is served, the rest is not."""
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)) + b"junk" + frame(_hl7(2)))
        assert parse_ack(_read_ack(sock)).control_id == "CTRL0001"
        assert _wait_until(lambda: len(events) == 2)
        _assert_closed_by_server(sock)
    finally:
        sock.close()

    assert _classes(events) == [EVENT_HL7, EVENT_JUNK]


def test_non_hl7_payload_after_good_frame_closes(make_listener):
    listener, events = make_listener()
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)) + frame(b"NOT HL7") + frame(_hl7(3)))
        assert parse_ack(_read_ack(sock)).control_id == "CTRL0001"
        assert _wait_until(lambda: len(events) == 2)
        _assert_closed_by_server(sock)
    finally:
        sock.close()

    # The frame after the non-HL7 one is never processed: the connection closed.
    assert _classes(events) == [EVENT_HL7, EVENT_NON_HL7_PAYLOAD]


def test_stop_closes_an_idle_persistent_connection_promptly(make_listener):
    listener, events = make_listener(idle_timeout=30.0)
    sock = _connect(listener)
    try:
        sock.sendall(frame(_hl7(1)))
        _read_ack(sock)
        started = time.monotonic()
        listener.stop()
        assert time.monotonic() - started < 10.0
        assert _wait_until(lambda: not listener._handlers)
        assert not listener._conns
        _assert_closed_by_server(sock)
    finally:
        sock.close()

    assert _classes(events) == [EVENT_HL7]  # stop() is not an event


# ---------------------------------------------------------------------------
# Regression lock: hostile traffic never disturbs a legitimate session
# ---------------------------------------------------------------------------


def test_probe_storm_while_real_client_is_mid_frame(make_listener):
    """A real client is halfway through a frame while HTTP, TLS, junk and
    connect-then-close probes hit the same port. The real client must still
    get its AA ACK, every probe must be recorded as its own class, and
    nothing but the real message may be classified as HL7."""
    listener, events = make_listener()
    message = frame(_hl7(1))
    half = len(message) // 2

    real = _connect(listener)
    try:
        real.sendall(message[:half])
        time.sleep(0.1)  # let the server start buffering the half frame

        rounds = 5
        for _ in range(rounds):
            for probe in (
                b"GET /probe HTTP/1.1\r\nHost: localhost\r\n\r\n",
                b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03",
                b"\xff\xfe\xfd not mllp at all",
            ):
                p = _connect(listener)
                p.sendall(probe)
                p.close()
            _connect(listener).close()  # connect, send nothing, close

        expected_probes = rounds * 4
        assert _wait_until(lambda: len(events) == expected_probes, timeout=5.0)

        # The real client finishes its frame and is served normally.
        real.sendall(message[half:])
        assert parse_ack(_read_ack(real)).code == "AA"

        # ...and its session is still usable for another message.
        real.sendall(frame(_hl7(2)))
        assert parse_ack(_read_ack(real)).control_id == "CTRL0002"
    finally:
        real.close()

    assert _wait_until(lambda: len(events) == expected_probes + 2, timeout=5.0)
    counts = Counter(_classes(events))
    assert counts[EVENT_HTTP_PROBE] == rounds
    assert counts[EVENT_TLS_PROBE] == rounds
    assert counts[EVENT_JUNK] == rounds
    assert counts[EVENT_SCAN_PROBE] == rounds
    # Only the real client's two messages are HL7; no probe was parsed as one.
    assert counts[EVENT_HL7] == 2
    assert {e.msh10 for e in events if e.event_class == EVENT_HL7} == {"CTRL0001", "CTRL0002"}
    assert all(e.full_message is None for e in events if e.event_class != EVENT_HL7)
