# Scenario runner (EXPERIMENTAL)

> **Status: experimental, format v0.** The command line, the scenario file
> format and the result file may change without notice while the first
> scenarios are being exercised.

The scenario runner is an automated test of an interface engine. MSHroom
sends an *original* message to the engine, the engine (possibly changing it)
forwards the result to a **return port** that MSHroom listens on, and MSHroom
checks the message that comes back.

```
MSHroom  --send-->  engine under test  --forward-->  MSHroom (return listener)
   ^                                                        |
   +----------------------- verdict <-----------------------+
```

The engine is unknown: any engine that can receive MLLP and forward MLLP will
do. MSHroom never uses an engine's admin interface; it only speaks MLLP.
(Tested with an in-process stub engine in the test suite; engine-specific
setup notes, if any, belong in this section under "Tested with".)

The first scenarios are **passthrough**: what comes back must equal what was
sent. Later scenarios add small transformations (the engine must, say,
upper-case a name) as *exercises*.

## Running one

```powershell
python -m mshroom run corpus/scenarios/adt_lifecycle/scenario.toml `
    --target 127.0.0.1:6661 --listen 6662
```

| Option | Meaning |
| --- | --- |
| `--target HOST:PORT` | The engine's inbound MLLP port (where steps are sent). |
| `--listen PORT` | The port MSHroom listens on for the engine's returned messages. |
| `--listen-host HOST` | Interface for that listener (default `0.0.0.0`). |
| `--keep-open` | Send every step over one persistent connection instead of one connection per step. |
| `--timeout SECS` | Default per-step wait for returned messages, for steps that set no `timeout` of their own (default 10). |
| `--json PATH` | Write the result file here (default `runs/<run_id>.json`). |

Set the engine up so that the channel listening on `--target` forwards its
output to `--listen`. `python -m mshroom run` never starts the web app or the
desktop window and never imports the desktop-window library, so it works on
headless machines.

**Exit codes:** `0` every step passed; `1` at least one step failed; `2`
usage or infrastructure problem (bad scenario or template, scenario not
marked synthetic, return port already in use, target refused or unresolvable).

## Scenario file

A scenario is a TOML file plus one HL7 template per step.

```toml
[scenario]
name = "adt_lifecycle"
description = "Register, admit, transfer and discharge one fictional patient."
synthetic = true            # REQUIRED. Without it the runner refuses to run (exit 2).
correlate_on = "MSH-10"     # optional; the field that carries the run token (default MSH-10)

[patient]                   # fictional demographics, available to templates
identifier = "MSHROOM-TEST-0001"   # REQUIRED; must start with MSHROOM-TEST-
family = "HOLMES"
given = "SHERLOCK"

[[step]]
name = "register"           # shown in the report
label = "registered"        # free text, display only
template = "a04_register.hl7"   # path relative to this file
returns = 1                 # messages expected back (default 1; 0 for filter tests)
timeout = 10                # seconds to wait for them (default 10, or --timeout)
mode = "passthrough"        # or "assert" (default passthrough)
ignore = ["MSH-3", "MSH-4", "MSH-5", "MSH-6", "MSH-7"]   # default shown

[[step.expect]]             # optional, any number
field = "PID-5.1"
equals = "HOLMES"           # or: regex = "..." | present = true
```

Unknown keys are errors (a misspelled `returns` must not silently become the
default). Steps run one after another, in file order.

### Templates

A template is an ordinary HL7 message in a text file, checked in with the
scenario. Line endings do not matter; the runner sends `\r` between segments.
`{{...}}` placeholders are replaced before sending:

| Placeholder | Value |
| --- | --- |
| `{{token}}` | The run token, `<run_id>-<step_no>`, unique per run and step. |
| `{{run_id}}` | The run id alone, e.g. `MSHR20260919101500a1b2c3`. |
| `{{step}}` | The 1-based step number. |
| `{{now}}` | The current time as an HL7 timestamp (`yyyyMMddHHmmss`, UTC). |
| `{{patient.<key>}}` | A value from the `[patient]` table. |

An unknown placeholder is a load error. The template must put `{{token}}`
into the `correlate_on` field (usually MSH-10); this is checked when the
scenario loads. If a template has a PID segment, its PID-3 must start with
`MSHROOM-TEST-`. Patient values may not contain HL7 delimiters
(`| ^ ~ \ &`) or line breaks.

### Modes

* **`passthrough`** (default): the returned message must equal the sent one
  field for field, except the `ignore` list. The default ignore list is
  MSH-3 to MSH-7, because engines rewrite routing and timestamp fields. An
  explicit `ignore = [...]` *replaces* the default (`ignore = []` compares
  everything). Entries name a field (`MSH-7`) or a whole segment (`PV1`).
  Trailing empty fields count as equal to missing ones.
* **`assert`**: no whole-message comparison; only the `[[step.expect]]`
  entries are checked. `regex` must match the whole field text
  (`re.fullmatch`). `present = true` means non-empty, `false` means absent or
  empty. A field reference without repetition or component (`PID-5`) means the
  entire field; `PID-5.1` means one component.

`expect` entries are also honoured in passthrough mode.

## How a step is judged

1. The message is sent; the immediate ACK must have code `AA`.
2. MSHroom waits up to `timeout` seconds for exactly `returns` messages on
   the return port that carry *this step's* token. After they arrive it keeps
   listening for about one more second to catch extras.
3. Each returned message is compared or asserted per the step's `mode`.

Nothing is ever matched by guessing. Every failure has a name and belongs to
one step:

| Code | Meaning |
| --- | --- |
| `NO_ACK` | No ACK came back for the sent message (timeout, connection closed). |
| `ACK_NOT_AA` | The ACK code was not `AA` (or the reply was not an ACK). Return checks are skipped for that step. |
| `MISSING_RETURN` | Fewer than `returns` correlated messages arrived in time. |
| `EXTRA_RETURN` | More than `returns` correlated messages arrived (a duplicate, or anything at all when `returns = 0`). |
| `UNCORRELATED` | A message arrived whose `correlate_on` field has no run token (the engine replaced the control ID). It is never counted. |
| `LATE_OR_DUPLICATE` | A message arrived carrying another step's token, or another run's. It is never counted. |
| `ASSERTION_FAILED` | An `expect` entry did not hold (reports field, expected, actual). |
| `PASSTHROUGH_DIFF` | Passthrough comparison found differences (lists each field with sent and returned values). |

Non-HL7 traffic on the return port (port scans, HTTP probes, junk) is not
judged; it is counted per step in the result file (`non_hl7_events`).

## Result file

Unless `--json` says otherwise, the full result is written to
`runs/<run_id>.json` (the directory is created). It records, per step, the
message sent, the ACK, every message that came back with its role
(`correlated`, `late`, `uncorrelated`), the failures, and the counts of
non-HL7 events. The top-level `schema` value is `mshroom-scenario-result/0`.

### Synthetic-data guard

The runner is for fictional data. If a returned message's PID-3 does not
start with `MSHROOM-TEST-` (or it has no PID-3 at all), MSHroom cannot show it
is fictional and treats it as possible real patient data: its body is **not**
stored (the file keeps only a SHA-256 hash and `"synthetic": false`), values
from it are replaced by `<redacted: non-synthetic data>` in the report and in
failures, `non_synthetic_data_seen` is set to `true`, and a warning is
printed. Never point the runner at an engine carrying real patient traffic.

## Shipped scenarios

* `corpus/scenarios/adt_lifecycle/` -- passthrough of a four-message
  patient lifecycle (register `ADT^A04`, admit `A01`, transfer `A02`,
  discharge `A03`; HL7 v2.5.1 with EVN, PID and PV1) for one fictional
  patient.
