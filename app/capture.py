"""SQLite capture log for the MLLP Listener (Phase 2).

One row per classified connection: timestamp, peer, classification, the
first 256 bytes (hex + printable), and -- for HL7 traffic only -- the full
message text, the ACK code sent, and MSH-9/MSH-10 for the events table.

``capture.db`` is a runtime artifact (gitignored), not checked-in data --
see BUILD_PLAN section 9.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "capture.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    peer_host TEXT NOT NULL,
    peer_port INTEGER NOT NULL,
    event_class TEXT NOT NULL,
    first_bytes_hex TEXT NOT NULL,
    first_bytes_printable TEXT NOT NULL,
    full_message TEXT,
    ack_code TEXT,
    msh9 TEXT,
    msh10 TEXT
)
"""

_INSERT_SQL = """
INSERT INTO events (
    timestamp, peer_host, peer_port, event_class,
    first_bytes_hex, first_bytes_printable, full_message, ack_code, msh9, msh10
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _printable(data: bytes) -> str:
    """Render *data* as printable ASCII, non-printable bytes shown as '.'
    -- a quick eyeball view alongside the hex dump."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


class CaptureLog:
    """Thread-safe wrapper around one SQLite ``capture.db``.

    The Listener hands off events from many short-lived per-connection
    threads (see :class:`hl7kit.mllp.MllpListener`); this is a lab tool
    with modest traffic, so a single shared connection guarded by a lock
    is simpler and safer than juggling one SQLite connection per thread.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def record(self, event: Any) -> int:
        """Store one ``hl7kit.mllp.ListenerEvent``. Returns the new row id.

        Accepts anything with the same attributes as ``ListenerEvent``
        (duck-typed) so tests can pass lightweight stand-ins without
        importing ``hl7kit.mllp``.
        """
        first_bytes = bytes(event.first_bytes)[:256]
        with self._lock:
            cur = self._conn.execute(
                _INSERT_SQL,
                (
                    event.timestamp.isoformat(),
                    event.peer_host,
                    event.peer_port,
                    event.event_class,
                    first_bytes.hex(),
                    _printable(first_bytes),
                    event.full_message,
                    event.ack_code,
                    event.msh9,
                    event.msh10,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        """Most recent *limit* events, newest first. Omits ``full_message``
        (can be large) but flags its presence via ``has_message`` so the
        UI knows which rows are clickable into the Viewer."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, timestamp, peer_host, peer_port, event_class, "
                "first_bytes_hex, first_bytes_printable, ack_code, msh9, msh10, "
                "(full_message IS NOT NULL) AS has_message "
                "FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            events = [dict(row) for row in rows]
            for event in events:
                event["has_message"] = bool(event["has_message"])  # SQLite has no bool type
            return events

    def get_event(self, event_id: int) -> Optional[dict[str, Any]]:
        """One event by id, including its full message text if any.
        Returns ``None`` if no such row exists."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return dict(row) if row is not None else None

    def counts_by_class(self) -> dict[str, int]:
        """Total events seen per classification, for the UI's probe
        counters. Classes never seen are simply absent (callers default
        to 0)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_class, COUNT(*) AS n FROM events GROUP BY event_class"
            ).fetchall()
            return {row["event_class"]: row["n"] for row in rows}

    def clear(self) -> None:
        """Delete all rows (keeps the schema). Test helper."""
        with self._lock:
            self._conn.execute("DELETE FROM events")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
