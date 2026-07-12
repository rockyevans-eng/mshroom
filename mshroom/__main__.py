"""``python -m mshroom`` -- desktop window (default) or headless server.

Two run modes, one codebase:

* **Desktop (default):** uvicorn serves ``app.main:app`` on
  ``127.0.0.1:<OS-assigned port>`` in a background thread, and a native
  window (pywebview) opens on it. Closing the window shuts the server --
  and with it the MLLP listener -- down cleanly.
* **Headless (``--headless [--host H] [--port P]``):** plain
  ``uvicorn`` in the foreground, default ``0.0.0.0:8550`` -- the same
  network-facing behavior as the documented uvicorn command. Ctrl+C /
  SIGTERM shut it down cleanly (uvicorn's own signal handling runs the
  app's lifespan shutdown, which stops the MLLP listener).

Load-bearing invariants:

* ``--headless`` must never import pywebview -- headless boxes (servers,
  containers) may not have a WebView2/GTK runtime installed at all, so
  the import lives inside :func:`run_desktop` only.
* The desktop window binds loopback on an OS-assigned free port: the
  window is the only intended client, and a fixed port would collide
  with a second copy or another app. The *MLLP listener* is unaffected --
  it still binds ``0.0.0.0`` on ``HL7_LISTENER_PORT`` (default 6671) in
  both modes, because receiving from other machines is the point.
"""

from __future__ import annotations

import argparse
import threading
import time

import uvicorn

#: Headless mode defaults: this is a network tool, so the server side
#: binds all interfaces on a stable, documented port.
DEFAULT_HEADLESS_HOST = "0.0.0.0"
DEFAULT_HEADLESS_PORT = 8550

#: Desktop window title and initial size.
WINDOW_TITLE = "MSHroom"
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800


def create_server(host: str, port: int) -> uvicorn.Server:
    """A uvicorn Server for the app, not yet running."""
    config = uvicorn.Config("app.main:app", host=host, port=port, log_level="info")
    return uvicorn.Server(config)


def start_server_thread(
    host: str = "127.0.0.1", port: int = 0, startup_timeout: float = 15.0
) -> tuple[uvicorn.Server, threading.Thread, int]:
    """Start uvicorn on a background thread; return (server, thread, bound port).

    ``port=0`` asks the OS for a free port; the actual port is read back
    from the listening socket once the server reports itself started.
    Raises ``RuntimeError`` if the server dies or fails to start within
    *startup_timeout* seconds. The thread is a daemon on purpose: if
    shutdown ever goes wrong, a stuck server thread must not keep the
    desktop process alive after the window is gone.
    """
    server = create_server(host, port)
    thread = threading.Thread(target=server.run, name="mshroom-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("Server exited during startup (see log output above).")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError(f"Server did not start within {startup_timeout:g}s.")
        time.sleep(0.05)
    bound_port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, bound_port


def stop_server_thread(server: uvicorn.Server, thread: threading.Thread, timeout: float = 10.0) -> bool:
    """Shut the background server down; True if its thread fully exited.

    ``should_exit`` asks uvicorn for a graceful shutdown (runs the app's
    lifespan shutdown, which stops the MLLP listener). If the thread is
    still alive after *timeout*, escalate to ``force_exit`` (skip waiting
    on lingering connections) and wait once more.
    """
    server.should_exit = True
    thread.join(timeout=timeout)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=timeout)
    return not thread.is_alive()


def run_desktop(auto_close_seconds: float | None = None) -> int:
    """Desktop mode: background server on loopback + native window.

    ``auto_close_seconds`` closes the window automatically after that many
    seconds -- only used by the test suite's window smoke test (a human
    never wants this). Returns a process exit code.
    """
    import webview  # deliberately NOT a top-level import -- see module docstring

    server, thread, port = start_server_thread()
    try:
        window = webview.create_window(
            WINDOW_TITLE,
            f"http://127.0.0.1:{port}/",
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
        )
        if auto_close_seconds is not None:

            def _auto_close() -> None:
                time.sleep(auto_close_seconds)
                window.destroy()

            threading.Thread(target=_auto_close, daemon=True).start()
        # Blocks until the window is closed (or destroyed above).
        webview.start()
    finally:
        # Runs on normal close, Ctrl+C in the console, and webview
        # failures alike: the server (and the MLLP listener via the
        # app's lifespan shutdown) must never outlive the window.
        stop_server_thread(server, thread)
    return 0


def run_headless(host: str, port: int) -> int:
    """Headless mode: uvicorn in the foreground, exactly like running the
    documented ``python -m uvicorn app.main:app`` command."""
    server = create_server(host, port)
    server.run()  # installs SIGINT/SIGTERM (and SIGBREAK on Windows) handlers
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mshroom",
        description=(
            "MSHroom -- HL7 v2 test utility. Default: open the desktop window. "
            "With --headless: run the web server for network/service use "
            f"(default {DEFAULT_HEADLESS_HOST}:{DEFAULT_HEADLESS_PORT}). The MLLP listener "
            "binds 0.0.0.0 on HL7_LISTENER_PORT (default 6671) in both modes."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run the web server without a window (for servers/services)",
    )
    parser.add_argument("--host", default=None, help="headless only: interface to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="headless only: web UI port (default 8550)")
    args = parser.parse_args(argv)

    if not args.headless:
        if args.host is not None or args.port is not None:
            # In desktop mode the server exists only for the window, on a
            # loopback port nobody needs to know -- a --host/--port here
            # is almost certainly someone who meant --headless.
            parser.error("--host/--port only apply to --headless mode")
        return run_desktop()

    host = args.host if args.host is not None else DEFAULT_HEADLESS_HOST
    port = args.port if args.port is not None else DEFAULT_HEADLESS_PORT
    return run_headless(host, port)


if __name__ == "__main__":
    raise SystemExit(main())
