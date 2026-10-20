"""terminal-plugin#20 — a PTY that stops accepting input must never block the event loop.

Real PTYs and real processes only (a mocked write would pass whether or not the syscall
can block). Two shapes, both reproduced against v0.9.3, where each froze the loop until
the child was killed:

- a program in raw mode that never reads stdin — the PTY input queue fills and the
  master ``write`` blocks for as long as the program lives;
- a program that DOES read and echoes — it can only take input as fast as its output is
  drained, and the drain is the loop's own pump; a blocking write on the loop deadlocks.

Each scenario runs its own event loop in a worker thread, watched from the test thread:
a stall is reported (and the child killed) instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time

import pytest

from terminal import api, sessions, tools
from terminal.pty_session import PtySession, PtyWriteTimeout
from terminal.sessions import SessionManager

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY backend")

# Far beyond any PTY input buffer (macOS ~1 KiB raw queue; Linux up to 640 KiB of
# tty buffers), so a non-reading child is guaranteed to stop accepting it.
BIG = 2 * 1024 * 1024
MAX_LOOP_GAP = 0.75  # seconds — a blocked loop shows up as a heartbeat gap


def _script(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def stops_reading(tmp_path):
    """A 'shell' that puts its tty in raw mode, then never reads its input."""
    return _script(tmp_path, "stops_reading.sh", "stty raw -echo\nexec sleep 600")


@pytest.fixture
def raw_echo(tmp_path):
    """A 'shell' that reads every byte and echoes it (raw mode + echo, then cat)."""
    return _script(tmp_path, "raw_echo.sh", "stty raw\nexec cat")


def _run_watched(scenario, *, limit: float = 30.0):
    """Run ``scenario(state)`` (an async fn) on a fresh loop in a worker thread, with a
    heartbeat task beside it. Returns ``(result, max_heartbeat_gap)``. If the loop wedges,
    kill the child whose pid the scenario stored in ``state['pids']`` and fail."""
    state: dict = {"pids": [], "beat": time.monotonic(), "gap": 0.0}
    out: dict = {}

    async def main():
        async def heartbeat():
            while True:
                now = time.monotonic()
                state["gap"] = max(state["gap"], now - state["beat"])
                state["beat"] = now
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(heartbeat())
        try:
            out["result"] = await scenario(state)
        except BaseException as exc:  # noqa: BLE001 — surfaced in the test thread
            out["error"] = exc
        finally:
            hb.cancel()

    th = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    th.start()
    th.join(limit)
    if th.is_alive():
        silent = time.monotonic() - state["beat"]
        for pid in state["pids"]:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        th.join(5)
        pytest.fail(f"the event loop wedged — no heartbeat for {silent:.1f}s (terminal-plugin#20)")
    if "error" in out:
        raise out["error"]
    return out.get("result"), state["gap"]


def test_a_pty_that_stops_reading_fails_the_write_without_blocking_the_loop(stops_reading):
    async def scenario(state):
        sess = PtySession(shell=stops_reading)
        sess.start()
        state["pids"].append(sess.pid)
        try:
            await asyncio.sleep(0.3)  # let it reach `stty raw` + sleep
            t0 = time.monotonic()
            sess.write(b"k" * 4096)  # fire-and-forget keystrokes: returns at once
            assert time.monotonic() - t0 < 0.1
            t0 = time.monotonic()
            with pytest.raises(PtyWriteTimeout) as exc:
                await sess.send(b"x" * BIG, timeout=1.0)
            took = time.monotonic() - t0
            assert 0.9 <= took < 3.0, took
            assert exc.value.total == BIG and exc.value.written < BIG
            assert "stopped accepting input" in str(exc.value)
        finally:
            t0 = time.monotonic()
            await asyncio.wait_for(sess.aclose(), 8)
            closed_in = time.monotonic() - t0
        return closed_in

    closed_in, gap = _run_watched(scenario)
    assert gap < MAX_LOOP_GAP, f"the loop stalled {gap:.2f}s"
    assert closed_in < 5.0  # the input thread let go once the child died


def test_a_big_write_to_an_echoing_pty_cannot_deadlock_the_pump(raw_echo):
    """The deadlock shape: the child echoes, so it stops reading input until its output is
    drained — and the session's pump (on the loop) is what drains it."""

    async def scenario(state):
        mgr = SessionManager()
        sess = mgr.create(shell=raw_echo)
        state["pids"].append(sess.pty.pid)
        try:
            await asyncio.sleep(0.3)
            payload = b"y" * (256 * 1024)
            written = await sess.send(payload, timeout=20)
            assert written == len(payload)
            # cat writes everything back (plus the tty echo): wait for the pump to see it
            deadline = time.monotonic() + 10
            while sess.emitted < len(payload) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            return sess.emitted
        finally:
            await asyncio.wait_for(mgr.close_all(), 8)

    emitted, gap = _run_watched(scenario)
    assert emitted >= 256 * 1024
    assert gap < MAX_LOOP_GAP, f"the loop stalled {gap:.2f}s"


def test_terminal_run_fails_cleanly_when_the_tab_stops_accepting_input(stops_reading, registry, monkeypatch):
    """The tool from the issue: it returns a clear error well inside its own budget and
    the loop keeps serving meanwhile."""
    monkeypatch.setattr(tools, "TYPE_TIMEOUT", 1.0)

    async def scenario(state):
        mgr = SessionManager()
        for mod in (sessions, tools, api):
            monkeypatch.setattr(mod, "MANAGER", mgr)
        cfg = {"shell": stops_reading, "login_shell": False}
        t = {x.name: x for x in tools.build_tools(registry, lambda: api.resolve(cfg))}
        try:
            run = asyncio.create_task(t["terminal_run"].ainvoke({"command": "echo " + "z" * BIG, "wait_seconds": 30}))
            while mgr.agent_session() is None and not run.done():
                await asyncio.sleep(0.01)
            if mgr.agent_session() is not None:
                state["pids"].append(mgr.agent_session().pty.pid)
            t0 = time.monotonic()
            out = await asyncio.wait_for(run, 15)
            return out, time.monotonic() - t0
        finally:
            await asyncio.wait_for(mgr.close_all(), 8)

    (out, took), gap = _run_watched(scenario)
    assert "stopped accepting input" in out and "session='new'" in out
    assert took < 8.0  # ≈ settle (≤3s) + the 1s typing budget, never the 30s wait
    assert gap < MAX_LOOP_GAP, f"the loop stalled {gap:.2f}s"
