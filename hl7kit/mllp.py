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
  UI must show a clean message rather than a stack trace. This is the
  "new connection per message" mode.
* :class:`MllpConnection` -- the same client behavior over one *persistent*
  socket (the "Keep connection open" mode, like the setting of that name on
  an interface engine's TCP sender): reuse across sends, pipelined-byte
  keeping, stale-connection detection and one transparent reconnect.
  :func:`send_message` is implemented on top of it.
* :func:`extract_frames` -- pure helper the server uses to pull every
  complete frame out of a receive buffer (see "persistent connections"
  below).
* :class:`MllpListener` -- the Listener's server: accepts connections,
  classifies each one (HL7 traffic vs. port scans, HTTP/TLS probes, and
  outright junk), ACKs real HL7 (AA if it parses cleanly, AE if the framed
  payload starts ``MSH`` but doesn't have enough structure to ACK
  meaningfully), and reports what it sees as :class:`ListenerEvent` objects
  via an ``on_event`` callback -- ``app/capture.py`` uses this to persist
  the capture log. Classification never raises: a bad connection is just
  another kind of hostile input, logged and closed, never a crash.

Persistent connections
----------------------
Real interface engines open one MLLP connection and keep it open, sending
many messages over it (each one waiting for its ACK, or several sent
back-to-back). So the Listener does **not** close a connection after the
first message. The rules are:

1. The *first* bytes on a connection decide what it is. Anything that does
   not start with the MLLP start byte ``0x0B`` (TLS handshake, HTTP
   request, junk, or nothing at all) is classified and closed right away,
   exactly as before. Non-HL7 traffic is never parsed as a message.
2. A connection that opens with ``0x0B`` is treated as an MLLP session:
   every complete frame is answered and recorded as its own event (one
   :class:`ListenerEvent` per frame), and any incomplete trailing bytes are
   kept as the start of the next frame.
3. A session ends quietly (no event) when the peer closes cleanly or goes
   idle *between* messages -- that is simply how a persistent connection
   normally ends. It ends with an event only when something was wrong:
   partial frame + idle = ``TIMEOUT``; partial frame + close = ``JUNK``;
   an over-long unfinished frame = ``JUNK``; stray non-MLLP bytes where a
   new frame should start = ``JUNK``; a framed payload that is not HL7 =
   ``NON_HL7_PAYLOAD``.
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


#: Bytes (CR, LF) some senders emit between frames (for example ``0x1C 0x0D 0x0A``,
#: or a blank line after each message). They carry no meaning, so the server
#: skips them instead of treating them as junk.
_FILLER_BYTES = (0x0D, 0x0A)  # carriage return, line feed


def extract_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Pull every complete MLLP frame out of *buffer*.

    Returns ``(payloads, remainder)``:

    * ``payloads`` -- the bytes inside each complete frame, in order, with
      the ``0x0B`` start byte and ``0x1C 0x0D`` end bytes removed.
    * ``remainder`` -- whatever is left after the last complete frame. It is
      one of three things: empty (the buffer ended exactly on a frame
      boundary); the *start of an incomplete frame* (begins with ``0x0B``
      -- keep it and append the next network read to it); or *junk* (begins
      with any other byte -- a new frame should have started there but
      didn't, so the caller decides what to do; the server closes).

    Bare CR / LF bytes between frames are skipped silently. This is
    a pure function: no sockets, no state, safe to call repeatedly on a
    growing buffer.

    Unlike :func:`unframe` (which the client uses and which tolerates junk
    *before* a frame), this refuses to skip arbitrary bytes: on a
    long-lived connection, silently skipping unknown bytes could hide a
    peer that is not speaking MLLP at all.
    """
    payloads: list[bytes] = []
    pos = 0
    while True:
        # Skip filler between frames (see _FILLER_BYTES).
        while pos < len(buffer) and buffer[pos] in _FILLER_BYTES:
            pos += 1
        if pos >= len(buffer) or buffer[pos : pos + 1] != START_BLOCK:
            # Either the buffer is used up, or the next byte is not a start
            # block (junk). Both are "no more frames to extract".
            return payloads, buffer[pos:]
        end = buffer.find(END_BLOCK, pos + 1)
        if end == -1:
            # Frame started but its end marker has not arrived yet. This
            # includes a buffer that ends with a lone 0x1C: the 0x0D that
            # completes the marker is still on the wire.
            return payloads, buffer[pos:]
        payloads.append(buffer[pos + 1 : end])
        pos = end + len(END_BLOCK)


@dataclass(frozen=True)
class MllpResult:
    """Outcome of one send attempt.

    Exactly one of ``response`` / ``error`` is meaningful: on success
    ``ok`` is True and ``response`` holds the decoded response text (the
    ACK); on failure ``ok`` is False and ``error`` holds a short,
    human-readable description safe to show directly in the UI.

    ``reused`` is True only when the message went out on a connection that
    was *already open* from an earlier send (see :class:`MllpConnection`).
    A brand-new connection -- including one opened by the automatic
    reconnect after the peer closed the old one -- reports ``False``.
    """

    ok: bool
    response: str = ""
    error: str = ""
    reused: bool = False


def _describe_network_error(exc: OSError, host: str, port: int, timeout: float) -> str:
    """Turn a socket exception into the friendly one-line message shown in the UI.

    Shared by :func:`send_message` and :class:`MllpConnection` so both report
    the same problem in the same words. The order of the ``isinstance``
    checks matters: ``ConnectionRefusedError``, ``socket.timeout`` and
    ``socket.gaierror`` are all subclasses of ``OSError``, so the generic
    ``OSError`` catch-all must come last.
    """
    if isinstance(exc, ConnectionRefusedError):
        return (
            f"Connection refused by {host}:{port} -- nothing is listening there. "
            "Is the receiving interface deployed and started?"
        )
    if isinstance(exc, socket.timeout):
        return (
            f"Timed out after {timeout:g}s talking to {host}:{port}. The host may be "
            "unreachable, or the listener accepted the message but never responded."
        )
    if isinstance(exc, socket.gaierror):
        return f"Could not resolve host {host!r}."
    # Catch-all for the remaining network errors (host unreachable, network
    # down, connection reset, ...) -- still a friendly one-liner, never a traceback.
    return f"Network error talking to {host}:{port}: {exc.strerror or exc}"


def _drop_leading_junk(buffer: bytes) -> bytes:
    """Discard any bytes in front of the first MLLP start byte (``0x0B``).

    The client is deliberately more forgiving than the server (see
    :func:`extract_frames`): a receiver may emit stray CR/LF or banner bytes
    before its ACK and we still want to read the ACK. If the buffer holds no
    start byte at all, everything in it is junk and is discarded.
    """
    start = buffer.find(START_BLOCK)
    return b"" if start == -1 else buffer[start:]


def _split_first_frame(buffer: bytes) -> Optional[tuple[bytes, bytes]]:
    """Split *buffer* into ``(first_payload, everything_after_that_frame)``.

    Returns ``None`` if *buffer* holds no complete frame yet. Built on
    :func:`extract_frames`: any further complete frames are re-framed and
    kept, followed by the unfinished remainder, so nothing an engine
    pipelined behind its ACK is lost. (Bare CR/LF filler between frames is
    dropped -- it carries no meaning.)
    """
    payloads, remainder = extract_frames(buffer)
    if not payloads:
        return None
    rest = b"".join(frame(p) for p in payloads[1:]) + remainder
    return payloads[0], rest


#: Errors that mean "the peer's end of an idle connection is already gone"
#: when they surface while *writing* a message onto a reused socket. Only
#: these trigger the transparent reconnect after a write (see
#: :meth:`MllpConnection.send`).
_PEER_GONE_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class MllpConnection:
    """One persistent MLLP client connection (the "Keep connection open" mode).

    Interface engines' TCP senders have a *Keep Connection Open* setting:
    on, one socket carries many messages; off, every message gets a fresh
    socket. A receiving engine may behave differently in the two modes, so
    the Sender must be able to test both. :func:`send_message` is the
    "off" mode; this class is the "on" mode.

    Behavior contract (mirrors :func:`send_message`):

    * :meth:`send` normalizes line endings, frames the message, writes it,
      and reads exactly one framed response (the ACK).
    * It **never raises** for network problems -- it returns an
      :class:`MllpResult` with a friendly ``error``.
    * The socket is opened lazily by the first :meth:`send` (or explicitly
      via :meth:`connect`) and then reused by later sends.

    Rules that only matter for a connection that lives across messages:

    1. **Pipelined bytes are kept.** An engine may write more than one frame
       before we read (for example an extra ACK). Only the first complete
       frame is this send's response; every byte after it stays in an
       internal buffer and is consulted first by the *next* send.
    2. **Stale connections are detected, not blindly reused.** Before
       writing onto a reused socket we check whether the peer has closed its
       end while the connection sat idle (engines close idle connections).
       If so we reconnect **once**, transparently, before sending; if the
       reconnect fails the error says so plainly.
    3. **No silent double-delivery.** The reconnect only happens when
       nothing has been written yet (peer already gone before we write, or
       the write itself failed with a reset/broken pipe). If the message
       was written and the peer then dropped the connection without an ACK,
       we cannot know whether it was processed, so that is reported as an
       error rather than retried -- resending could deliver a duplicate.
    4. **A failed send poisons the socket.** After any error or timeout
       the socket is closed and the read buffer cleared: a late ACK for the
       failed message would otherwise be mistaken for the response to the
       next one.

    Thread-safe: an internal lock serializes sends, so two threads sharing
    one connection can't interleave frames or steal each other's ACKs.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        # Bytes received beyond the ACK we returned (rule 1 above).
        self._buffer = b""
        # Re-entrant: send() calls peer_has_closed(), which takes the lock too.
        self._lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True if a socket is currently held (it may still have been closed
        by the peer -- see :meth:`peer_has_closed`)."""
        return self._sock is not None

    def connect(self, timeout: float = 10.0) -> MllpResult:
        """Open the socket now if it isn't open. Returns ``ok=True`` (with an
        empty response) on success, or ``ok=False`` with a friendly error.
        Never raises. Calling it on an open connection does nothing."""
        with self._lock:
            if self._sock is not None:
                return MllpResult(ok=True, reused=True)
            return self._open(timeout)

    def close(self) -> None:
        """Close the socket (if any) and forget buffered bytes. Idempotent.
        A later :meth:`send` simply opens a new connection."""
        with self._lock:
            self._drop()

    def __enter__(self) -> "MllpConnection":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def peer_has_closed(self) -> bool:
        """True if the open socket's peer has closed (or reset) it.

        Uses a non-blocking ``MSG_PEEK`` read, which looks at pending bytes
        without consuming them: ``b""`` means the peer sent FIN (closed), a
        reset error means it aborted, and "would block" means the connection
        is quiet but alive. Returns ``False`` when no socket is held.
        """
        with self._lock:
            sock = self._sock
            if sock is None:
                return False
            try:
                sock.settimeout(0.0)  # non-blocking for the peek only
                return sock.recv(1, socket.MSG_PEEK) == b""
            except BlockingIOError:
                return False  # nothing to read right now: alive and idle
            except OSError:
                return True  # reset / aborted: as good as closed
            finally:
                try:
                    sock.settimeout(None)
                except OSError:
                    pass  # socket already dead; send() will notice

    # -- sending --------------------------------------------------------

    def send(self, message_text: str, timeout: float = 10.0) -> MllpResult:
        """Send one HL7 message and wait for one framed response (the ACK).

        Same normalization, framing and error behavior as
        :func:`send_message`; see the class docstring for the persistent-
        connection rules (buffer keeping, stale detection, single
        reconnect). *timeout* applies to opening a connection and to each
        socket read/write.
        """
        from hl7kit.parser import normalize_line_endings  # local import: avoid a cycle

        normalized, _ = normalize_line_endings(message_text)
        data = frame(normalized.encode("utf-8"))

        with self._lock:
            # Rule 2: a reused socket whose peer already left is replaced
            # *before* we write, so nothing is lost or duplicated.
            replaced_stale = self._sock is not None and self.peer_has_closed()
            if replaced_stale:
                self._drop()
            reused = self._sock is not None
            if not reused:
                failure = self._open_or_explain(timeout, replaced_stale)
                if failure is not None:
                    return failure

            assert self._sock is not None
            self._sock.settimeout(timeout)
            try:
                self._sock.sendall(data)
            except _PEER_GONE_ERRORS as exc:
                if not reused:
                    return self._fail(exc, timeout)
                # The peer's close raced our check (rule 3): the write hit a
                # dead socket, so the message did not arrive. Reconnect once.
                self._drop()
                failure = self._open_or_explain(timeout, True)
                if failure is not None:
                    return failure
                reused = False
                try:
                    self._sock.sendall(data)  # type: ignore[union-attr]
                except OSError as exc2:
                    return self._fail(exc2, timeout)
            except OSError as exc:
                return self._fail(exc, timeout, reused)
            return self._read_ack(timeout, reused)

    # -- internals (all called with self._lock held) ---------------------

    def _drop(self) -> None:
        """Close the socket and clear the buffer (rule 4)."""
        sock, self._sock = self._sock, None
        self._buffer = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _open(self, timeout: float) -> MllpResult:
        """Open a fresh socket; friendly error result on failure."""
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as exc:
            return MllpResult(ok=False, error=_describe_network_error(exc, self.host, self.port, timeout))
        self._buffer = b""
        return MllpResult(ok=True)

    def _open_or_explain(self, timeout: float, was_reconnect: bool) -> Optional[MllpResult]:
        """Open a socket; return ``None`` on success or an error result.

        When *was_reconnect* the failure message says the old connection had
        been closed by the peer and the reconnect attempt failed too, so the
        user learns both facts.
        """
        opened = self._open(timeout)
        if opened.ok:
            return None
        if was_reconnect:
            return MllpResult(
                ok=False,
                error=(
                    f"The connection to {self.host}:{self.port} had been closed by the peer since "
                    f"the last send, and reconnecting failed: {opened.error}"
                ),
            )
        return opened

    def _fail(self, exc: OSError, timeout: float, reused: bool = False) -> MllpResult:
        """Build the friendly error for *exc* and poison the socket (rule 4)."""
        self._drop()
        return MllpResult(
            ok=False,
            error=_describe_network_error(exc, self.host, self.port, timeout),
            reused=reused,
        )

    def _read_ack(self, timeout: float, reused: bool) -> MllpResult:
        """Read until one complete frame is buffered; return it as the ACK.

        Bytes already buffered from an earlier send are examined first
        (rule 1). Everything after the first complete frame is put back
        into the buffer for the next send.
        """
        assert self._sock is not None
        host, port = self.host, self.port
        total_read = 0  # per-call counter so endless junk can't loop forever
        while True:
            self._buffer = _drop_leading_junk(self._buffer)
            split = _split_first_frame(self._buffer)
            if split is not None:
                payload, self._buffer = split
                return MllpResult(ok=True, response=payload.decode("utf-8", errors="replace"), reused=reused)
            if len(self._buffer) >= MAX_RESPONSE_BYTES or total_read >= MAX_RESPONSE_BYTES:
                self._drop()
                return MllpResult(
                    ok=False,
                    error=f"Response from {host}:{port} exceeded 1 MB without completing an MLLP frame.",
                    reused=reused,
                )
            try:
                chunk = self._sock.recv(65536)
            except OSError as exc:
                return self._fail(exc, timeout, reused)
            if not chunk:
                # Peer closed without completing a frame. What we say depends
                # on whether any of a response had arrived.
                partial = bool(self._buffer)
                self._drop()
                if partial:
                    error = (
                        f"{host}:{port} closed the connection before sending a "
                        "complete MLLP response (partial data received)."
                    )
                else:
                    error = (
                        f"{host}:{port} accepted the message but closed the "
                        "connection without sending a response (no ACK)."
                    )
                return MllpResult(ok=False, error=error, reused=reused)
            total_read += len(chunk)
            self._buffer += chunk


def send_message(
    host: str,
    port: int,
    message_text: str,
    timeout: float = 10.0,
) -> MllpResult:
    """Send one HL7 message over MLLP and wait for one framed response.

    This is the "Keep connection open = off" mode: a new connection per
    message, closed as soon as the ACK is read. It is a thin wrapper over
    :class:`MllpConnection` so both modes share one implementation.

    Line endings in *message_text* are normalized to ``\\r`` before
    sending (HL7 requires bare-``\\r`` segment terminators on the wire;
    a message pasted into the UI may carry ``\\r\\n``/``\\n``).

    Never raises for network-level problems -- returns an
    :class:`MllpResult` with ``ok=False`` and a friendly ``error`` string
    for connection refused, timeouts, DNS failures, and a response that
    never completes a frame.
    """
    with MllpConnection(host, port) as connection:
        return connection.send(message_text, timeout=timeout)


# ---------------------------------------------------------------------------
# Server: the Listener
# ---------------------------------------------------------------------------

#: Default port the Listener binds to (BUILD_PLAN section 5); configurable
#: per-instance and, in the app, via the HL7_LISTENER_PORT env var.
DEFAULT_LISTENER_PORT = 6671

#: Same ceiling as the client's response buffer, applied to inbound frames.
MAX_MESSAGE_BYTES = 1_048_576  # 1 MB

#: How long a connection may sit with no bytes arriving before it is closed.
#: The clock restarts every time bytes arrive (so, in practice, after every
#: message). Idle with nothing buffered is a normal end of a persistent
#: session and is closed quietly; idle in the middle of a frame is logged
#: as TIMEOUT.
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

    ``raw_frame`` is the exact payload bytes that arrived between the MLLP
    start/end markers (markers excluded), uncapped and undecoded. It is set
    for ``EVENT_HL7`` and ``EVENT_NON_HL7_PAYLOAD`` only. ``full_message``
    is that same payload decoded with ``errors="replace"`` for display, so
    invalid UTF-8 becomes U+FFFD there and *only* ``raw_frame`` is
    guaranteed to equal what was on the wire.
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
    raw_frame: Optional[bytes] = None


class MllpListener:
    """MLLP server: accepts connections, classifies and ACKs them.

    Runs its accept loop on a background daemon thread (started by
    :meth:`start`, stopped by :meth:`stop`); each connection is handled on
    its own daemon thread (which lives as long as the connection does, since
    connections are persistent) so one slow or hostile peer can't block the
    others. Every classified connection is reported through
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
        # Live per-connection state, so stop() can shut down *everything*:
        # closing a connection's socket makes a handler blocked in recv()
        # fail immediately instead of sitting out the idle timeout -- a
        # stop() would otherwise leave handler threads lingering for up to
        # `idle_timeout` seconds (service mode needs a prompt, clean stop).
        self._conns: set[socket.socket] = set()
        self._handlers: set[threading.Thread] = set()

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
        """Stop the listener completely: close the listening socket, stop
        the accept loop, close any in-flight connections, and join their
        handler threads. Idempotent like :meth:`start`.

        Closing an in-flight connection's socket is what makes this
        prompt: a handler blocked in ``recv()`` gets an immediate
        ``OSError`` (handled as "peer went away") instead of waiting out
        the idle timeout, so no handler threads outlive this call by more
        than a moment.
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
        # Unblock and reap in-flight connection handlers. Snapshot under
        # the lock; handlers mutate these sets as they finish.
        with self._lock:
            conns = list(self._conns)
            handlers = list(self._handlers)
        for conn in conns:
            self._close(conn)
        for handler in handlers:
            handler.join(timeout=join_timeout)

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
            handler = threading.Thread(target=self._safe_handle, args=(conn, addr), daemon=True)
            with self._lock:
                if not self._running:
                    # stop() won the race while accept() was returning:
                    # it has already swept _conns, so registering now
                    # would leak this connection past the stop.
                    self._close(conn)
                    break
                self._conns.add(conn)
                self._handlers.add(handler)
            handler.start()

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
            with self._lock:
                self._conns.discard(conn)
                self._handlers.discard(threading.current_thread())

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

        # From here on this is an MLLP session (see the module docstring's
        # "Persistent connections" section). `first` is the opening bytes;
        # they already start with 0x0B.
        self._serve_mllp_session(conn, peer_host, peer_port, first)

    def _serve_mllp_session(self, conn: socket.socket, peer_host: str, peer_port: int, buffer: bytes) -> None:
        """Serve one persistent MLLP connection until it ends.

        *buffer* holds the bytes already read (non-empty, starts with
        ``0x0B``). The loop is: extract every complete frame, answer each
        one, look at what is left over, then read more bytes. It returns
        (and the caller closes the socket) when the peer goes away, goes
        idle, or sends something that makes continuing pointless.
        """
        while True:
            payloads, buffer = extract_frames(buffer)

            for payload in payloads:
                if not self._handle_frame(conn, peer_host, peer_port, payload):
                    # A non-HL7 payload was reported; close the connection.
                    return

            if buffer and buffer[:1] != START_BLOCK:
                # Bytes where a new frame should have started, and they are
                # not the 0x0B start byte: the peer stopped speaking MLLP
                # mid-stream. Report them and close -- guessing where the
                # next frame begins could mis-frame every later message.
                self._emit(EVENT_JUNK, peer_host, peer_port, buffer)
                return

            if len(buffer) > self.max_bytes:
                # An unfinished frame this large is not a real HL7 message;
                # stop buffering it (memory safety).
                self._emit(EVENT_JUNK, peer_host, peer_port, buffer)
                return

            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                if buffer:
                    # Idle in the middle of a frame: the peer started a
                    # message and never finished it.
                    self._emit(EVENT_TIMEOUT, peer_host, peer_port, buffer)
                # Empty buffer = idle *between* messages. That is how a
                # persistent connection normally winds down; not an event.
                return
            if not chunk:
                if buffer:
                    # Closed mid-frame: never completed, nothing to ACK.
                    self._emit(EVENT_JUNK, peer_host, peer_port, buffer)
                # Empty buffer = clean close after complete messages; quiet.
                return
            buffer += chunk

    def _handle_frame(self, conn: socket.socket, peer_host: str, peer_port: int, payload: bytes) -> bool:
        """Answer and record one complete frame's payload.

        Returns ``True`` if the connection should stay open for more
        frames, ``False`` if it should be closed (payload was not HL7).
        """
        if not payload.startswith(b"MSH"):
            # Framed, but not HL7 -- can't build a meaningful ACK (no MSH
            # to read sender/receiver/control ID from), so just close.
            self._emit(EVENT_NON_HL7_PAYLOAD, peer_host, peer_port, frame(payload), raw_frame=payload)
            return False

        text = payload.decode("utf-8", errors="replace")
        message = parse_message(text)
        # AA = "accepted"; AE = "error". A framed payload that starts MSH but
        # lacks MSH-9/MSH-10 gets AE, and the connection stays usable: the
        # sender's *next* message may be fine.
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
            frame(payload),  # the raw bytes of exactly this frame
            full_message=text,
            ack_code=ack_code,
            message=message,
            raw_frame=payload,  # exact wire bytes; full_message is lossy for invalid UTF-8
        )
        return True

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
        raw_frame: Optional[bytes] = None,
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
            raw_frame=raw_frame,
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
