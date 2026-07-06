# Contributing to MSHroom

Thanks for considering a contribution.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .[dev]
.venv\Scripts\python.exe -m pytest -v
```

All tests should pass before and after your change. If you touch
`hl7kit/` or `app/`, add or update a test that would have caught the bug
or covers the new behavior.

## Rules

- **No PHI, ever.** Never include real patient data, real facility names,
  or real message traffic in an issue, a pull request, a fixture, or a
  screenshot. Everything in `corpus/` must be synthetic/fictitious data.
- **No message-type or profile selection.** This is a deliberate product
  decision (see the README) -- MSHroom parses whatever HL7 v2 structure is
  actually present rather than asking the user to pick a type first.
  Contributions that reintroduce a type/profile picker will be declined.
- Keep dependencies minimal. This project intentionally avoids a frontend
  build step and prefers the standard library where reasonable.

## Code style: write for the maintainer who isn't you

This codebase is maintained by people (and tools) of very different skill
levels, so readability is a hard requirement, not a nicety:

- **Every module gets a top docstring** saying what it's for and stating
  any invariant the rest of the code relies on (see `hl7kit/parser.py` for
  the house style -- its offset invariant and MSH-quirk notes are the model).
- **Comment the "why," especially domain logic.** HL7 is full of
  non-obvious rules (MSH-1 *is* the field separator; ACK behavior;
  MLLP framing bytes). Anywhere the code encodes a rule like that, say so
  in a comment -- the next reader may not be an HL7 person.
- **Public functions and classes get docstrings**; parameters that aren't
  self-explanatory get explained.
- **No cleverness without a comment.** If a line took thought to write,
  it needs a sentence saying what it's doing there.
- Follow the formatting of the surrounding code; keep functions small
  enough to read without scrolling.

## Pull requests

Open an issue first for anything beyond a small fix so the approach can
be discussed before you invest the time. Keep PRs focused -- one change,
one PR.
