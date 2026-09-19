"""Tests for the Sender's "Keep connection open" mode.

Covers three layers, all with real TCP sockets on 127.0.0.1 (no mocks):

* :class:`hl7kit.mllp.MllpConnection` -- one persistent client socket:
  reuse, transparent reconnect, pipelined-byte keeping, friendly errors.
* :class:`app.send_pool.SendPool` -- the bounded registry of such sockets:
  cap, idle expiry, explicit close, shutdown.
* ``POST /api/send`` (``keep_open``) and ``POST /api/send/close`` end to end.

Two kinds of receiver are used. The real :class:`hl7kit.mllp.MllpListener`
is used when the test only needs a well-behaved persistent engine (its
events give us ``peer_port``, which proves how many sockets were used). The
tiny :class:`StubEngine` below is used when a test needs a *misbehaving*
peer: one that closes connections, refuses reconnects, or pipelines extra
frames.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.send_pool import SendPool
from hl7kit.ack import build_ack, parse_ack
from hl7kit.mllp import (
    EVENT_HL7,
    MllpConnection,
    MllpListener,
    _split_first_frame,
    extract_frames,
    frame,
    send_message,
)
from hl7kit.parser import parse_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hl7(n: int) -> str:
    """A small, obviously fictional HL7 message with control ID CTRL<n>."""
    return (
        f"MSH|^~\\&|TESTSENDER|TESTFAC|TESTRECV|TESTFAC|20260101000000||ADT^A01|CTRL{n:04d}|P|2.5.1\r"
        "PID|1||MSHROOM-TEST-0001||FICTIONAL^TESTPATIENT\r"
    )


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.01):
    """Poll *predicate* until truthy or *timeout* elapses; return its last value.
    Server threads run in the background, so their effects land a moment
    after the client call that caused them."""
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


def _hl7_events(events: list) -> list:
    return [e for e in list(events) if e.event_class == EVENT_HL7]


@pytest.fixture
def dead_port():
    """A localhost port that reliably *refuses* connections.

    A socket that is bound but never listens makes connects fail fast with
    "refused", and holding it open stops the OS handing the same port to
    our own outgoing socket. (Merely closing a probe socket leaves the port
    free, and on Windows a connect to a free port can stall until timeout
    instead of being refused.)
    """
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # NOTE: Windows takes ~2s to report "refused", so callers use timeout>=5.
    blocker.bind(("127.0.0.1", 0))
    yield blocker.getsockname()[1]
    blocker.close()


@pytest.fixture
def listener_and_events():
    """A real persistent MllpListener on an ephemeral port plus its event list."""
    events: list = []
    listener = MllpListener(host="127.0.0.1", port=0, on_event=events.append, idle_timeout=5.0)
    listener.start()
    yield listener, events
    listener.stop()


class StubEngine:
    """A configurable, misbehaving MLLP receiver.

    Unlike ``MllpListener`` it is *scriptable*: it ACKs every frame it
    receives (AA) but can also

    * ``close_after`` -- close the connection after ACKing that many
      messages on it (an engine dropping an idle/finished connection);
    * ``refuse_after_first`` -- stop listening as soon as the first
      connection is accepted, so any reconnect is refused;
    * ``extra_after_first`` -- write an extra frame right behind the very
      first ACK (an engine pipelining more than one frame).

    ``connections`` counts accepted sockets, ``messages`` records every
    payload received, and ``peer_closed`` counts connections the *client*
    closed (recv returned EOF).
    """

    EXTRA = b"MSA|AA|EXTRA-FRAME"

    def __init__(
        self,
        close_after: Optional[int] = None,
        refuse_after_first: bool = False,
        extra_after_first: bool = False,
    ) -> None:
        self.close_after = close_after
        self.refuse_after_first = refuse_after_first
        self.extra_after_first = extra_after_first
        self.connections = 0
        self.messages: list[bytes] = []
        self.peer_closed = 0
        self._lock = threading.Lock()
        self._stopped = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while not self._stopped:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.connections += 1
            if self.refuse_after_first:
                # Closed here, in the accept thread itself, before the
                # connection is served: the port is deterministically dead by
                # the time the client can possibly try to reconnect.
                self._sock.close()
                self._stopped = True
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        handled = 0
        buffer = b""
        conn.settimeout(5)
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    with self._lock:
                        self.peer_closed += 1
                    return
                buffer += chunk
                payloads, buffer = extract_frames(buffer)
                for payload in payloads:
                    with self._lock:
                        self.messages.append(payload)
                        first_ever = len(self.messages) == 1
                    handled += 1
                    ack = build_ack(parse_message(payload.decode("utf-8")), "AA")
                    data = frame(ack.encode("utf-8"))
                    if self.extra_after_first and first_ever:
                        data += frame(self.EXTRA)
                    conn.sendall(data)
                    if self.close_after is not None and handled >= self.close_after:
                        return
        except OSError:
            with self._lock:
                self.peer_closed += 1
        finally:
            conn.close()

    def close(self) -> None:
        self._stopped = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def make_engine():
    """Factory for StubEngine; every engine made is shut down at teardown."""
    made: list[StubEngine] = []

    def _make(**kwargs) -> StubEngine:
        engine = StubEngine(**kwargs)
        made.append(engine)
        return engine

    yield _make
    for engine in made:
        engine.close()


# ---------------------------------------------------------------------------
# MllpConnection: reuse
# ---------------------------------------------------------------------------


def test_connection_reuses_one_socket_for_many_sends(listener_and_events):
    """N messages, ONE connection: the server sees a single peer port."""
    listener, events = listener_and_events
    with MllpConnection("127.0.0.1", listener.actual_port) as conn:
        results = [conn.send(_hl7(n), timeout=5) for n in range(1, 4)]

    assert all(r.ok for r in results), [r.error for r in results]
    assert [r.reused for r in results] == [False, True, True]
    # Each ACK answers its own message (no off-by-one between sends).
    assert [parse_ack(r.response).control_id for r in results] == ["CTRL0001", "CTRL0002", "CTRL0003"]
    assert _wait_until(lambda: len(_hl7_events(events)) == 3)
    assert len({e.peer_port for e in _hl7_events(events)}) == 1


def test_send_message_still_opens_a_new_connection_each_time(listener_and_events):
    """The one-shot function is unchanged: a fresh socket per message."""
    listener, events = listener_and_events
    for n in (1, 2):
        result = send_message("127.0.0.1", listener.actual_port, _hl7(n), timeout=5)
        assert result.ok, result.error
        assert result.reused is False
    assert _wait_until(lambda: len(_hl7_events(events)) == 2)
    assert len({e.peer_port for e in _hl7_events(events)}) == 2


def test_close_then_send_opens_a_new_connection(listener_and_events):
    listener, events = listener_and_events
    conn = MllpConnection("127.0.0.1", listener.actual_port)
    assert conn.send(_hl7(1), timeout=5).ok
    conn.close()
    assert not conn.is_open
    again = conn.send(_hl7(2), timeout=5)
    conn.close()
    assert again.ok and again.reused is False
    assert _wait_until(lambda: len(_hl7_events(events)) == 2)
    assert len({e.peer_port for e in _hl7_events(events)}) == 2


def test_context_manager_closes_the_socket(make_engine):
    engine = make_engine()
    with MllpConnection("127.0.0.1", engine.port) as conn:
        assert conn.send(_hl7(1), timeout=5).ok
        assert conn.is_open
    assert not conn.is_open
    # The server sees the client close its end.
    assert _wait_until(lambda: engine.peer_closed == 1)


def test_connect_explicitly_and_report_refusal_without_raising(dead_port):
    conn = MllpConnection("127.0.0.1", dead_port)
    result = conn.connect(timeout=5)
    assert not result.ok
    assert "refused" in result.error.lower()
    assert not conn.is_open


# ---------------------------------------------------------------------------
# MllpConnection: peer closes between sends
# ---------------------------------------------------------------------------


def test_peer_close_between_sends_reconnects_transparently(make_engine):
    """The engine drops the connection after each message. The second send
    must notice, reconnect once, and still succeed."""
    engine = make_engine(close_after=1)
    with MllpConnection("127.0.0.1", engine.port) as conn:
        first = conn.send(_hl7(1), timeout=5)
        assert first.ok and first.reused is False
        # Deterministic: wait until the FIN has actually reached our socket.
        assert _wait_until(conn.peer_has_closed)

        second = conn.send(_hl7(2), timeout=5)

    assert second.ok, second.error
    assert second.reused is False  # it travelled on a NEW connection
    assert parse_ack(second.response).control_id == "CTRL0002"
    assert engine.connections == 2
    assert len(engine.messages) == 2  # delivered exactly once each


def test_reconnect_refused_gives_clear_error_and_no_exception(make_engine):
    """Peer closed the connection AND won't accept a new one: a friendly
    error naming both facts, never an exception, socket left closed."""
    engine = make_engine(close_after=1, refuse_after_first=True)
    with MllpConnection("127.0.0.1", engine.port) as conn:
        assert conn.send(_hl7(1), timeout=5).ok
        assert _wait_until(conn.peer_has_closed)

        result = conn.send(_hl7(2), timeout=5)

        assert not result.ok
        assert "closed by the peer" in result.error
        assert "reconnecting failed" in result.error
        assert "refused" in result.error.lower()
        assert "Traceback" not in result.error
        assert not conn.is_open
    assert len(engine.messages) == 1  # the second message never arrived


def test_timeout_poisons_the_socket_so_a_late_ack_cannot_desync(make_engine):
    """After a timeout the connection is dropped; the next send starts on a
    fresh socket instead of reading a stale reply."""
    silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)  # accepts at the OS level but never replies
    try:
        conn = MllpConnection("127.0.0.1", silent.getsockname()[1])
        result = conn.send(_hl7(1), timeout=0.3)
        assert not result.ok
        assert "timed out" in result.error.lower()
        assert not conn.is_open
    finally:
        silent.close()


# ---------------------------------------------------------------------------
# MllpConnection: pipelined bytes
# ---------------------------------------------------------------------------


def test_split_first_frame_keeps_everything_after_the_first_frame():
    """Pure helper: first payload out, later frames and the partial tail kept."""
    buffer = frame(b"ONE") + frame(b"TWO") + b"\x0bPART"
    first, rest = _split_first_frame(buffer)  # type: ignore[misc]
    assert first == b"ONE"
    assert extract_frames(rest) == ([b"TWO"], b"\x0bPART")
    assert _split_first_frame(b"\x0bincomplete") is None


def test_pipelined_extra_frame_is_kept_for_the_next_send(make_engine):
    """The engine writes ACK1 and an extra frame back to back. The extra
    frame must not be lost: the NEXT send consumes it (then the real ACK for
    message 2 answers the send after that)."""
    engine = make_engine(extra_after_first=True)
    with MllpConnection("127.0.0.1", engine.port) as conn:
        first = conn.send(_hl7(1), timeout=5)
        second = conn.send(_hl7(2), timeout=5)
        third = conn.send(_hl7(3), timeout=5)

    assert first.ok and parse_ack(first.response).control_id == "CTRL0001"
    assert second.ok and second.response.encode("utf-8") == StubEngine.EXTRA
    assert second.reused is True
    assert third.ok and parse_ack(third.response).control_id == "CTRL0002"
    assert engine.connections == 1


# ---------------------------------------------------------------------------
# SendPool
# ---------------------------------------------------------------------------


def test_pool_reuses_connection_per_destination(listener_and_events):
    listener, events = listener_and_events
    pool = SendPool()
    try:
        results = [pool.send("127.0.0.1", listener.actual_port, _hl7(n), timeout=5) for n in range(1, 4)]
        assert all(r.ok for r in results)
        assert [r.reused for r in results] == [False, True, True]
        assert len(pool) == 1
        assert _wait_until(lambda: len(_hl7_events(events)) == 3)
        assert len({e.peer_port for e in _hl7_events(events)}) == 1
    finally:
        pool.shutdown()


def test_pool_registry_is_capped_and_evicts_least_recently_used(make_engine):
    engines = [make_engine() for _ in range(3)]
    pool = SendPool(max_connections=2)
    try:
        for engine in engines:
            assert pool.send("127.0.0.1", engine.port, _hl7(1), timeout=5).ok
        assert len(pool) == 2
        assert ("127.0.0.1", engines[0].port) not in pool.keys()  # oldest evicted
        # The evicted connection was really closed, not just forgotten.
        assert _wait_until(lambda: engines[0].peer_closed == 1)
        # Sending to it again simply opens a new connection.
        again = pool.send("127.0.0.1", engines[0].port, _hl7(2), timeout=5)
        assert again.ok and again.reused is False
        assert len(pool) == 2
    finally:
        pool.shutdown()


def test_pool_closes_idle_connections_lazily(make_engine):
    """Idle expiry is checked on the next call; a fake clock keeps it instant."""
    now = [1000.0]
    engine = make_engine()
    pool = SendPool(idle_limit=60.0, clock=lambda: now[0])
    try:
        assert pool.send("127.0.0.1", engine.port, _hl7(1), timeout=5).reused is False
        now[0] += 30
        assert pool.send("127.0.0.1", engine.port, _hl7(2), timeout=5).reused is True
        now[0] += 61  # idle past the limit
        third = pool.send("127.0.0.1", engine.port, _hl7(3), timeout=5)
        assert third.ok and third.reused is False
        assert _wait_until(lambda: engine.peer_closed == 1)  # old socket closed
        assert engine.connections == 2
    finally:
        pool.shutdown()


def test_pool_reaper_thread_closes_idle_connections_without_a_new_call(make_engine):
    engine = make_engine()
    pool = SendPool(idle_limit=0.2)
    try:
        assert pool.send("127.0.0.1", engine.port, _hl7(1), timeout=5).ok
        assert len(pool) == 1
        assert _wait_until(lambda: len(pool) == 0, timeout=5)
        assert _wait_until(lambda: engine.peer_closed == 1)
    finally:
        pool.shutdown()


def test_pool_failed_send_drops_the_entry(dead_port):
    pool = SendPool()
    try:
        result = pool.send("127.0.0.1", dead_port, _hl7(1), timeout=5)
        assert not result.ok and "refused" in result.error.lower()
        assert len(pool) == 0
    finally:
        pool.shutdown()


def test_pool_close_one_and_close_all(make_engine):
    a, b = make_engine(), make_engine()
    pool = SendPool()
    try:
        pool.send("127.0.0.1", a.port, _hl7(1), timeout=5)
        pool.send("127.0.0.1", b.port, _hl7(1), timeout=5)
        assert pool.close("127.0.0.1", a.port) == 1
        assert pool.close("127.0.0.1", a.port) == 0  # already gone
        assert _wait_until(lambda: a.peer_closed == 1)
        assert pool.close_all() == 1
        assert _wait_until(lambda: b.peer_closed == 1)
        assert len(pool) == 0
    finally:
        pool.shutdown()


def test_pool_shutdown_closes_everything(make_engine):
    engines = [make_engine() for _ in range(3)]
    pool = SendPool()
    for engine in engines:
        assert pool.send("127.0.0.1", engine.port, _hl7(1), timeout=5).ok
    assert pool.shutdown() == 3
    assert len(pool) == 0
    for engine in engines:
        assert _wait_until(lambda e=engine: e.peer_closed == 1)


# ---------------------------------------------------------------------------
# /api/send (keep_open) and /api/send/close
# ---------------------------------------------------------------------------


@pytest.fixture
def api():
    """TestClient with the app's send pool emptied before and after."""
    app_main.send_pool.close_all()
    client = TestClient(app_main.app)
    yield client
    app_main.send_pool.close_all()


def _post_send(client: TestClient, port: int, n: int, **extra):
    return client.post(
        "/api/send",
        json={"host": "127.0.0.1", "port": port, "message": _hl7(n), "timeout": 5, **extra},
    )


def test_api_keep_open_reuses_one_connection(api, listener_and_events):
    listener, events = listener_and_events
    responses = [_post_send(api, listener.actual_port, n, keep_open=True).json() for n in (1, 2, 3)]
    assert [r["ok"] for r in responses] == [True, True, True]
    assert [r["reused"] for r in responses] == [False, True, True]
    assert [r["ack"]["control_id"] for r in responses] == ["CTRL0001", "CTRL0002", "CTRL0003"]
    assert _wait_until(lambda: len(_hl7_events(events)) == 3)
    assert len({e.peer_port for e in _hl7_events(events)}) == 1


def test_api_default_is_a_new_connection_per_message(api, listener_and_events):
    """keep_open omitted / false: today's behavior, nothing is kept."""
    listener, events = listener_and_events
    for extra in ({}, {"keep_open": False}):
        data = _post_send(api, listener.actual_port, 1, **extra).json()
        assert data["ok"] is True
        assert data["reused"] is False
    assert len(app_main.send_pool) == 0
    assert _wait_until(lambda: len(_hl7_events(events)) == 2)
    assert len({e.peer_port for e in _hl7_events(events)}) == 2


def test_api_keep_open_error_is_friendly_json(api, dead_port):
    data = _post_send(api, dead_port, 1, keep_open=True).json()
    assert data["ok"] is False
    assert "refused" in data["error"].lower()
    assert data["reused"] is False
    assert len(app_main.send_pool) == 0


def test_api_send_close_one_destination(api, make_engine):
    engine = make_engine()
    assert _post_send(api, engine.port, 1, keep_open=True).json()["ok"]
    resp = api.post("/api/send/close", json={"host": "127.0.0.1", "port": engine.port})
    assert resp.status_code == 200
    assert resp.json() == {"closed": 1}
    assert _wait_until(lambda: engine.peer_closed == 1)
    # Nothing left to close, and the next send opens a fresh connection.
    assert api.post("/api/send/close", json={"host": "127.0.0.1", "port": engine.port}).json() == {"closed": 0}
    assert _post_send(api, engine.port, 2, keep_open=True).json()["reused"] is False


def test_api_send_close_all_with_empty_body(api, make_engine):
    a, b = make_engine(), make_engine()
    for engine in (a, b):
        assert _post_send(api, engine.port, 1, keep_open=True).json()["ok"]
    assert api.post("/api/send/close").json() == {"closed": 2}
    assert api.post("/api/send/close", json={}).json() == {"closed": 0}
    assert _wait_until(lambda: a.peer_closed == 1 and b.peer_closed == 1)


def test_api_send_close_rejects_half_a_destination(api):
    assert api.post("/api/send/close", json={"host": "127.0.0.1"}).status_code == 422
    assert api.post("/api/send/close", json={"port": 6661}).status_code == 422


def test_app_shutdown_closes_all_kept_open_connections(make_engine, monkeypatch):
    """Entering and leaving the app's lifespan (uvicorn start/stop) must
    close every kept-open Sender connection."""
    # Keep the lifespan's own listener off the default port.
    monkeypatch.setattr(
        app_main, "listener", MllpListener(host="127.0.0.1", port=0, on_event=app_main._on_listener_event)
    )
    a, b = make_engine(), make_engine()
    with TestClient(app_main.app) as client:
        for engine in (a, b):
            assert _post_send(client, engine.port, 1, keep_open=True).json()["ok"]
        assert len(app_main.send_pool) == 2
    assert len(app_main.send_pool) == 0
    assert _wait_until(lambda: a.peer_closed == 1 and b.peer_closed == 1)
