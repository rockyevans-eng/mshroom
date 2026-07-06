"""MSHroom web app: FastAPI routes + static frontend.

The **Viewer** (parse endpoint feeding the tree/raw two-way-highlighting
UI), the **Sender** (corpus list/load + MLLP send endpoint), and the
**Listener** (MLLP server + capture log): it runs in-process on a
background thread, started/stopped via the app's lifespan (real server
runs) or the ``/api/listener/start`` and ``/api/listener/stop`` routes
(manual control, and what the test suite uses -- the app's own lifespan
only fires when something actually enters it, e.g. ``uvicorn`` or
``with TestClient(app) as client:``).

Run locally (from the repo root)::

    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8550

All tree offsets returned by ``/api/parse`` are in **original-text space**
(exactly what the user pasted), translated via
:meth:`hl7kit.parser.Message.to_original_offsets` -- the parser normalizes
line endings internally, but the browser highlights against the pasted text.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field as PydanticField

from app.capture import CaptureLog
from hl7kit import dictionary
from hl7kit.ack import parse_ack
from hl7kit.mllp import DEFAULT_LISTENER_PORT, ListenerEvent, MllpListener, send_message
from hl7kit.parser import (
    Component,
    Field,
    Message,
    Repetition,
    Segment,
    parse_message,
)

BASE_DIR = Path(__file__).resolve().parents[1]
CORPUS_DIR = BASE_DIR / "corpus"
STATIC_DIR = Path(__file__).resolve().parent / "static"
CAPTURE_DB_PATH = BASE_DIR / "capture.db"

capture_log = CaptureLog(CAPTURE_DB_PATH)


def _on_listener_event(event: ListenerEvent) -> None:
    capture_log.record(event)


#: The live Listener instance. Reassigned (not mutated) by
#: ``/api/listener/start`` when asked to bind a different port -- see that
#: route for why.
listener = MllpListener(
    host="0.0.0.0",
    port=int(os.environ.get("HL7_LISTENER_PORT", DEFAULT_LISTENER_PORT)),
    on_event=_on_listener_event,
)
#: Set when the lifespan's auto-start (real server boot) fails to bind --
#: surfaced in /api/listener/status rather than crashing the whole app.
listener_start_error: Optional[str] = None


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    global listener_start_error
    try:
        listener.start()
        listener_start_error = None
    except OSError as exc:
        # Don't let a taken port take the whole Viewer/Sender app down with
        # it -- the Listener is one of three tools, not a prerequisite.
        listener_start_error = f"Could not bind listener port {listener.port}: {exc}"
    yield
    listener.stop()


app = FastAPI(title="MSHroom", version="0.1.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class ParseRequest(BaseModel):
    """Body of POST /api/parse."""

    text: str


class SendRequest(BaseModel):
    """Body of POST /api/send."""

    host: str
    port: int = PydanticField(ge=1, le=65535)
    message: str
    timeout: float = PydanticField(default=10.0, gt=0, le=60)


class ListenerStartRequest(BaseModel):
    """Body of POST /api/listener/start. ``port`` is optional -- an empty
    body just (re)starts the Listener on its currently configured port.
    ``0`` is valid and means "let the OS pick a free port" (plain socket
    semantics), which the test suite uses to avoid port collisions."""

    port: Optional[int] = PydanticField(default=None, ge=0, le=65535)


# ---------------------------------------------------------------------------
# Tree building (parse tree -> JSON the frontend renders)
# ---------------------------------------------------------------------------


def _orig_span(message: Message, start: int, end: int) -> tuple[int, int]:
    """Node offsets in original-(pasted-)text space."""
    return message.to_original_offsets(start, end)


def _leaf_decoded(node: Field | Repetition | Component) -> str:
    """Escape-decoded display value for a node rendered as a leaf.

    Walks down the single-child chain to the subcomponent, which is where
    the parser stores the decoded value. Falls back to raw text if the
    chain is missing (malformed input)."""
    current: Any = node
    if isinstance(current, Field):
        current = current.repetitions[0] if current.repetitions else None
    if isinstance(current, Repetition):
        current = current.components[0] if current.components else None
    if isinstance(current, Component):
        current = current.subcomponents[0] if current.subcomponents else None
    if current is not None and hasattr(current, "decoded"):
        return current.decoded
    return getattr(node, "raw", "")


def _subcomponent_nodes(message: Message, comp: Component, comp_ref: str) -> list[dict[str, Any]]:
    nodes = []
    for sc in comp.subcomponents:
        start, end = _orig_span(message, sc.start, sc.end)
        nodes.append(
            {
                "kind": "subcomponent",
                "ref": f"{comp_ref}.{sc.index}",
                "name": None,
                "value": sc.decoded,
                "raw": sc.raw,
                "start": start,
                "end": end,
                "children": [],
            }
        )
    return nodes


def _component_nodes(message: Message, rep: Repetition, base_ref: str) -> list[dict[str, Any]]:
    nodes = []
    for comp in rep.components:
        comp_ref = f"{base_ref}.{comp.index}"
        start, end = _orig_span(message, comp.start, comp.end)
        children: list[dict[str, Any]] = []
        if len(comp.subcomponents) > 1:
            children = _subcomponent_nodes(message, comp, comp_ref)
        nodes.append(
            {
                "kind": "component",
                "ref": comp_ref,
                "name": None,
                "value": _leaf_decoded(comp) if not children else comp.raw,
                "raw": comp.raw,
                "start": start,
                "end": end,
                "children": children,
            }
        )
    return nodes


def _is_leaf_rep(rep: Repetition) -> bool:
    """A repetition with one component holding one subcomponent has no
    structure worth a deeper tree level."""
    return len(rep.components) == 1 and len(rep.components[0].subcomponents) == 1


def _field_node(message: Message, seg_ref: str, seg_id: str, f: Field) -> dict[str, Any]:
    field_ref = f"{seg_ref}-{f.index}"
    start, end = _orig_span(message, f.start, f.end)
    children: list[dict[str, Any]] = []

    if len(f.repetitions) > 1:
        for rep in f.repetitions:
            rep_ref = f"{field_ref}[{rep.index}]"
            r_start, r_end = _orig_span(message, rep.start, rep.end)
            rep_children: list[dict[str, Any]] = []
            if not _is_leaf_rep(rep):
                rep_children = _component_nodes(message, rep, rep_ref)
            children.append(
                {
                    "kind": "repetition",
                    "ref": rep_ref,
                    "name": None,
                    "value": _leaf_decoded(rep) if not rep_children else rep.raw,
                    "raw": rep.raw,
                    "start": r_start,
                    "end": r_end,
                    "children": rep_children,
                }
            )
    elif f.repetitions and not _is_leaf_rep(f.repetitions[0]):
        children = _component_nodes(message, f.repetitions[0], field_ref)

    return {
        "kind": "field",
        "ref": field_ref,
        "name": dictionary.field_name(seg_id, f.index),
        "value": _leaf_decoded(f) if not children else f.raw,
        "raw": f.raw,
        "start": start,
        "end": end,
        "children": children,
    }


def _segment_node(message: Message, seg: Segment, occurrence: int, total: int) -> dict[str, Any]:
    seg_ref = f"{seg.seg_id}[{occurrence}]" if occurrence > 1 else seg.seg_id
    label = f"{seg.seg_id} #{occurrence}" if total > 1 else seg.seg_id
    start, end = _orig_span(message, seg.start, seg.end)
    return {
        "kind": "segment",
        "ref": seg_ref,
        "label": label,
        "name": None,
        "value": "",
        "raw": seg.raw,
        "start": start,
        "end": end,
        "children": [_field_node(message, seg_ref, seg.seg_id, f) for f in seg.fields],
    }


def build_tree(message: Message) -> list[dict[str, Any]]:
    """Build the JSON tree the frontend renders.

    Every node carries ``ref`` (canonical notation, segment occurrence
    included for repeated segments), ``name`` (dictionary field name or
    null), ``value``/``raw``, and ``start``/``end`` offsets **into the
    original pasted text**. Trivial single-child levels (one repetition,
    one component, one subcomponent) are collapsed so the tree matches
    what an interface analyst expects to see.
    """
    counts: dict[str, int] = {}
    for seg in message.segments:
        counts[seg.seg_id] = counts.get(seg.seg_id, 0) + 1
    seen: dict[str, int] = {}
    nodes = []
    for seg in message.segments:
        seen[seg.seg_id] = seen.get(seg.seg_id, 0) + 1
        nodes.append(_segment_node(message, seg, seen[seg.seg_id], counts[seg.seg_id]))
    return nodes


def _message_summary(message: Message) -> dict[str, Optional[str]]:
    """MSH-9 / MSH-10 style header info for display, best-effort."""

    def raw(ref: str) -> Optional[str]:
        node = message.get(ref)
        value = getattr(node, "raw", None)
        return value if isinstance(value, str) and value else None

    return {
        "message_type": raw("MSH-9"),
        "control_id": raw("MSH-10"),
        "version": raw("MSH-12"),
        "sending_app": raw("MSH-3"),
        "sending_facility": raw("MSH-4"),
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.post("/api/parse")
def api_parse(req: ParseRequest) -> dict[str, Any]:
    """Parse pasted HL7 text into the tree JSON the Viewer renders.

    Never 500s on weird input -- the parser is guaranteed not to raise, so
    even garbage comes back as a (possibly structureless) tree.
    """
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="Message text is empty.")
    message = parse_message(req.text)
    return {
        "ok": True,
        "tree": build_tree(message),
        "summary": _message_summary(message),
        "segment_count": len(message.segments),
    }


@app.get("/api/corpus")
def api_corpus_list() -> dict[str, Any]:
    """List the golden corpus messages available to load into the Sender."""
    files = []
    for path in sorted(CORPUS_DIR.glob("*.hl7")):
        text = path.read_bytes().decode("utf-8", errors="replace")
        message = parse_message(text)
        node = message.get("MSH-9")
        msg_type = getattr(node, "raw", "") or ""
        files.append({"name": path.name, "message_type": msg_type})
    return {"files": files}


@app.get("/api/corpus/{name}")
def api_corpus_load(name: str) -> dict[str, Any]:
    """Return the raw text of one corpus message.

    ``name`` must exactly match a listed ``*.hl7`` file -- checked against
    the actual directory listing, so path tricks (``..``, separators)
    simply never match.
    """
    valid_names = {p.name for p in CORPUS_DIR.glob("*.hl7")}
    if name not in valid_names:
        raise HTTPException(status_code=404, detail=f"No corpus file named {name!r}.")
    text = (CORPUS_DIR / name).read_bytes().decode("utf-8", errors="replace")
    return {"name": name, "text": text}


@app.post("/api/send")
def api_send(req: SendRequest) -> dict[str, Any]:
    """Send a message over MLLP and return the parsed ACK (or a friendly error).

    Always returns 200 with ``ok`` true/false -- a refused connection or
    timeout is a normal test situation (the receiving system may not be
    listening yet), not a server error.
    """
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="Message text is empty.")
    result = send_message(req.host.strip(), req.port, req.message, timeout=req.timeout)
    if not result.ok:
        return {"ok": False, "error": result.error}
    ack = parse_ack(result.response)
    return {
        "ok": True,
        "ack": {
            "code": ack.code,
            "control_id": ack.control_id,
            "text": ack.text,
            "is_accept": ack.is_accept,
            "raw": result.response,
            "tree": build_tree(ack.message),
        },
    }


# ---------------------------------------------------------------------------
# Listener + capture log routes (Phase 2)
# ---------------------------------------------------------------------------


def _listener_status() -> dict[str, Any]:
    return {
        "running": listener.is_running,
        "host": listener.host,
        "port": listener.actual_port,
        "counts": capture_log.counts_by_class(),
        "error": listener_start_error,
    }


@app.get("/api/listener/status")
def api_listener_status() -> dict[str, Any]:
    """Current run state, bound port, and per-class event counters."""
    return _listener_status()


@app.post("/api/listener/start")
def api_listener_start(req: Optional[ListenerStartRequest] = None) -> dict[str, Any]:
    """Start the Listener (no-op if already running).

    If ``port`` is given and differs from the currently configured port,
    a fresh :class:`~hl7kit.mllp.MllpListener` is built for that port and
    replaces the module-level one -- simplest way to let a caller (or a
    test) rebind without restarting the whole app.
    """
    global listener, listener_start_error
    if listener.is_running:
        return _listener_status()
    if req is not None and req.port is not None and req.port != listener.port:
        listener = MllpListener(host=listener.host, port=req.port, on_event=_on_listener_event)
    try:
        listener.start()
        listener_start_error = None
    except OSError as exc:
        listener_start_error = f"Could not bind listener port {listener.port}: {exc}"
    return _listener_status()


@app.post("/api/listener/stop")
def api_listener_stop() -> dict[str, Any]:
    """Stop the Listener (no-op if not running)."""
    listener.stop()
    return _listener_status()


@app.get("/api/listener/events")
def api_listener_events(limit: int = 200) -> dict[str, Any]:
    """Most recent capture-log rows, newest first (see
    :meth:`app.capture.CaptureLog.list_events` for the shape)."""
    return {"events": capture_log.list_events(limit=limit)}


@app.get("/api/listener/events/{event_id}")
def api_listener_event_detail(event_id: int) -> dict[str, Any]:
    """One capture-log row in full, including its parse tree when the
    event was classified as HL7 -- what the Listener page's "open in
    Viewer" link fetches before handing the text to the Viewer."""
    event = capture_log.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"No capture event with id {event_id}.")
    result: dict[str, Any] = {"event": event}
    if event.get("full_message"):
        message = parse_message(event["full_message"])
        result["tree"] = build_tree(message)
        result["summary"] = _message_summary(message)
    return result


# ---------------------------------------------------------------------------
# Pages + static assets
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def page_viewer() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sender", include_in_schema=False)
def page_sender() -> FileResponse:
    return FileResponse(STATIC_DIR / "sender.html")


@app.get("/listener", include_in_schema=False)
def page_listener() -> FileResponse:
    return FileResponse(STATIC_DIR / "listener.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
