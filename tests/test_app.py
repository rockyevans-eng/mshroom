"""Tests for the FastAPI routes in app.main (Phase 1: Viewer + Sender).

Uses FastAPI's TestClient (httpx). The /api/send tests run against the
same stub MLLP server used in test_mllp.py -- no external services.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_mllp import StubMllpServer

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"

client = TestClient(app)


def _corpus_text(name: str) -> str:
    return (CORPUS_DIR / name).read_bytes().decode("utf-8")


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


# ---------------------------------------------------------------------------
# POST /api/parse
# ---------------------------------------------------------------------------


def test_parse_returns_tree_with_summary():
    resp = client.post("/api/parse", json={"text": _corpus_text("adt_a01.hl7")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["summary"]["message_type"] == "ADT^A01"
    assert data["summary"]["control_id"] == "CTRL0001"
    seg_refs = [n["ref"] for n in data["tree"]]
    assert seg_refs[0] == "MSH"
    assert "PID" in seg_refs


def test_parse_every_node_offset_slices_original_text():
    """The load-bearing invariant: each node's (start, end) must slice the
    ORIGINAL posted text to exactly the node's raw value."""
    text = _corpus_text("oru_r01.hl7")
    data = client.post("/api/parse", json={"text": text}).json()
    checked = 0
    for node in _walk(data["tree"]):
        assert text[node["start"] : node["end"]] == node["raw"], node["ref"]
        checked += 1
    assert checked > 50  # sanity: the walk really covered the tree


def test_parse_offsets_map_back_to_pasted_crlf_text():
    """Paste with \\r\\n line endings: offsets must point into the pasted
    text (via to_original_offsets), not the normalized text."""
    original = _corpus_text("adt_a01.hl7").replace("\r", "\r\n")
    data = client.post("/api/parse", json={"text": original}).json()
    for node in _walk(data["tree"]):
        assert original[node["start"] : node["end"]] == node["raw"], node["ref"]


def test_parse_repeated_segments_labeled_and_referenced_distinctly():
    """ORU^R01 has four OBX segments: labels 'OBX #1'..'OBX #4', refs
    'OBX', 'OBX[2]', 'OBX[3]', 'OBX[4]'."""
    data = client.post("/api/parse", json={"text": _corpus_text("oru_r01.hl7")}).json()
    obx = [n for n in data["tree"] if n["ref"].startswith("OBX")]
    assert [n["label"] for n in obx] == ["OBX #1", "OBX #2", "OBX #3", "OBX #4"]
    assert [n["ref"] for n in obx] == ["OBX", "OBX[2]", "OBX[3]", "OBX[4]"]
    # Field refs inside a repeated segment carry the segment occurrence.
    obx2_field_refs = [f["ref"] for f in obx[1]["children"]]
    assert "OBX[2]-5" in obx2_field_refs


def test_parse_singleton_segment_label_has_no_counter():
    data = client.post("/api/parse", json={"text": _corpus_text("adt_a01.hl7")}).json()
    pid = next(n for n in data["tree"] if n["ref"] == "PID")
    assert pid["label"] == "PID"


def test_parse_field_nodes_carry_dictionary_names():
    data = client.post("/api/parse", json={"text": _corpus_text("adt_a01.hl7")}).json()
    pid = next(n for n in data["tree"] if n["ref"] == "PID")
    pid5 = next(f for f in pid["children"] if f["ref"] == "PID-5")
    assert pid5["name"] == "Patient Name"


def test_parse_component_refs_use_canonical_notation():
    data = client.post("/api/parse", json={"text": _corpus_text("adt_a01.hl7")}).json()
    pid = next(n for n in data["tree"] if n["ref"] == "PID")
    pid5 = next(f for f in pid["children"] if f["ref"] == "PID-5")
    comp_refs = [c["ref"] for c in pid5["children"]]
    assert "PID-5.1" in comp_refs
    for ref in comp_refs:
        assert "^" not in ref


def test_parse_empty_text_is_422():
    assert client.post("/api/parse", json={"text": "   "}).status_code == 422


def test_parse_garbage_still_returns_tree():
    resp = client.post("/api/parse", json={"text": "GET / HTTP/1.1\r\nHost: x\r\n"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# GET /api/corpus + /api/corpus/{name}
# ---------------------------------------------------------------------------


def test_corpus_list():
    data = client.get("/api/corpus").json()
    names = [f["name"] for f in data["files"]]
    assert "adt_a01.hl7" in names
    assert "oru_r01.hl7" in names
    assert len(names) >= 6
    adt = next(f for f in data["files"] if f["name"] == "adt_a01.hl7")
    assert adt["message_type"] == "ADT^A01"


def test_corpus_load():
    data = client.get("/api/corpus/adt_a01.hl7").json()
    assert data["name"] == "adt_a01.hl7"
    assert data["text"].startswith("MSH|^~\\&|REGISTRATION")


def test_corpus_load_unknown_is_404():
    assert client.get("/api/corpus/nope.hl7").status_code == 404


def test_corpus_load_rejects_path_tricks():
    # Traversal-looking names simply don't match any listed corpus file.
    assert client.get("/api/corpus/..%2Fpyproject.toml").status_code == 404
    assert client.get("/api/corpus/malformed%2Fbad_msh.hl7").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/send (against a local stub MLLP server)
# ---------------------------------------------------------------------------


def test_send_returns_parsed_aa_ack():
    server = StubMllpServer()
    try:
        resp = client.post(
            "/api/send",
            json={
                "host": "127.0.0.1",
                "port": server.port,
                "message": _corpus_text("adt_a01.hl7"),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        ack = data["ack"]
        assert ack["code"] == "AA"
        assert ack["is_accept"] is True
        assert ack["control_id"] == "CTRL0001"
        # The ACK comes back as a renderable mini-tree.
        seg_refs = [n["ref"] for n in ack["tree"]]
        assert seg_refs == ["MSH", "MSA"]
    finally:
        server.close()


def test_send_connection_refused_is_friendly_json():
    # A socket that is bound but never listens: connections to it are
    # refused, and holding it open for the duration of the request stops
    # the OS from handing the same port to the outgoing client socket
    # (the Linux self-connect flake a close-then-connect approach hits).
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    dead_port = blocker.getsockname()[1]
    try:
        resp = client.post(
            "/api/send",
            json={"host": "127.0.0.1", "port": dead_port, "message": "MSH|^~\\&|A|B", "timeout": 5},
        )
        assert resp.status_code == 200  # a refused connection is not a server error
        data = resp.json()
        assert data["ok"] is False
        assert "refused" in data["error"].lower()
        assert "Traceback" not in data["error"]
    finally:
        blocker.close()


def test_send_empty_message_is_422():
    resp = client.post("/api/send", json={"host": "127.0.0.1", "port": 6661, "message": " "})
    assert resp.status_code == 422


def test_send_invalid_port_is_422():
    resp = client.post("/api/send", json={"host": "x", "port": 0, "message": "MSH|"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Pages + static assets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/sender"])
def test_pages_serve_html(path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "MSHroom" in resp.text


@pytest.mark.parametrize(
    "asset", ["/static/style.css", "/static/tree.js", "/static/viewer.js", "/static/sender.js"]
)
def test_static_assets_serve(asset):
    assert client.get(asset).status_code == 200
