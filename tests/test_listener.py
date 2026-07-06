"""Tests for the MLLP Listener (Phase 2): hl7kit.mllp.MllpListener,
app.capture.CaptureLog, and the /api/listener/* routes.

Every classification test drives a real TCP socket against a real
MllpListener bound to an ephemeral localhost port, and asserts the actual
capture.db row it produced -- not just "no exception". Ports are always
``0`` (OS-assigned) so tests never collide with each other or with a real
Listener that might be running elsewhere on the machine.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.capture import CaptureLog
from app.main import app
from hl7kit.ack import parse_ack
from hl7kit.mllp import END_BLOCK, START_BLOCK, MllpListener, frame, unframe

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
PROBES_DIR = CORPUS_DIR / "probes"


def _corpus_text(name: str) -> str:
    return (CORPUS_DIR / name).read_bytes().decode("utf-8")


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.02):
    """Poll *predicate* (a zero-arg callable) until it's truthy or
    *timeout* elapses, returning its final value. Connection handling
    happens on background threads, so tests can't assume the capture-log
    row exists the instant the socket call returns."""
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


def _last_event(log: CaptureLog):
    events = log.list_events(limit=1)
    return events[0] if events else None


def _wait_for_event(log: CaptureLog, timeout: float = 3.0) -> dict:
    """Wait for at least one row to land, return the most recent one."""
    event = _wait_until(lambda: _last_event(log), timeout=timeout)
    assert event is not None, "no capture-log event arrived in time"
    return event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def capture_log(tmp_path):
    log = CaptureLog(tmp_path / "test_capture.db")
    yield log
    log.close()


@pytest.fixture
def running_listener(capture_log):
    """A listener with generous timeouts, for the ordinary classification
    tests (fast tests don't want to wait on idle_timeout/max_bytes)."""
    listener = MllpListener(host="127.0.0.1", port=0, on_event=capture_log.record, idle_timeout=5.0)
    listener.start()
    yield listener, capture_log
    listener.stop()


def _connect(listener: MllpListener, timeout: float = 5.0) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", listener.actual_port), timeout=timeout)
    return sock


# ---------------------------------------------------------------------------
# Classification table (BUILD_PLAN section 5), one test per row
# ---------------------------------------------------------------------------


def test_hl7_message_parses_stores_and_acks_aa(running_listener):
    listener, log = running_listener
    text = _corpus_text("adt_a01.hl7")
    sock = _connect(listener)
    try:
        sock.sendall(frame(text.encode("utf-8")))
        response = sock.recv(65536)
    finally:
        sock.close()

    ack = parse_ack(unframe(response).decode("utf-8"))
    assert ack.code == "AA"
    assert ack.control_id == "CTRL0001"

    event = _wait_for_event(log)
    assert event["event_class"] == "HL7"
    assert event["ack_code"] == "AA"
    assert event["msh9"] == "ADT^A01"
    assert event["msh10"] == "CTRL0001"
    full = log.get_event(event["id"])
    assert full["full_message"] == text


def test_framed_msh_that_fails_to_resolve_gets_ae_ack(running_listener):
    """A payload that starts MSH but has no message type/control ID at all
    -- framed HL7-shaped traffic that isn't complete enough to ACK as
    accepted. Classified as HL7 (payload starts MSH), but AE not AA."""
    listener, log = running_listener
    payload = b"MSH|^~\\&|A|B"
    sock = _connect(listener)
    try:
        sock.sendall(frame(payload))
        response = sock.recv(65536)
    finally:
        sock.close()

    ack = parse_ack(unframe(response).decode("utf-8"))
    assert ack.code == "AE"

    event = _wait_for_event(log)
    assert event["event_class"] == "HL7"
    assert event["ack_code"] == "AE"


def test_framed_non_hl7_payload_logged_and_closed_politely(running_listener):
    listener, log = running_listener
    sock = _connect(listener)
    try:
        sock.sendall(frame(b"NOT AN HL7 MESSAGE AT ALL"))
        # No ACK is possible (no MSH to build one from); the peer just
        # gets a clean close, not a hang or a garbage response.
        response = sock.recv(65536)
        assert response == b""
    finally:
        sock.close()

    event = _wait_for_event(log)
    assert event["event_class"] == "NON_HL7_PAYLOAD"
    assert event["ack_code"] is None


def test_http_probe_classified_and_closed(running_listener):
    listener, log = running_listener
    http_bytes = (PROBES_DIR / "http_probe.txt").read_bytes()
    sock = _connect(listener)
    try:
        sock.sendall(http_bytes)
        assert sock.recv(65536) == b""
    finally:
        sock.close()

    event = _wait_for_event(log)
    assert event["event_class"] == "HTTP_PROBE"
    assert event["first_bytes_printable"].startswith("GET / HTTP/1.1")


def test_tls_probe_classified_and_closed(running_listener):
    listener, log = running_listener
    tls_bytes = (PROBES_DIR / "tls_probe.bin").read_bytes()
    sock = _connect(listener)
    try:
        sock.sendall(tls_bytes)
        assert sock.recv(65536) == b""
    finally:
        sock.close()

    event = _wait_for_event(log)
    assert event["event_class"] == "TLS_PROBE"
    assert event["first_bytes_hex"].startswith("16")


def test_scan_probe_connect_and_close_zero_bytes(running_listener):
    listener, log = running_listener
    sock = _connect(listener)
    sock.close()  # send nothing at all

    event = _wait_for_event(log)
    assert event["event_class"] == "SCAN_PROBE"
    assert event["first_bytes_hex"] == ""


def test_junk_bytes_classified(running_listener):
    listener, log = running_listener
    junk_bytes = (PROBES_DIR / "junk.bin").read_bytes()
    sock = _connect(listener)
    try:
        sock.sendall(junk_bytes)
        assert sock.recv(65536) == b""
    finally:
        sock.close()

    event = _wait_for_event(log)
    assert event["event_class"] == "JUNK"


def test_oversized_frame_classified_as_junk(capture_log):
    """A frame that never completes and blows past max_bytes -- use a tiny
    max_bytes so the test doesn't need to push a real megabyte."""
    listener = MllpListener(
        host="127.0.0.1", port=0, on_event=capture_log.record, idle_timeout=5.0, max_bytes=1000
    )
    listener.start()
    try:
        sock = _connect(listener)
        try:
            sock.sendall(START_BLOCK + b"MSH" + (b"X" * 5000))  # no END_BLOCK -- never completes
            assert sock.recv(65536) == b""
        finally:
            sock.close()
        event = _wait_for_event(capture_log)
        assert event["event_class"] == "JUNK"
    finally:
        listener.stop()


def test_idle_connection_times_out(capture_log):
    """A connection that opens and then sends nothing for longer than
    idle_timeout is logged TIMEOUT, not left hanging forever."""
    listener = MllpListener(host="127.0.0.1", port=0, on_event=capture_log.record, idle_timeout=0.3)
    listener.start()
    try:
        sock = _connect(listener)
        try:
            event = _wait_for_event(capture_log, timeout=3.0)
            assert event["event_class"] == "TIMEOUT"
        finally:
            sock.close()
    finally:
        listener.stop()


def test_idle_mid_frame_times_out(capture_log):
    """Partial MLLP frame, then silence past idle_timeout -- still TIMEOUT,
    not JUNK, and the partial bytes are what's captured."""
    listener = MllpListener(host="127.0.0.1", port=0, on_event=capture_log.record, idle_timeout=0.3)
    listener.start()
    try:
        sock = _connect(listener)
        try:
            sock.sendall(START_BLOCK + b"MSH|^~\\&|only the start")
            event = _wait_for_event(capture_log, timeout=3.0)
            assert event["event_class"] == "TIMEOUT"
            assert b"only the start" in bytes.fromhex(event["first_bytes_hex"])
        finally:
            sock.close()
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# Robustness: garbage floods and handler exceptions never kill the listener
# ---------------------------------------------------------------------------


def test_survives_garbage_flood_and_keeps_working(running_listener):
    listener, log = running_listener

    for i in range(30):
        sock = _connect(listener, timeout=2.0)
        try:
            if i % 3 == 0:
                sock.close()  # SCAN_PROBE
            elif i % 3 == 1:
                sock.sendall(bytes(range(256)) * 4)  # JUNK
                sock.close()
            else:
                sock.sendall(b"GET /nmap-style-probe HTTP/1.1\r\n\r\n")
                sock.close()
        except OSError:
            pass  # a probe closing the connection mid-write is fine too

    assert listener.is_running

    # The listener must still work normally afterward.
    text = _corpus_text("adt_a01.hl7")
    sock = _connect(listener)
    try:
        sock.sendall(frame(text.encode("utf-8")))
        response = sock.recv(65536)
    finally:
        sock.close()
    ack = parse_ack(unframe(response).decode("utf-8"))
    assert ack.code == "AA"
    assert listener.is_running


def test_broken_on_event_callback_does_not_kill_accept_loop(capture_log):
    def _raising_callback(event):
        raise RuntimeError("capture log is on fire")

    listener = MllpListener(host="127.0.0.1", port=0, on_event=_raising_callback, idle_timeout=5.0)
    listener.start()
    try:
        sock = _connect(listener)
        try:
            sock.sendall(frame(_corpus_text("adt_a01.hl7").encode("utf-8")))
            # Even though on_event blew up, the ACK still goes out --
            # the callback failure is isolated from the response path.
            response = sock.recv(65536)
        finally:
            sock.close()
        ack = parse_ack(unframe(response).decode("utf-8"))
        assert ack.code == "AA"
        assert listener.is_running

        # And the accept loop is still healthy: a second connection works.
        sock2 = _connect(listener)
        sock2.close()
        assert listener.is_running
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# start()/stop() lifecycle
# ---------------------------------------------------------------------------


def test_start_stop_idempotent_and_port_resolves(capture_log):
    listener = MllpListener(host="127.0.0.1", port=0, on_event=capture_log.record)
    assert not listener.is_running
    listener.start()
    listener.start()  # no-op, doesn't raise
    assert listener.is_running
    assert listener.actual_port != 0
    listener.stop()
    listener.stop()  # no-op, doesn't raise
    assert not listener.is_running


def test_start_raises_on_port_already_in_use(capture_log):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        listener = MllpListener(host="127.0.0.1", port=port, on_event=capture_log.record)
        with pytest.raises(OSError):
            listener.start()
        assert not listener.is_running
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# CaptureLog
# ---------------------------------------------------------------------------


def test_capture_log_counts_by_class(running_listener):
    listener, log = running_listener  # `log` is the same capture_log fixture instance
    sock = _connect(listener)
    sock.close()
    _wait_for_event(log)
    counts = log.counts_by_class()
    assert counts.get("SCAN_PROBE", 0) >= 1


def test_capture_log_get_event_unknown_id_returns_none(capture_log):
    assert capture_log.get_event(999999) is None


# ---------------------------------------------------------------------------
# API routes: /api/listener/*
# ---------------------------------------------------------------------------

client = TestClient(app)  # no `with` -- lifespan (auto-start) never fires


@pytest.fixture(autouse=True)
def _reset_app_listener():
    """Every /api/listener test shares app.main's module-level listener and
    capture_log. Make sure each test starts from a clean, stopped state and
    leaves one behind for the next test."""
    import app.main as main_module

    main_module.capture_log.clear()
    if main_module.listener.is_running:
        main_module.listener.stop()
    yield
    if main_module.listener.is_running:
        main_module.listener.stop()


def test_listener_status_when_stopped():
    resp = client.get("/api/listener/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert "port" in data
    assert data["counts"] == {}


def test_listener_start_stop_via_api():
    resp = client.post("/api/listener/start", json={"port": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    bound_port = data["port"]
    assert bound_port != 0

    # Starting again while running is a no-op, not an error.
    resp2 = client.post("/api/listener/start", json={"port": 0})
    assert resp2.json()["running"] is True

    resp3 = client.post("/api/listener/stop")
    assert resp3.status_code == 200
    assert resp3.json()["running"] is False


def test_listener_events_routes_reflect_real_traffic():
    start = client.post("/api/listener/start", json={"port": 0})
    port = start.json()["port"]

    import app.main as main_module

    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(frame(_corpus_text("adt_a01.hl7").encode("utf-8")))
        sock.recv(65536)
    finally:
        sock.close()

    event = _wait_for_event(main_module.capture_log)

    list_resp = client.get("/api/listener/events")
    assert list_resp.status_code == 200
    events = list_resp.json()["events"]
    assert len(events) == 1
    assert events[0]["event_class"] == "HL7"
    assert events[0]["has_message"] is True
    assert "full_message" not in events[0]  # list view omits the body

    detail_resp = client.get(f"/api/listener/events/{event['id']}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["event"]["full_message"].startswith("MSH|")
    seg_refs = [n["ref"] for n in detail["tree"]]
    assert "MSH" in seg_refs
    assert detail["summary"]["message_type"] == "ADT^A01"


def test_listener_event_detail_unknown_id_is_404():
    resp = client.get("/api/listener/events/999999")
    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["/listener"])
def test_listener_page_serves_html(path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "MSHroom" in resp.text


@pytest.mark.parametrize("asset", ["/static/listener.js"])
def test_listener_static_assets_serve(asset):
    assert client.get(asset).status_code == 200
