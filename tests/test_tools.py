"""Agent tools — against REAL shells. The tools run on the test's event loop, the same
way they run on the server's loop in production."""

from __future__ import annotations

import asyncio
import os

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
