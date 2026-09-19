"""SQLite capture log for the MLLP Listener (Phase 2).

One row per classified connection: timestamp, peer, classification, the
first 256 bytes (hex + printable), and -- for HL7 traffic only -- the full
message text, the ACK code sent, and MSH-9/MSH-10 for the events table.

Wire fidelity: ``full_message`` is the payload decoded with
``errors="replace"`` (good for the viewer, lossy for invalid UTF-8), so the
exact payload bytes are also stored in the ``raw_message`` BLOB column. For
HL7 and framed-non-HL7 events, ``raw_message`` is the source of truth for
"what was on the wire"; never rebuild it from ``full_message``.

Schema versioning: the schema version lives in SQLite's ``PRAGMA
user_version``. Version 0 is either a brand-new empty file or a legacy
(pre-``raw_message``) database -- told apart by whether the ``events`` table
exists. Migrations are additive only (``ADD COLUMN``), and a legacy file is
copied to ``<db>.bak-<UTC timestamp>`` before it is touched. Backups are
never deleted by this code.

``capture.db`` is a runtime artifact (gitignored), not checked-in data --
see BUILD_PLAN section 9. It can contain the messages you tested with, so
treat it (and its ``.bak-*`` copies) as sensitive; see ``SECURITY.md``.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "capture.db"

#: Schema version this code writes and expects. Bump it together with a new
#: entry in ``_MIGRATIONS`` -- never edit an already-shipped migration.
SCHEMA_VERSION = 1

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
    msh10 TEXT,
    raw_message BLOB
)
"""

#: Upgrade steps keyed by the version they upgrade *from*. Each is a list of
#: statements run inside one transaction together with the version bump, so a
#: failed migration leaves the file at its old version (and the backup on
#: disk). Additive only: never DROP/rename/rewrite, so no row can be lost.
_MIGRATIONS: dict[int, list[str]] = {
    # v0 (legacy, no raw bytes) -> v1: add the exact-payload BLOB. Old rows
    # get NULL: their original bytes were never kept and cannot be invented.
    0: ["ALTER TABLE events ADD COLUMN raw_message BLOB"],
}

_INSERT_SQL = """
INSERT INTO events (
    timestamp, peer_host, peer_port, event_class,
    first_bytes_hex, first_bytes_printable, full_message, ack_code, msh9, msh10,
    raw_message
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _printable(data: bytes) -> str:
    """Render *data* as printable ASCII, non-printable bytes shown as '.'
    -- a quick eyeball view alongside the hex dump."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def _backup_path(db_path: Path) -> Path:
    """``<db>.bak-<UTC timestamp>`` next to the database, never an existing
    file. The timestamp avoids ``:`` because Windows filenames forbid it; a
    counter suffix keeps two migrations in the same second from colliding."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        candidate = db_path.with_name(f"{db_path.name}.bak-{stamp}-{n}")
        n += 1
    return candidate


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _migrate(conn: sqlite3.Connection, db_path: Path) -> None:
    """Bring *conn*'s database up to :data:`SCHEMA_VERSION`.

    * Fresh file (no ``events`` table): create the current schema directly;
      there is nothing to lose, so no backup.
    * Legacy/older file: copy it aside first (SQLite's online backup API, so
      the copy is consistent even if a journal is present), then apply each
      pending migration in one transaction.
    * File written by a *newer* MSHroom: refuse rather than guess -- writing
      to a schema we don't understand could corrupt it.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"{db_path} has schema version {version}, newer than this MSHroom supports ({SCHEMA_VERSION}). "
            "Upgrade MSHroom or point it at a different capture database."
        )

    if version == 0 and not _table_exists(conn, "events"):
        conn.execute(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return

    if version == SCHEMA_VERSION:
        return

    backup = sqlite3.connect(_backup_path(db_path))
    try:
        conn.backup(backup)
    finally:
        backup.close()

    conn.commit()  # end any implicit transaction so BEGIN below is ours
    try:
        conn.execute("BEGIN")
        for step in range(version, SCHEMA_VERSION):
            for statement in _MIGRATIONS[step]:
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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
            try:
                _migrate(self._conn, self.db_path)
            except Exception:
                # Don't leave the file open (and locked on Windows) when a
                # refused/failed migration means no CaptureLog gets built.
                self._conn.close()
                raise

    def record(self, event: Any) -> int:
        """Store one ``hl7kit.mllp.ListenerEvent``. Returns the new row id.

        Accepts anything with the same attributes as ``ListenerEvent``
        (duck-typed) so tests can pass lightweight stand-ins without
        importing ``hl7kit.mllp``.
        """
        first_bytes = bytes(event.first_bytes)[:256]
        # getattr: stand-in events written before raw_frame existed lack it.
        raw_frame = getattr(event, "raw_frame", None)
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
                    bytes(raw_frame) if raw_frame is not None else None,
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
        """One event by id, including its full message text if any and the
        exact payload as ``raw_message`` (``bytes``, or ``None`` for events
        that carry no payload and for rows captured before that column
        existed). Returns ``None`` if no such row exists."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return dict(row) if row is not None else None

    def counts_by_class(self) -> dict[str, int]:
        """Total events seen per classification, for the UI's probe
        counters. Classes never seen are simply absent (callers default
        to 0)."""
        with self._lock:
            rows = self._conn.execute("SELECT event_class, COUNT(*) AS n FROM events GROUP BY event_class").fetchall()
            return {row["event_class"]: row["n"] for row in rows}

    def clear(self) -> None:
        """Delete all rows (keeps the schema). Test helper."""
        with self._lock:
            self._conn.execute("DELETE FROM events")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
