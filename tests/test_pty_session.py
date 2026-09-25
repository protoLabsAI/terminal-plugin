"""PTY session tests — these spawn REAL pseudo-terminals (stdlib pty), so they
exercise the actual shell bridge in CI (Linux/macOS runners have PTYs)."""

from __future__ import annotations

import asyncio

import pytest

from terminal.pty_session import (
    PtyError,
    PtySession,
    WinPtySession,
    default_shell,
    login_argv0,
    open_session,
    utf8_locale,
)


async def _read_until(sess, marker: str, timeout: float = 8.0) -> bytes:
    buf = b""

    async def _loop():
        nonlocal buf
        while marker.encode() not in buf:
            chunk = await sess.read()
            if not chunk:
                return
            buf += chunk

    await asyncio.wait_for(_loop(), timeout)
    return buf


# ── env (pure) ──────────────────────────────────────────────────────────────────


def test_default_shell_prefers_env(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert default_shell() == "/bin/zsh"
    monkeypatch.delenv("SHELL", raising=False)
    assert default_shell() == "/bin/bash"


def test_build_env_sets_term_and_scrubs_secrets(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_API_KEY", "s3cret")
    monkeypatch.setenv("KEEP_ME", "1")
    s = PtySession(scrub_env=["PROTOAGENT_API_KEY"], env_overrides={"FOO": "bar"})
    env = s._build_env()
    assert env["TERM"] == "xterm-256color" and env["COLORTERM"] == "truecolor"
    assert "PROTOAGENT_API_KEY" not in env  # the operator's secret is scrubbed
    assert env["KEEP_ME"] == "1" and env["FOO"] == "bar"


# ── a real PTY round-trip ─────────────────────────────────────────────────────


async def test_round_trip_resize_and_close():
    s = PtySession(shell="/bin/sh", cwd="/", cols=80, rows=24)
    s.start()
    try:
        assert s.pid and s.pid > 0
        s.resize(120, 40)  # must not raise
        s.write("echo hello_pty_marker\n")
        out = await _read_until(s, "hello_pty_marker")
        assert b"hello_pty_marker" in out
    finally:
        await s.aclose()
    assert s._fd is None  # fd closed on aclose


async def test_read_returns_empty_and_reaps_on_shell_exit():
    s = PtySession(shell="/bin/sh")
    s.start()
    try:
        s.write("exit 0\n")

        async def _drain():
            while await s.read():
                pass

        await asyncio.wait_for(_drain(), 8.0)
        assert s.poll() is not None  # the child exited → reaped, exit code known
    finally:
        await s.aclose()


async def test_a_signal_killed_shell_reports_eof_and_a_signal_exit_code():
    """The common real-world ending: the shell is killed (kill -9, OOM, a crash) rather
    than exiting cleanly. The reader must see EOF and poll() the negative-signal code."""
    import os
    import signal

    s = PtySession(shell="/bin/sh")
    s.start()
    try:
        os.kill(s.pid, signal.SIGKILL)

        async def _drain():
            while await s.read():
                pass

        await asyncio.wait_for(_drain(), 8.0)
        for _ in range(100):
            if s.poll() is not None:
                break
            await asyncio.sleep(0.02)
        assert s.poll() == -signal.SIGKILL
    finally:
        await s.aclose()


# ── backend selection + the Windows (pywinpty) backend ──────────────────────────


def test_open_session_picks_posix_on_this_platform():
    # The suite runs on Linux/macOS → the POSIX backend.
    assert isinstance(open_session(shell="/bin/sh"), PtySession)


def test_winpty_build_env_and_missing_dep():
    s = WinPtySession(scrub_env=["SECRET_API_KEY"], env_overrides={"FOO": "bar"})
    env = s._build_env()
    assert env["TERM"] == "xterm-256color" and env["FOO"] == "bar"
    assert "SECRET_API_KEY" not in env
    # pywinpty isn't installed off Windows → start() raises a clear PtyError (not a raw
    # ImportError), so the bridge surfaces a useful message.
    with pytest.raises(PtyError):
        s.start()


# ── daily-driver env: login shell + UTF-8 ───────────────────────────────────────


def test_login_argv0_is_the_dashed_basename():
    assert login_argv0("/bin/zsh") == "-zsh"
    assert login_argv0("/opt/homebrew/bin/fish") == "-fish"


@pytest.mark.parametrize(
    "env,expect_fill",
    [
        ({}, True),  # launched from a GUI: no locale at all
        ({"LANG": "C"}, True),
        ({"LANG": "en_US.UTF-8"}, False),
        ({"LANG": "C", "LC_ALL": "de_DE.utf8"}, False),
        ({"LC_CTYPE": "UTF-8"}, False),
    ],
)
def test_utf8_locale_only_fills_a_gap(env, expect_fill):
    out = utf8_locale(env)
    assert bool(out) is expect_fill
    if out:
        assert "UTF-8" in out["LANG"]


async def test_a_login_shell_reports_a_dashed_argv0():
    sess = PtySession(shell="/bin/sh", login=True)
    sess.start()
    try:
        sess.write("echo zero=[$0]\n")
        out = (await _read_until(sess, "zero=[-sh]")).decode("utf-8", "replace")
        assert "zero=[-sh]" in out
    finally:
        await sess.aclose()


async def test_a_non_login_shell_keeps_its_path_as_argv0():
    sess = PtySession(shell="/bin/sh", login=False)
    sess.start()
    try:
        sess.write("echo zero=[$0]\n")
        out = (await _read_until(sess, "zero=[/bin/sh]")).decode("utf-8", "replace")
        assert "zero=[/bin/sh]" in out
    finally:
        await sess.aclose()


async def test_the_shell_gets_a_utf8_locale_when_the_server_has_none(monkeypatch):
    for k in ("LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(k, raising=False)
    sess = PtySession(shell="/bin/sh")
    sess.start()
    try:
        sess.write("echo lang=[$LANG]\n")
        out = (await _read_until(sess, "UTF-8]")).decode("utf-8", "replace")
        assert "UTF-8]" in out
    finally:
        await sess.aclose()


async def test_closing_a_shell_with_unread_output_never_hangs():
    """Regression: on macOS a process exiting with unread tty output blocks in exit()
    until the master drains it. aclose() used to SIGKILL then block in waitpid() with
    nobody reading — a permanent hang. It must drain while it waits."""
    import time

    sess = PtySession(shell="/bin/sh", login=True)
    sess.start()
    try:
        sess.write("yes flood_flood_flood_flood\n")  # floods the pty; we never read it
        await asyncio.sleep(0.5)
    finally:
        t = time.monotonic()
        await asyncio.wait_for(sess.aclose(), 6)
        assert time.monotonic() - t < 4.5
    assert sess.pid is None and sess._fd is None  # reaped + closed


async def test_aclose_is_idempotent():
    sess = PtySession(shell="/bin/sh")
    sess.start()
    await sess.aclose()
    assert await sess.aclose() == sess._exit_code


async def test_closing_right_after_spawn_never_signals_our_own_process_group():
    """Regression: aclose() straight after start() races the child's setsid(). The old
    killpg(getpgid(child)) could land on the PARENT's group — i.e. kill the server."""
    import signal as _signal

    hits = []
    prev = _signal.signal(_signal.SIGHUP, lambda *a: hits.append("HUP"))
    prev_term = _signal.signal(_signal.SIGTERM, lambda *a: hits.append("TERM"))
    try:
        # (Each close can take the ~1s grace: the handlers installed above are inherited by
        # the pre-exec child, so it ignores HUP/TERM until SIGKILL — hence few rounds.)
        for _ in range(6):
            sess = PtySession(shell="/bin/sh")
            sess.start()
            await sess.aclose()  # immediately — maximally inside the race window
        await asyncio.sleep(0.05)
    finally:
        _signal.signal(_signal.SIGHUP, prev)
        _signal.signal(_signal.SIGTERM, prev_term)
    assert hits == []  # nothing ever reached this process
