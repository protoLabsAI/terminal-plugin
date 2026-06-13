"""PTY session tests — these spawn REAL pseudo-terminals (stdlib pty), so they
exercise the actual shell bridge in CI (Linux/macOS runners have PTYs)."""

from __future__ import annotations

import asyncio

from terminal.pty_session import PtySession, default_shell


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
