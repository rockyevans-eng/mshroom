# MSHroom

[![CI](https://github.com/rockyevans-eng/mshroom/actions/workflows/ci.yml/badge.svg)](https://github.com/rockyevans-eng/mshroom/actions/workflows/ci.yml)

**MSHroom** is a free, open-source HL7 v2 test utility. Paste a message and
see it as a tree, fire it at an interface engine over MLLP, and stand up
your own listener to catch and inspect whatever hits a port. It's built for
the "I need to check what's in this message" moment, without ever asking
you what kind of message it is first.

**MLLP** (Minimal Lower Layer Protocol) is the thin TCP wrapper healthcare
systems use to move HL7 v2 messages between interface engines: a start
byte, the message, two end bytes. MSHroom speaks it in both directions --
as the client sending messages and as the server receiving them.

The name is the joke: `MSH` is the first three characters of every HL7 v2
message.

## What it does

Three tools, one small FastAPI + vanilla-JS app:

- **Viewer** -- paste an HL7 v2 message (or load a bundled sample) and get
  a tree on the left, raw text on the right. Click a tree node (say,
  `PID-5`) and its exact characters highlight in the raw text; click or
  select text in the raw pane and the tree jumps to (and flashes) the
  matching node. There is no message-type or profile picker anywhere --
  paste anything and it parses, figures out its own structure, and lets
  you drill into whatever segments and repetitions are actually there.
- **Sender** -- an MLLP client. Point it at a host and port, load a sample
  or paste your own message, send it, and see the returned ACK rendered
  as its own mini-tree. A refused connection or timeout comes back as a
  plain-English message, never a stack trace.
- **Listener** -- an MLLP server. It starts automatically with the app,
  listens on a configurable port, ACKs real HL7 traffic, and logs every
  connection it sees. Critically, it never treats a connection as an HL7
  message until it has actually verified that it is one: port scans, stray
  HTTP requests, TLS handshakes, and plain junk are each classified and
  logged (never parsed, never answered) instead of being blindly fed to
  the parser. Click any logged HL7 row to open it straight in the Viewer.

## Quickstart

Requires Python 3.12+.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .[dev]
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8550
```

This is a network tool, and it binds accordingly:

- The web UI is at **http://\<this-machine's-IP\>:8550/** from any machine
  on your network (or `http://127.0.0.1:8550/` from the same box).
  `/sender` and `/listener` are the other two tools; `/docs` is the built-in
  API documentation.
- The **Listener accepts MLLP from external clients** -- point your
  interface engine, or another copy of MSHroom, at
  `<this-machine's-IP>:6671`. The port is configurable without a code
  change: set `HL7_LISTENER_PORT` before starting.
- If nothing can reach you, it's almost always the OS firewall: allow
  inbound TCP on 8550 and 6671 (the planned installer will offer to do
  this for you).

This is today's run mode. A native desktop shell (one window, tabs across
the top, no browser tab required) is planned -- see Roadmap below.

## Running the tests

```powershell
.venv\Scripts\python.exe -m pytest -v
```

All tests should pass. Each test name identifies the invariant it checks
(round-trip fidelity, offset correctness, a specific field value, a
malformed/probe input, an API route, an MLLP behavior, or -- for
`test_listener.py` -- which row of the Listener's classification table).

### Adding sample messages

`corpus/` is the library of built-in sample messages -- the ones in the
Viewer's dropdown. Every sample is completely fake (synthetic patients,
made-up facilities), and each comes with a small "answer key" file
(`*.expected.json`) listing what a few fields should contain, so the test
suite can prove the parser reads every sample correctly.

To add a sample: drop a new `.hl7` file in `corpus/` (segments separated by
real carriage returns, fake data only) and write a matching
`*.expected.json` -- copy any existing one to see the format. The tests
pick up new files automatically. `corpus/malformed/` and `corpus/probes/`
hold intentionally broken and non-HL7 inputs used to prove the parser and
Listener survive garbage; the same fake-data-only rule applies.

## Roadmap

- **V1 (current):** the three tabs above -- Viewer, Sender, Listener.
- **V1.1:** message editing and edit-and-resend from the Viewer/Sender.
- **V2:** multiple named inbound/outbound interfaces, plus a data-mapping
  workspace for documenting field-to-database mappings across systems.
- **V3:** a synthetic patient/episode generator (public-domain characters
  and storylines) for building realistic multi-message test scenarios.

## Screenshots

_(placeholder -- screenshots of the Viewer, Sender, and Listener tabs go
here)_

## Contributing

See `CONTRIBUTING.md`.

## Security

See `SECURITY.md`. Never include real patient data anywhere in this
project -- issues, fixtures, screenshots, or otherwise.

## License

Apache-2.0 -- see `LICENSE` and `NOTICE`.
