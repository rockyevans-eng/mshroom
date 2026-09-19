"""Wire-fidelity tests: the bytes a sender put on the wire must survive the
whole receive path exactly, and hostile bytes must never crash it.

Covers (all against real sockets, a real SQLite file, and the real FastAPI
routes -- no mocks):

* ``ListenerEvent.raw_frame`` / ``capture.db`` ``raw_message`` / the
  event-detail API's ``raw_hex`` all equal the exact payload bytes, even
  when the payload holds NUL, other C0 control characters, ESC, and invalid
  UTF-8 (which the decoded ``full_message`` necessarily mangles).
* Those payloads still parse and render (tree + summary) via ``/api/parse``.
* The versioned capture-db migration upgrades a legacy database (one that
  predates ``raw_message``) with zero row loss, after taking a backup.

All identifiers are fictional (``MSHROOM-TEST-`` prefix).
"""

from __future__ import annotations

import socket
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.capture import SCHEMA_VERSION, CaptureLog
from app.main import app
from hl7kit.mllp import MllpListener, frame

client = TestClient(app)  # no `with`: the app's own lifespan (auto-start) never fires

# Every C0 control byte except the MLLP framing bytes: 0x0B (start block) and
# 0x1C (end block) would legitimately end/start the frame mid-payload, so a
# sender cannot include them raw. 0x0D is left out here because the message
# below already uses it as the HL7 segment separator.
_C0_NO_FRAMING = bytes(b for b in range(0x20) if b not in (0x0B, 0x1C, 0x0D))

# NUL, every other C0 control char, ESC (0x1b, inside _C0_NO_FRAMING), a
# truncated UTF-8 lead byte, stray continuation bytes / 0xFF (never valid
# UTF-8), and markup that must render as text in the browser.
HOSTILE_MESSAGE = (
    b"MSH|^~\\&|MSHROOM-TEST-APP|MSHROOM-TEST-FAC|||20260101000000||ADT^A01|MSHROOM-TEST-0001|P|2.5.1\r"
    b"PID|1||MSHROOM-TEST-0001||DOE^JANE^" + _C0_NO_FRAMING + b"\xc3\xff\xfe\x80\xc3"
    b"|||<script>alert(1)</script>&amp;\r"
)


def _wait_until(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(0.02)
        result = predicate()
    return result


def _send(port: int, wire: bytes, read_reply: bool = True) -> None:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(wire)
        if read_reply:
            sock.recv(65536)
    finally:
        sock.close()


@pytest.fixture
def capture_log(tmp_path):
    log = CaptureLog(tmp_path / "fidelity.db")
    yield log
    log.close()


@pytest.fixture
def running_listener(capture_log):
    listener = MllpListener(host="127.0.0.1", port=0, on_event=capture_log.record, idle_timeout=5.0)
    listener.start()
    yield listener, capture_log
    listener.stop()


@pytest.fixture
def app_listener():
    """app.main's own listener + capture log, started on an ephemeral port,
    for tests that go through the real /api/listener/* routes."""
    import app.main as main_module

    main_module.capture_log.clear()
    if main_module.listener.is_running:
        main_module.listener.stop()
    port = client.post("/api/listener/start", json={"port": 0}).json()["port"]
    yield port, main_module.capture_log
    if main_module.listener.is_running:
        main_module.listener.stop()
    main_module.capture_log.clear()


def _first_event_id(log: CaptureLog) -> int:
    events = _wait_until(lambda: log.list_events(limit=1))
    assert events, "no capture-log event arrived in time"
    return events[0]["id"]


# ---------------------------------------------------------------------------
# Raw bytes: listener -> capture.db
# ---------------------------------------------------------------------------


def test_hostile_message_is_stored_byte_exact(running_listener):
    listener, log = running_listener
    _send(listener.actual_port, frame(HOSTILE_MESSAGE))

    row = log.get_event(_first_event_id(log))
    assert row["event_class"] == "HL7"
    assert row["ack_code"] == "AA"
    assert row["raw_message"] == HOSTILE_MESSAGE
    # The decoded text is the lossy display copy: it proves why raw_message
    # exists (stored text != wire) rather than being the thing we trust.
    assert "\ufffd" in row["full_message"]
    assert row["full_message"].encode("utf-8") != HOSTILE_MESSAGE


def test_raw_message_is_not_capped_at_256_bytes(running_listener):
    listener, log = running_listener
    body = b"MSH|^~\\&|A|B|||20260101||ADT^A01|MSHROOM-TEST-0002|P|2.5.1\rNTE|1||" + b"x" * 5000 + b"\r"
    _send(listener.actual_port, frame(body))

    row = log.get_event(_first_event_id(log))
    assert len(row["raw_message"]) == len(body) > 256
    assert row["raw_message"] == body
    assert len(row["first_bytes_hex"]) == 512  # the eyeball preview stays capped at 256 bytes


def test_non_hl7_payload_keeps_raw_bytes(running_listener):
    listener, log = running_listener
    payload = b"\xff\x00not-hl7\x1b[0m\xc3"
    _send(listener.actual_port, frame(payload), read_reply=False)

    row = log.get_event(_first_event_id(log))
    assert row["event_class"] == "NON_HL7_PAYLOAD"
    assert row["raw_message"] == payload
    assert row["full_message"] is None


def test_probe_events_carry_no_raw_message(running_listener):
    listener, log = running_listener
    _send(listener.actual_port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", read_reply=False)

    row = log.get_event(_first_event_id(log))
    assert row["event_class"] == "HTTP_PROBE"
    assert row["raw_message"] is None


def test_record_accepts_event_without_raw_frame_attribute(capture_log):
    """Duck-typed stand-ins written before raw_frame existed still record."""
    stand_in = SimpleNamespace(
        timestamp=datetime.now(timezone.utc),
        peer_host="127.0.0.1",
        peer_port=1,
        event_class="JUNK",
        first_bytes=b"abc",
        full_message=None,
        ack_code=None,
        msh9=None,
        msh10=None,
    )
    row_id = capture_log.record(stand_in)
    assert capture_log.get_event(row_id)["raw_message"] is None


# ---------------------------------------------------------------------------
# Raw bytes + illegal chars: listener -> capture -> detail API -> /api/parse
# ---------------------------------------------------------------------------


def test_hostile_message_full_pipeline_via_api(app_listener):
    port, log = app_listener
    _send(port, frame(HOSTILE_MESSAGE))
    event_id = _first_event_id(log)

    detail_resp = client.get(f"/api/listener/events/{event_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()

    # Exact wire bytes, as hex, alongside the existing fields.
    assert detail["event"]["raw_hex"] == HOSTILE_MESSAGE.hex()
    assert "raw_message" not in detail["event"]  # bytes never leak into JSON
    assert detail["event"]["event_class"] == "HL7"
    assert detail["event"]["full_message"]
    # The detail route's own tree/summary render for the hostile message.
    assert detail["summary"]["message_type"] == "ADT^A01"
    assert detail["summary"]["control_id"] == "MSHROOM-TEST-0001"
    assert [n["ref"] for n in detail["tree"]][:2] == ["MSH", "PID"]

    # The Viewer's path: feed the stored text back through /api/parse.
    parse_resp = client.post("/api/parse", json={"text": detail["event"]["full_message"]})
    assert parse_resp.status_code == 200
    parsed = parse_resp.json()
    assert parsed["ok"] is True
    # 3, not 2: the control-char run contains a raw 0x0A, which the parser
    # (by design) accepts as a segment separator like 0x0D.
    assert parsed["segment_count"] == 3
    assert parsed["summary"]["message_type"] == "ADT^A01"
    assert parsed["summary"]["control_id"] == "MSHROOM-TEST-0001"
    assert [n["ref"] for n in parsed["tree"]][:2] == ["MSH", "PID"]


def test_markup_and_ampersand_survive_as_plain_text(app_listener):
    """The API hands the browser the literal characters (JSON-escaped, never
    HTML); the JS renders them with textContent/createTextNode, so a message
    with <script> or & shows as text. This pins the data side of that."""
    port, log = app_listener
    _send(port, frame(HOSTILE_MESSAGE))
    detail = client.get(f"/api/listener/events/{_first_event_id(log)}").json()
    assert "<script>alert(1)</script>&amp;" in detail["event"]["full_message"]
    values = []

    def walk(nodes):
        for node in nodes:
            values.append(node["raw"])
            walk(node["children"])

    walk(detail["tree"])
    assert any("<script>alert(1)</script>&amp;" in v for v in values)


def test_non_hl7_detail_has_raw_hex_and_no_tree(app_listener):
    port, log = app_listener
    payload = b"\x00\x01\xffnot hl7"
    _send(port, frame(payload), read_reply=False)
    detail = client.get(f"/api/listener/events/{_first_event_id(log)}").json()
    assert detail["event"]["raw_hex"] == payload.hex()
    assert "tree" not in detail


def test_event_list_shape_is_unchanged(app_listener):
    port, log = app_listener
    _send(port, frame(HOSTILE_MESSAGE))
    _first_event_id(log)
    events = client.get("/api/listener/events").json()["events"]
    assert set(events[0]) == {
        "id",
        "timestamp",
        "peer_host",
        "peer_port",
        "event_class",
        "first_bytes_hex",
        "first_bytes_printable",
        "ack_code",
        "msh9",
        "msh10",
        "has_message",
    }


@pytest.mark.parametrize(
    "text",
    [
        "MSH|^~\\&|A|B|||20260101||ADT^A01|MSHROOM-TEST-0003|P|2.5.1\rPID|1||x\x00y\x01\x1bz\ufffd\r",
        "\x00\x01\x02\x1b",
        "MSH|\x00|\x1b|\ufffd",
    ],
)
def test_parse_never_raises_on_control_characters(text):
    resp = client.post("/api/parse", json={"text": text})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Versioned migration of capture.db
# ---------------------------------------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE events (
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


def _make_legacy_db(path, rows: int = 3) -> list[tuple]:
    """A capture.db exactly as the pre-raw_message code created it."""
    conn = sqlite3.connect(path)
    conn.execute(_LEGACY_SCHEMA)
    for i in range(rows):
        conn.execute(
            "INSERT INTO events (timestamp, peer_host, peer_port, event_class, first_bytes_hex, "
            "first_bytes_printable, full_message, ack_code, msh9, msh10) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"2026-01-0{i + 1}T00:00:00+00:00",
                "127.0.0.1",
                1000 + i,
                "HL7",
                "4d5348",
                "MSH",
                f"MSH|msg{i}",
                "AA",
                "ADT^A01",
                f"MSHROOM-TEST-{i}",
            ),
        )
    conn.commit()
    snapshot = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    conn.close()
    return snapshot


def _backups(path):
    return sorted(path.parent.glob(path.name + ".bak-*"))


def test_legacy_db_upgrades_with_zero_row_loss_and_backup(tmp_path):
    db = tmp_path / "capture.db"
    before = _make_legacy_db(db)

    log = CaptureLog(db)
    try:
        # Every legacy row is intact; the new column is appended, NULL.
        raw = sqlite3.connect(db)
        try:
            after = raw.execute("SELECT * FROM events ORDER BY id").fetchall()
            assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            raw.close()
        assert [row[:-1] for row in after] == before
        assert all(row[-1] is None for row in after)

        # And it keeps working: new rows carry raw bytes, old rows read back.
        new_id = log.record(
            SimpleNamespace(
                timestamp=datetime.now(timezone.utc),
                peer_host="127.0.0.1",
                peer_port=9,
                event_class="HL7",
                first_bytes=b"MSH",
                full_message="MSH|new",
                ack_code="AA",
                msh9="ADT^A01",
                msh10="MSHROOM-TEST-NEW",
                raw_frame=b"MSH|new\xff\x00",
            )
        )
        assert log.get_event(new_id)["raw_message"] == b"MSH|new\xff\x00"
        assert log.get_event(1)["full_message"] == "MSH|msg0"
        assert len(log.list_events()) == len(before) + 1
    finally:
        log.close()

    # One backup, taken before migrating: legacy shape, all legacy rows.
    backups = _backups(db)
    assert len(backups) == 1
    bak = sqlite3.connect(backups[0])
    try:
        assert bak.execute("PRAGMA user_version").fetchone()[0] == 0
        cols = [r[1] for r in bak.execute("PRAGMA table_info(events)")]
        assert "raw_message" not in cols
        assert bak.execute("SELECT * FROM events ORDER BY id").fetchall() == before
    finally:
        bak.close()


def test_reopening_migrated_db_makes_no_second_backup(tmp_path):
    db = tmp_path / "capture.db"
    _make_legacy_db(db, rows=1)
    CaptureLog(db).close()
    CaptureLog(db).close()
    assert len(_backups(db)) == 1


def test_fresh_db_is_created_at_current_version_without_backup(tmp_path):
    db = tmp_path / "capture.db"
    CaptureLog(db).close()
    raw = sqlite3.connect(db)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "raw_message" in [r[1] for r in raw.execute("PRAGMA table_info(events)")]
    finally:
        raw.close()
    assert _backups(db) == []


def test_db_from_a_newer_mshroom_is_refused_untouched(tmp_path):
    db = tmp_path / "capture.db"
    _make_legacy_db(db, rows=1)
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="newer"):
        CaptureLog(db)
    assert _backups(db) == []
    raw = sqlite3.connect(db)
    try:
        assert raw.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        raw.close()
