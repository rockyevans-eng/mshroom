"""Command line for ``python -m mshroom run`` (EXPERIMENTAL).

::

    python -m mshroom run <scenario.toml> --target HOST:PORT --listen PORT
        [--listen-host HOST] [--keep-open] [--timeout SECS] [--json PATH]

Exit codes: 0 every step passed; 1 at least one step failed; 2 usage or
infrastructure error (bad scenario or template, scenario not marked
synthetic, return port busy, target unreachable).

Invariant: this module -- and everything it imports -- never imports
pywebview or starts the web app. ``run`` must work on a headless box.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from . import runner
from .loader import DEFAULT_STEP_TIMEOUT, load_scenario
from .model import EXIT_ERROR, ScenarioError
from .report import format_report, write_json

#: Where result files go when ``--json`` is not given.
DEFAULT_RESULTS_DIR = "runs"


def parse_target(text: str) -> tuple[str, int]:
    """Split ``HOST:PORT`` into ``(host, port)``; ``ValueError`` if malformed."""
    host, sep, port_text = text.rpartition(":")
    if not sep or not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError(f"--target must look like HOST:PORT (got {text!r})")
    return host, int(port_text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mshroom run",
        description=(
            "EXPERIMENTAL. Run a scenario against an interface engine: send each step to --target, "
            "listen on --listen for what the engine sends back, and check it."
        ),
    )
    parser.add_argument("scenario", help="path to a scenario .toml file")
    parser.add_argument("--target", required=True, metavar="HOST:PORT", help="the engine's inbound MLLP port")
    parser.add_argument(
        "--listen",
        required=True,
        type=int,
        metavar="PORT",
        help="port MSHroom listens on for the engine's returned messages",
    )
    parser.add_argument("--listen-host", default="0.0.0.0", help="interface to listen on (default 0.0.0.0)")
    parser.add_argument("--keep-open", action="store_true", help="send every step over one persistent connection")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_STEP_TIMEOUT,
        metavar="SECS",
        help=f"default per-step wait for returns when a step sets none (default {DEFAULT_STEP_TIMEOUT:g})",
    )
    parser.add_argument(
        "--json", metavar="PATH", help=f"write the result here (default {DEFAULT_RESULTS_DIR}/<run_id>.json)"
    )
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def main(argv: Optional[list[str]] = None) -> int:
    """Run the ``run`` subcommand; return the process exit code.

    Bad command-line syntax exits through argparse (status 2, its own
    usage message); everything else is returned as a code.
    """
    args = build_parser().parse_args(argv)
    try:
        host, port = parse_target(args.target)
        if args.timeout <= 0:
            raise ValueError("--timeout must be greater than 0")
        scenario = load_scenario(args.scenario, default_timeout=args.timeout)
    except (ValueError, ScenarioError) as exc:
        return _fail(str(exc))

    # Read the grace at call time (not import time) so tests can shorten it.
    config = runner.RunConfig(
        target_host=host,
        target_port=port,
        listen_port=args.listen,
        listen_host=args.listen_host,
        keep_open=args.keep_open,
        grace=runner.EXTRA_RETURN_GRACE_SECONDS,
    )
    result = runner.run_scenario(scenario, config)
    print(format_report(result))
    json_path = Path(args.json) if args.json else Path(DEFAULT_RESULTS_DIR) / f"{result.run_id}.json"
    print(f"Result file: {write_json(result, json_path)}")
    return result.exit_code
