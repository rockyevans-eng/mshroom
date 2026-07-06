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

## Pull requests

Open an issue first for anything beyond a small fix so the approach can
be discussed before you invest the time. Keep PRs focused -- one change,
one PR.
