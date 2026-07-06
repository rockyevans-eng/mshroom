"""MLLP (Minimal Lower Layer Protocol) framing, client, and server.

MLLP wraps an HL7 message on the wire as::

    0x0B  <message bytes>  0x1C 0x0D

This module provides:

* :func:`frame` / :func:`unframe` -- pure framing helpers (shared by the
  client and the server below).
* :func:`send_message` -- the Sender's client: connect, send one framed
  message, wait for one framed response (the ACK), return a
  :class:`MllpResult`. Connection problems (refused, timeout, unreachable)
  come back as a *result with a friendly error string*, never as a raised
  exception -- the target listener may simply not exist yet, and the
  UI must show a clean message rather than a stack trace.
* :class:`MllpListener` -- the Listener's server: accepts connections,
  classifies each one (HL7 traffic vs. port scans, HTTP/TLS probes, and
  outright junk), ACKs real HL7 (AA if it parses cleanly, AE if the framed
  payload starts ``MSH`` but doesn't have enough structure to ACK
  meaningfully), and reports every connection as a :class:`ListenerEvent`
  via an ``on_event`` callback -- ``app/capture.py`` uses this to persist
  the capture log. Classification never raises: a bad connection is just
  another kind of hostile input, logged and closed, never a crash.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from hl7kit.ack import build_ack
from hl7kit.parser import Message, parse_message

START_BLOCK = b"\x0b"
END_BLOCK = b"\x1c\x0d"

#: Refuse to buffer a response larger than this (defensive; ACKs are tiny).
MAX_RESPONSE_BYTES = 1_048_576  # 1 MB


def frame(payload: bytes) -> bytes:
    """Wrap *payload* in an MLLP frame."""
    return START_BLOCK + payload + END_BLOCK


def unframe(data: bytes) -> Optional[bytes]:
    """Extract the payload from one MLLP frame in *data*.

    Returns ``None`` when *data* doesn't contain a complete frame.
    Tolerates leading garbage before the start block and trailing bytes
    after the end block (both discarded).
    """
    start = data.find(START_BLOCK)
    if start == -1:
        return None
    end = data.find(END_BLOCK, start + 1)
    if end == -1:
        return None
    return data[start + 1 : end]


@dataclass(frozen=True)
class MllpResult:
    """Outcome of one send attempt.

    Exactly one of ``response`` / ``error`` is meaningful: on success
    ``ok`` is True and ``response`` holds the decoded response text (the
    ACK); on failure ``ok`` is False and ``error`` holds a short,
    human-readable description safe to show directly in the UI.
    """

    ok: bool
    response: str = ""
    error: str = ""


def send_message(
    host: str,
    port: int,
    message_text: str,
    timeout: float = 10.0,
) -> MllpResult:
    """Send one HL7 message over MLLP and wait for one framed response.

    Line endings in *message_text* are normalized to ``\\r`` before
    sending (HL7 requires bare-``\\r`` segment terminators on the wire;
    a message pasted into the UI may carry ``\\r\\n``/``\\n``).

    Never raises for network-level problems -- returns an
    :class:`MllpResult` with ``ok=False`` and a friendly ``error`` string
    for connection refused, timeouts, DNS failures, and a response that
    never completes a frame.
    """
    from hl7kit.parser import normalize_line_endings  # local import: avoid a cycle

    normalized, _ = normalize_line_endings(message_text)
    payload = normalized.encode("utf-8")

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(frame(payload))
            buffer = b""
            while len(buffer) < MAX_RESPONSE_BYTES:
                chunk = sock.recv(65536)
                if not chunk:
                    # Peer closed without completing a frame.
                    if buffer:
                        return MllpResult(
                            ok=False,
                            error=(
                                f"{host}:{port} closed the connection before sending a "
                                "complete MLLP response (partial data received)."
                            ),
                        )
                    return MllpResult(
                        ok=False,
                        error=(
                            f"{host}:{port} accepted the message but closed the "
                            "connection without sending a response (no ACK)."
                        ),
                    )
                buffer += chunk
                response_payload = unframe(buffer)
                if response_payload is not None:
                    return MllpResult(
                        ok=True,
                        response=response_payload.decode("utf-8", errors="replace"),
                    )
            return MllpResult(
                ok=False,
                error=f"Response from {host}:{port} exceeded 1 MB without completing an MLLP frame.",
            )
    except ConnectionRefusedError:
        return MllpResult(
            ok=False,
            error=(
                f"Connection refused by {host}:{port} -- nothing is listening there. "
                "Is the receiving interface deployed and started?"
            ),
        )
    except socket.timeout:
        return MllpResult(
            ok=False,
            error=(
                f"Timed out after {timeout:g}s talking to {host}:{port}. The host may be "
                "unreachable, or the listener accepted the message but never responded."
            ),
        )
    except socket.gaierror:
        return MllpResult(ok=False, error=f"Could not resolve host {host!r}.")
    except OSError as exc:
        # Catch-all for the remaining network errors (host unreachable,
        # network down, ...) -- still a friendly one-liner, never a traceback.
        return MllpResult(ok=False, error=f"Network error talking to {host}:{port}: {exc.strerror or exc}")


# ---------------------------------------------------------------------------
# Server: the Listener
# ---------------------------------------------------------------------------

#: Default port the Listener binds to (BUILD_PLAN section 5); configurable
#: per-instance and, in the app, via the HL7_LISTENER_PORT env var.
DEFAULT_LISTENER_PORT = 6671

#: Same ceiling as the client's response buffer, applied to inbound frames.
MAX_MESSAGE_BYTES = 1_048_576  # 1 MB

#: How long a connection may sit with no bytes arriving before it's logged
#: as TIMEOUT and closed.
DEFAULT_IDLE_TIMEOUT = 30.0

#: Every classification the Listener can emit (BUILD_PLAN section 5's table).
EVENT_HL7 = "HL7"
EVENT_NON_HL7_PAYLOAD = "NON_HL7_PAYLOAD"
EVENT_HTTP_PROBE = "HTTP_PROBE"
EVENT_TLS_PROBE = "TLS_PROBE"
EVENT_SCAN_PROBE = "SCAN_PROBE"
EVENT_JUNK = "JUNK"
EVENT_TIMEOUT = "TIMEOUT"
EVENT_CLASSES = (
    EVENT_HL7,
    EVENT_NON_HL7_PAYLOAD,
    EVENT_HTTP_PROBE,
    EVENT_TLS_PROBE,
    EVENT_SCAN_PROBE,
    EVENT_JUNK,
    EVENT_TIMEOUT,
)

_TLS_RECORD_BYTE = b"\x16"
_HTTP_METHODS = (b"GET ", b"POST ", b"HEAD ", b"OPTIONS ")


def _looks_like_http(data: bytes) -> bool:
    """True if *data* opens with an HTTP request line (BUILD_PLAN's four
    example verbs -- a browser hitting the listener port by mistake)."""
    return data.startswith(_HTTP_METHODS)


def _looks_like_tls(data: bytes) -> bool:
    """True if *data* opens with a TLS record header (content type
    ``handshake`` = ``0x16``) -- something trying HTTPS/TLS at a plaintext
    MLLP port."""
    return data[:1] == _TLS_RECORD_BYTE


def _raw_or_none(message: Message, reference: str) -> Optional[str]:
    """Raw text of a referenced node, or ``None`` if absent/empty."""
    node = message.get(reference)
    raw = getattr(node, "raw", None)
    return raw if isinstance(raw, str) and raw else None


def _message_is_valid(message: Message) -> bool:
    """True if *message* has the minimum structure needed to ACK it
    meaningfully: an MSH segment with a non-empty message type (MSH-9) and
    control ID (MSH-10). A framed payload that starts ``MSH`` but fails
    this check is "unparseable" for Listener purposes -- it gets an AE
    instead of an AA, per BUILD_PLAN section 5.
    """
    if message.segment("MSH") is None:
        return False
    return _raw_or_none(message, "MSH-9") is not None and _raw_or_none(message, "MSH-10") is not None


@dataclass(frozen=True)
class ListenerEvent:
    """One classified connection, ready to hand to ``app/capture.py``.

    ``first_bytes`` is capped at 256 bytes (the capture log's contract).
    ``full_message``/``ack_code``/``msh9``/``msh10`` are only meaningful
    when ``event_class == EVENT_HL7``; they're ``None`` for every other
    class.
    """

    event_class: str
    peer_host: str
    peer_port: int
    first_bytes: bytes
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    full_message: Optional[str] = None
    ack_code: Optional[str] = None
    msh9: Optional[str] = None
    msh10: Optional[str] = None


class MllpListener:
    """MLLP server: accepts connections, classifies and ACKs them.

    Runs its accept loop on a background daemon thread (started by
    :meth:`start`, stopped by :meth:`stop`); each connection is handled on
    its own short-lived daemon thread so one slow or hostile peer can't
    block the others. Every classified connection is reported through
    ``on_event`` (a ``Callable[[ListenerEvent], None]``) -- typically
    ``app.capture.CaptureLog.record``.

    Designed to survive indefinitely against ``nmap``-style scans,
    repeated open/close, and garbage floods: no code path here lets an
    exception from a single connection escape and kill the accept loop
    (see :meth:`_accept_loop` and :meth:`_safe_handle`).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_LISTENER_PORT,
        on_event: Optional[Callable[[ListenerEvent], None]] = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        max_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self.host = host
        self.port = port
        self.on_event = on_event
        self.idle_timeout = idle_timeout
        self.max_bytes = max_bytes
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def actual_port(self) -> int:
        """The bound port -- resolves ``port=0`` to the OS-assigned port
        once :meth:`start` has run; otherwise just the configured port."""
        if self._sock is not None:
            return self._sock.getsockname()[1]
        return self.port

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Bind and start accepting connections. Idempotent: calling
        ``start`` while already running does nothing. Raises ``OSError``
        if the port can't be bound (e.g. already in use) -- that's a
        startup-time decision for the caller, not something to swallow.
        """
        with self._lock:
            if self._running:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, self.port))
                sock.listen(50)
            except OSError:
                sock.close()
                raise
            # Short timeout so the accept loop wakes up periodically to
            # notice self._running flipping to False in stop().
            sock.settimeout(1.0)
            self._sock = sock
            self._running = True
            self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._accept_thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """Stop accepting new connections and close the listening socket.
        In-flight connection handlers finish on their own (each is a
        short-lived daemon thread); idempotent like :meth:`start`.
        """
        with self._lock:
            if not self._running:
                return
            self._running = False
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._accept_thread
        if thread is not None:
            thread.join(timeout=join_timeout)
            self._accept_thread = None

    # -- accept loop ----------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._sock.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                # Listening socket closed (stop()) or a fatal accept error
                # -- either way, the accept loop is done.
                break
            except Exception:
                # Belt and suspenders: nothing accept() does should land
                # here, but the accept loop must never die.
                continue
            threading.Thread(target=self._safe_handle, args=(conn, addr), daemon=True).start()

    def _safe_handle(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        """Outermost guard around one connection: whatever goes wrong in
        classification, this connection's thread dies quietly and the
        socket gets closed -- the accept loop and every other connection
        are unaffected."""
        try:
            self._handle_connection(conn, addr)
        except Exception:
            pass
        finally:
            self._close(conn)

    # -- per-connection classification ----------------------------------

    def _handle_connection(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        peer_host, peer_port = addr[0], addr[1]
        conn.settimeout(self.idle_timeout)
        try:
            self._classify_and_respond(conn, peer_host, peer_port)
        except OSError:
            # Peer reset the connection, network hiccup mid-read/write --
            # nothing more to classify; the connection just gets closed.
            pass

    def _classify_and_respond(self, conn: socket.socket, peer_host: str, peer_port: int) -> None:
        try:
            first = conn.recv(65536)
        except socket.timeout:
            self._emit(EVENT_TIMEOUT, peer_host, peer_port, b"")
            return

        if not first:
            # Connected, sent nothing, and the peer is already gone --
            # the classic port-scan signature.
            self._emit(EVENT_SCAN_PROBE, peer_host, peer_port, b"")
            return

        if _looks_like_tls(first):
            self._emit(EVENT_TLS_PROBE, peer_host, peer_port, first)
            return

        if _looks_like_http(first):
            self._emit(EVENT_HTTP_PROBE, peer_host, peer_port, first)
            return

        if first[:1] != START_BLOCK:
            # Doesn't open like MLLP, HTTP, or TLS -- junk, and there's no
            # reason to keep waiting to find out what it is.
            self._emit(EVENT_JUNK, peer_host, peer_port, first)
            return

        buffer = first
        while unframe(buffer) is None:
            if len(buffer) > self.max_bytes:
                self._emit(EVENT_JUNK, peer_host, peer_port, buffer)
                return
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                self._emit(EVENT_TIMEOUT, peer_host, peer_port, buffer)
                return
            if not chunk:
                # Closed mid-frame: never completed, nothing to ACK.
                self._emit(EVENT_JUNK, peer_host, peer_port, buffer)
                return
            buffer += chunk

        payload = unframe(buffer)
        assert payload is not None  # loop only exits once a frame completes

        if not payload.startswith(b"MSH"):
            # Framed, but not HL7 -- can't build a meaningful ACK (no MSH
            # to read sender/receiver/control ID from), so just close.
            self._emit(EVENT_NON_HL7_PAYLOAD, peer_host, peer_port, buffer)
            return

        text = payload.decode("utf-8", errors="replace")
        message = parse_message(text)
        ack_code = "AA" if _message_is_valid(message) else "AE"
        ack_text = build_ack(message, ack_code)
        try:
            conn.sendall(frame(ack_text.encode("utf-8")))
        except OSError:
            pass  # best-effort ACK -- the event is recorded either way
        self._emit(
            EVENT_HL7,
            peer_host,
            peer_port,
            buffer,
            full_message=text,
            ack_code=ack_code,
            message=message,
        )

    def _emit(
        self,
        event_class: str,
        peer_host: str,
        peer_port: int,
        raw: bytes,
        *,
        full_message: Optional[str] = None,
        ack_code: Optional[str] = None,
        message: Optional[Message] = None,
    ) -> None:
        msh9 = msh10 = None
        if message is not None:
            msh9 = _raw_or_none(message, "MSH-9")
            msh10 = _raw_or_none(message, "MSH-10")
        event = ListenerEvent(
            event_class=event_class,
            peer_host=peer_host,
            peer_port=peer_port,
            first_bytes=raw[:256],
            full_message=full_message,
            ack_code=ack_code,
            msh9=msh9,
            msh10=msh10,
        )
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                # A broken capture log must not be able to take down a
                # connection handler, let alone the accept loop.
                pass

    @staticmethod
    def _close(conn: socket.socket) -> None:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
