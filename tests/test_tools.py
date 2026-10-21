"""Agent tools — against REAL shells. The tools run on the test's event loop, the same
way they run on the server's loop in production."""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from terminal import api, sessions, tools
from terminal.sessions import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    m = SessionManager()
    monkeypatch.setattr(sessions, "MANAGER", m)
    monkeypatch.setattr(tools, "MANAGER", m)
    monkeypatch.setattr(api, "MANAGER", m)
    return m


@pytest.fixture
def cfg():
    return {"shell": "/bin/sh", "login_shell": False}


@pytest.fixture
def t(registry, cfg, mgr):
    return {x.name: x for x in tools.build_tools(registry, lambda: api.resolve(cfg))}


def test_the_four_tools_are_registered(registry):
    import terminal

    terminal.register(registry)
    names = {getattr(x, "name", "") for x in registry.tools}
    assert {"terminal_list", "terminal_read", "terminal_run", "terminal_open"} <= names
    assert "/api/plugins/terminal" in registry.routers  # the gated session list


async def test_run_opens_the_agent_tab_and_returns_output(t, mgr, registry):
    try:
        out = await t["terminal_run"].ainvoke({"command": "echo hello_from_agent", "wait_seconds": 10})
        assert "hello_from_agent" in out and "finished" in out
        agent = mgr.agent_session()
        assert agent is not None and agent.name == "Agent" and agent.pending_adopt
        # the view hears about the new tab; the operator's view is NOT switched
        assert ("session_opened", {"session": agent.id, "name": "Agent", "focus": False}) in registry.emitted
        assert registry.navigations == []
        # a second run reuses the same tab
        out2 = await t["terminal_run"].ainvoke({"command": "echo again_42", "wait_seconds": 10})
        assert "again_42" in out2 and len(mgr.list()) == 1
    finally:
        await mgr.close_all()


async def test_run_waits_for_a_real_program_to_finish(t, mgr):
    try:
        out = await t["terminal_run"].ainvoke({"command": "sleep 1; echo slept_done", "wait_seconds": 15})
        assert "slept_done" in out and "finished" in out
    finally:
        await mgr.close_all()


async def test_run_returns_early_for_a_long_running_command(t, mgr):
    try:
        out = await t["terminal_run"].ainvoke({"command": "echo started_srv; sleep 30", "wait_seconds": 1.5})
        assert "started_srv" in out and "still running" in out
        # …and refuses to type into the tab while that program holds it
        busy = await t["terminal_run"].ainvoke({"command": "echo nope", "wait_seconds": 2})
        assert "busy running" in busy and "sleep" in busy
    finally:
        await mgr.close_all()


async def test_run_returns_at_the_deadline_for_a_command_that_never_prompts(t, mgr):
    """terminal-plugin#21 — a tab kept busy (and noisy) for the whole wait returns
    'still running after Ns' AT the deadline, not after the command eventually ends."""
    try:
        started = time.monotonic()
        out = await t["terminal_run"].ainvoke(
            {"command": "while true; do echo tick; sleep 0.1; done", "wait_seconds": 3}
        )
        elapsed = time.monotonic() - started
        assert "still running after 3s" in out
        assert elapsed < 5, f"took {elapsed:.1f}s — the wait overran its deadline"
    finally:
        await mgr.close_all()


async def test_run_does_not_hang_the_loop_when_the_foreground_probe_blocks(t, mgr, monkeypatch):
    """terminal-plugin#21 — if foreground_pgid blocks far past the wait, terminal_run
    still returns within wait + 2s (the probe runs off-loop, bounded) and the event loop
    keeps serving other coroutines throughout."""
    sess = mgr.create(shell="/bin/sh", origin="agent")
    await asyncio.sleep(0.4)  # let the first prompt settle before we swap the probe
    release = threading.Event()
    calls = {"n": 0}
    shell_pid = sess.pty.pid

    def blocking_fg():
        calls["n"] += 1
        if calls["n"] == 1:
            return shell_pid  # the pre-typing probe sees the shell at its prompt — safe to type
        release.wait(30)  # every wait-loop probe wedges here until teardown
        return shell_pid

    monkeypatch.setattr(sess.pty, "foreground_pgid", blocking_fg)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.05)

    beat = asyncio.create_task(ticker())
    try:
        started = time.monotonic()
        out = await t["terminal_run"].ainvoke({"command": "echo marker; sleep 30", "wait_seconds": 3})
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"took {elapsed:.1f}s — a blocking probe stalled the wait"
        assert "still running after 3s" in out
        assert ticks > 15, f"only {ticks} ticks — the event loop was blocked during the wait"
    finally:
        beat.cancel()
        release.set()
        await mgr.close_all()


async def test_run_refuses_to_type_when_the_tabs_state_cannot_be_read(t, mgr, monkeypatch):
    """terminal-plugin#21 — when the pre-typing probe can't read the tab's state in time,
    terminal_run refuses and types nothing (never feed keystrokes into a wedged tab)."""
    sess = mgr.create(shell="/bin/sh", origin="agent")
    await asyncio.sleep(0.4)
    marker = sess.emitted
    release = threading.Event()

    def blocking_fg():
        release.wait(30)  # the pre-typing probe can never read the state in time
        return sess.pty.pid

    monkeypatch.setattr(sess.pty, "foreground_pgid", blocking_fg)
    try:
        out = await t["terminal_run"].ainvoke({"command": "echo should_not_run", "wait_seconds": 3})
        assert "couldn't be read" in out and "state" in out.lower()
        raw, _ = sess.text_since(marker)
        assert "should_not_run" not in raw  # nothing was typed into the tab
    finally:
        release.set()
        await mgr.close_all()


async def test_run_in_a_cwd(t, mgr, tmp_path):
    try:
        out = await t["terminal_run"].ainvoke({"command": "pwd", "cwd": str(tmp_path), "wait_seconds": 10})
        assert os.path.realpath(str(tmp_path)) in out or str(tmp_path) in out
    finally:
        await mgr.close_all()


async def test_read_the_active_tab(t, mgr):
    try:
        s = mgr.create(shell="/bin/sh")
        s.focused_at = 1.0  # a view is showing it
        s.write("echo \x1b_x; printf '\\033[31mERROR: boom\\033[0m\\n'\r")
        await asyncio.sleep(0.8)
        out = await t["terminal_read"].ainvoke({"session": "active", "lines": 20})
        assert "ERROR: boom" in out and "\x1b" not in out  # colour codes stripped
        assert s.id in out
    finally:
        await mgr.close_all()


async def test_list_describes_tabs(t, mgr):
    try:
        await t["terminal_run"].ainvoke({"command": "echo x", "wait_seconds": 5})
        out = await t["terminal_list"].ainvoke({})
        assert "Agent" in out and "agent's tab" in out
    finally:
        await mgr.close_all()


async def test_open_brings_the_terminal_view_up(t, mgr, registry, tmp_path):
    try:
        out = await t["terminal_open"].ainvoke({"cwd": str(tmp_path), "name": "repo"})
        assert "Opened terminal" in out
        assert registry.navigations == ["terminal"]
        s = mgr.list()[0]
        assert s.origin == "operator" and s.name == "repo" and s.pending_adopt
        assert ("session_opened", {"session": s.id, "name": "repo", "focus": True}) in registry.emitted
        bad = await t["terminal_open"].ainvoke({"cwd": str(tmp_path / "nope")})
        assert "No such directory" in bad
    finally:
        await mgr.close_all()


@pytest.mark.parametrize("level,allowed", [("off", set()), ("read", {"terminal_list", "terminal_read"})])
async def test_agent_access_gates_the_tools(t, mgr, cfg, level, allowed):
    cfg["agent_access"] = level
    try:
        calls = {
            "terminal_list": {},
            "terminal_read": {},
            "terminal_run": {"command": "echo hi", "wait_seconds": 1},
            "terminal_open": {},
        }
        for name, args in calls.items():
            out = await t[name].ainvoke(args)
            refused = "agent access" in out
            assert refused is (name not in allowed), (name, out)
        assert mgr.list() == []  # nothing was spawned
    finally:
        await mgr.close_all()


async def test_access_is_read_live(t, mgr, cfg):
    cfg["agent_access"] = "off"
    assert "agent access" in await t["terminal_list"].ainvoke({})
    cfg["agent_access"] = "read"  # a Settings change — no reload
    assert "agent access" not in await t["terminal_list"].ainvoke({})
