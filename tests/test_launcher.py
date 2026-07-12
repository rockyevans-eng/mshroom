"""Tests for the ``python -m mshroom`` launcher (desktop + headless modes).

What runs where:

* The server-thread helpers and the headless subprocess tests run
  everywhere, CI included -- real uvicorn, real sockets, no mocks.
* ``test_desktop_window_opens_and_exits`` opens a REAL pywebview window,
  so it is guarded by :func:`_webview_skip_reason`: it is skipped when
  pywebview isn't importable, on Linux without a display, and on Windows
  without the WebView2 runtime (registry check). On a normal Windows
  desktop and on GitHub's windows runners it actually runs.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mshroom import __main__ as launcher

REPO_ROOT = Path(__file__).resolve().parents[1]


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _free_port() -> int:
    """Ask the OS for a currently free TCP port (close it right away --
    a small race with other processes, acceptable in tests)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _no_server_thread_left() -> bool:
    return not any(t.name == "mshroom-uvicorn" and t.is_alive() for t in threading.enumerate())


# ---------------------------------------------------------------------------
# In-process server thread (the desktop mode's engine, minus the window)
# ---------------------------------------------------------------------------


def test_server_thread_serves_and_stops_cleanly():
    """start_server_thread binds an OS-assigned loopback port, serves the
    tabbed page, and stop_server_thread leaves no thread and no running
    MLLP listener behind."""
    server, thread, port = launcher.start_server_thread()
    try:
        assert port != 0
        status, body = _get(f"http://127.0.0.1:{port}/")
        assert status == 200
        assert "MSHroom" in body
    finally:
        assert launcher.stop_server_thread(server, thread) is True
    assert _no_server_thread_left()
    # The app's lifespan shutdown must have stopped the MLLP listener.
    import app.main as main_module

    assert main_module.listener.is_running is False


def test_headless_defaults_are_network_facing():
    """Spec: headless is the server mode -- it binds all interfaces on the
    documented port, never a localhost-only default."""
    assert launcher.DEFAULT_HEADLESS_HOST == "0.0.0.0"
    assert launcher.DEFAULT_HEADLESS_PORT == 8550


def test_host_and_port_flags_require_headless():
    with pytest.raises(SystemExit):
        launcher.main(["--port", "9999"])
    with pytest.raises(SystemExit):
        launcher.main(["--host", "127.0.0.1"])


# ---------------------------------------------------------------------------
# Headless mode as a real subprocess
# ---------------------------------------------------------------------------


def test_headless_subprocess_serves_and_shuts_down_cleanly(tmp_path):
    """Full headless lifecycle: starts, serves /, exits cleanly on the
    platform's shutdown signal.

    Doubles as the no-pywebview guarantee: a poisoned ``webview`` module
    is planted first on PYTHONPATH, so if ``--headless`` (or anything it
    imports) touched pywebview, the server would crash instead of serving.
    """
    (tmp_path / "webview.py").write_text(
        "raise RuntimeError('--headless must not import pywebview')\n", encoding="utf-8"
    )
    port = _free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    env["HL7_LISTENER_PORT"] = "0"  # OS-assigned; never collide with other tests

    creationflags = 0
    if sys.platform == "win32":
        # A new process group so CTRL_BREAK_EVENT reaches only this child.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [sys.executable, "-m", "mshroom", "--headless", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    try:
        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
                pytest.fail(f"headless process exited early ({proc.returncode}):\n{out}")
            try:
                status, body = _get(f"http://127.0.0.1:{port}/", timeout=2)
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                last_error = exc
                time.sleep(0.25)
        else:
            pytest.fail(f"headless server never answered on port {port}: {last_error}")
        assert status == 200
        assert "MSHroom" in body

        # Graceful stop: SIGTERM on POSIX, CTRL_BREAK (-> SIGBREAK, which
        # uvicorn handles) on Windows. Both run the lifespan shutdown.
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=20)
        out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""

        # Clean shutdown is proven by uvicorn's shutdown log lines: the
        # lifespan shutdown ran (that's what stops the MLLP listener).
        assert "Shutting down" in out, f"no graceful shutdown in output:\n{out}"
        assert "Application shutdown complete" in out, f"lifespan shutdown missing:\n{out}"

        # The exit code is signal-death by design: after its graceful
        # shutdown, uvicorn re-raises the captured signal with the default
        # handler restored so parents see how the process was stopped
        # (POSIX: killed by SIGTERM; Windows: the CRT's default SIGBREAK
        # handler exits with code 3). Exit 0 would mean the signal was
        # swallowed, which uvicorn deliberately does not do.
        if sys.platform == "win32":
            assert returncode in (0, 3), f"unexpected exit code {returncode}:\n{out}"
        else:
            assert returncode in (0, -signal.SIGTERM), f"unexpected exit code {returncode}:\n{out}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Desktop window smoke test (guarded -- see module docstring)
# ---------------------------------------------------------------------------


def _webview2_runtime_present() -> bool:
    """Windows only: is the WebView2 Evergreen runtime installed?
    Microsoft's documented detection is the presence of its EdgeUpdate
    client key (per-machine or per-user)."""
    import winreg

    key_path = (
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
        r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            winreg.CloseKey(winreg.OpenKey(hive, key_path))
            return True
        except OSError:
            continue
    return False


def _webview_skip_reason() -> str | None:
    """None if this environment can actually show a pywebview window."""
    if importlib.util.find_spec("webview") is None:
        return "pywebview is not installed"
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "no display available"
    if sys.platform == "win32" and not _webview2_runtime_present():
        return "WebView2 runtime is not installed"
    return None


_SKIP_REASON = _webview_skip_reason()


@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")
def test_desktop_window_opens_and_exits():
    """Real-window smoke test: run_desktop opens a native window on the
    in-process server, the auto-close hook closes it, and everything --
    server thread and MLLP listener -- is down when it returns."""
    rc = launcher.run_desktop(auto_close_seconds=4.0)
    assert rc == 0
    assert _no_server_thread_left()
    import app.main as main_module

    assert main_module.listener.is_running is False
