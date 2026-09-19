"""Registry of live Sender connections for the "Keep connection open" option.

The Send tab normally opens a new MLLP connection per message
(:func:`hl7kit.mllp.send_message`). With *Keep connection open* ticked, the
web server has to remember the socket between two HTTP requests, because the
browser cannot hold a TCP connection to the receiver itself. This module is
that memory: a :class:`SendPool` maps ``(host, port)`` to one
:class:`hl7kit.mllp.MllpConnection`.

Invariants the rest of the app relies on:

* **Bounded.** The pool never holds more than ``max_connections`` sockets. A
  new destination beyond the cap evicts (closes) the least recently used
  connection -- an old idle one is the cheapest thing to lose.
* **Self-cleaning.** A connection unused for ``idle_limit`` seconds is
  closed. That is checked lazily at the start of every pool call and by a
  small daemon reaper thread (so a quiet server still lets go of sockets);
  :meth:`SendPool.shutdown` stops the reaper and closes everything.
* **Failure drops the entry.** A send that fails leaves the underlying
  connection closed (see :class:`~hl7kit.mllp.MllpConnection`), so the pool
  forgets it and the next send starts clean.
* **One lock guards the registry; each connection has its own lock.** The
  pool lock is *not* held while a message is in flight, so a slow receiver
  can't stall sends to other destinations.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

from hl7kit.mllp import MllpConnection, MllpResult

#: Most live connections the pool will hold at once (see module docstring).
DEFAULT_MAX_CONNECTIONS = 16

#: Seconds a connection may sit unused before it is closed.
DEFAULT_IDLE_LIMIT = 60.0

PoolKey = tuple[str, int]


def make_key(host: str, port: int) -> PoolKey:
    """Normalize a destination into a registry key.

    Host names are case-insensitive and users paste stray spaces, so both
    are normalized; otherwise ``Example`` and ``example`` would hold two
    connections to the same place.
    """
    return host.strip().lower(), port


@dataclass
class _Entry:
    """One registry slot: the connection plus when it was last used."""

    connection: MllpConnection
    last_used: float


class SendPool:
    """Thread-safe, bounded registry of persistent Sender connections."""

    def __init__(
        self,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        idle_limit: float = DEFAULT_IDLE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """*clock* is injectable only so callers can control time; the
        default (``time.monotonic``) is right for production."""
        self.max_connections = max_connections
        self.idle_limit = idle_limit
        self._clock = clock
        self._lock = threading.Lock()
        # OrderedDict keeps least-recently-used first, which is what the
        # eviction rule needs (move_to_end on every use).
        self._entries: "OrderedDict[PoolKey, _Entry]" = OrderedDict()
        self._reaper: Optional[threading.Thread] = None
        self._reaper_stop = threading.Event()

    # -- introspection ----------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[PoolKey]:
        """Destinations currently held, least recently used first."""
        with self._lock:
            return list(self._entries)

    # -- sending ------------------------------------------------------------

    def send(self, host: str, port: int, message_text: str, timeout: float = 10.0) -> MllpResult:
        """Send *message_text* over the pooled connection for ``(host, port)``.

        Creates the connection on first use and reuses it afterwards; the
        returned :class:`MllpResult` has ``reused`` set accordingly. Never
        raises for network problems (same contract as ``send_message``).
        """
        key = make_key(host, port)
        with self._lock:
            # Lazy idle check on every call (see module docstring).
            doomed = self._pop_idle_locked()
            entry = self._entries.get(key)
            if entry is None:
                doomed += self._pop_lru_for_room_locked()
                entry = _Entry(MllpConnection(key[0], port), self._clock())
                self._entries[key] = entry
            # Stamp the use *now*, not only at completion, so a send that is
            # still in flight can never look idle to the reaper.
            entry.last_used = self._clock()
            self._entries.move_to_end(key)
            self._ensure_reaper_locked()
        # Closing may wait for a connection's in-flight send, so it happens
        # with the pool lock released.
        for old in doomed:
            old.connection.close()

        # The message is in flight with the pool lock released (see module
        # docstring). MllpConnection serializes concurrent users itself.
        result = entry.connection.send(message_text, timeout=timeout)

        with self._lock:
            still_registered = self._entries.get(key) is entry
            if still_registered and result.ok:
                entry.last_used = self._clock()
                return result
            if still_registered:
                # Failed send: the connection already closed its socket, so
                # the slot is dead weight. Forget it; next send starts clean.
                del self._entries[key]
        # Either the send failed, or this entry was evicted / closed by
        # someone else while we were sending. Close it so a connection the
        # pool no longer tracks can't leak (a lazily reopened socket would).
        if not still_registered or not result.ok:
            entry.connection.close()
        return result

    # -- closing ------------------------------------------------------------

    def close(self, host: str, port: int) -> int:
        """Close and forget the connection for one destination.

        Returns how many connections were closed (0 or 1).
        """
        with self._lock:
            entry = self._entries.pop(make_key(host, port), None)
        if entry is None:
            return 0
        entry.connection.close()
        return 1

    def close_all(self) -> int:
        """Close and forget every connection; returns how many were closed."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.connection.close()
        return len(entries)

    def shutdown(self) -> int:
        """Stop the reaper thread and close everything (app shutdown)."""
        self._reaper_stop.set()
        reaper = self._reaper
        if reaper is not None and reaper is not threading.current_thread():
            reaper.join(timeout=5.0)
        with self._lock:
            self._reaper = None
        return self.close_all()

    def reap_idle(self) -> int:
        """Close connections idle longer than ``idle_limit``; returns the count."""
        with self._lock:
            stale = self._pop_idle_locked()
        for entry in stale:
            entry.connection.close()
        return len(stale)

    # -- internals ----------------------------------------------------------

    def _pop_idle_locked(self) -> list[_Entry]:
        """Remove and return idle entries. Caller holds the lock and must
        close the returned connections *after* releasing it (closing waits
        for any in-flight send on that connection)."""
        now = self._clock()
        expired = [key for key, e in self._entries.items() if now - e.last_used >= self.idle_limit]
        return [self._entries.pop(key) for key in expired]

    def _pop_lru_for_room_locked(self) -> list[_Entry]:
        """Remove least-recently-used entries until there is room for one
        more, and return them for the caller to close (outside the lock)."""
        evicted = []
        while len(self._entries) >= self.max_connections:
            _, oldest = self._entries.popitem(last=False)
            evicted.append(oldest)
        return evicted

    def _ensure_reaper_locked(self) -> None:
        """Start the daemon reaper if it isn't running. It wakes every few
        seconds and closes idle connections, so an unattended server does
        not hold sockets open past the idle limit until the next request."""
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._reaper_stop = stop = threading.Event()
        interval = max(0.05, min(5.0, self.idle_limit / 2))

        def _loop() -> None:
            # Event.wait doubles as an interruptible sleep: shutdown() sets
            # the event and this returns immediately.
            while not stop.wait(interval):
                self.reap_idle()

        self._reaper = threading.Thread(target=_loop, name="send-pool-reaper", daemon=True)
        self._reaper.start()
