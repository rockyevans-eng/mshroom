"""Scenario runner (EXPERIMENTAL): automated tests of an interface engine.

MSHroom sends an original message to an engine, the engine (possibly
transforming it) forwards the result to a return port MSHroom listens on,
and MSHroom checks what came back. The first scenarios are *passthrough*
(what returns must equal what was sent); small transformation exercises
build on the same machinery.

Modules, in dependency order:

* :mod:`~mshroom.scenario.model` -- dataclasses for scenarios and results.
* :mod:`~mshroom.scenario.assertions` -- pure message comparison / checks.
* :mod:`~mshroom.scenario.profiles` -- what "correct" means per step mode.
* :mod:`~mshroom.scenario.loader` -- TOML + templates to a validated scenario.
* :mod:`~mshroom.scenario.runner` -- sends steps, collects and judges returns.
* :mod:`~mshroom.scenario.report` -- console report and JSON result file.
* :mod:`~mshroom.scenario.cli` -- ``python -m mshroom run``.

Invariant: nothing in this package imports pywebview or the web app, so the
runner works anywhere the standard library and ``hl7kit`` do. The format
is v0 and may change; see ``docs/scenarios.md``.
"""
