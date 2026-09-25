"""The Windows backend (pywinpty) — these run ONLY on Windows (the CI windows job).
They drive a real cmd.exe through WinPtySession and through the whole WebSocket bridge
(sessions + api), the same path the console uses."""

from __future__ import annotations

import asyncio
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows backend")


async def _read_until(sess, marker: str, timeout: float = 20.0) -> str:
    buf = ""

    async def _loop():
        nonlocal buf
        while marker not in buf:
            chunk = await sess.read()
            if not chunk:
                return
            buf += chunk.decode("utf-8", "replace")

    await asyncio.wait_for(_loop(), timeout)
    return buf


def test_open_session_picks_winpty():
    from terminal.pty_session import WinPtySession, open_session

    assert isinstance(open_session(shell="cmd.exe"), WinPtySession)


async def test_cmd_round_trip_resize_and_close():
    from terminal.pty_session import WinPtySession

    sess = WinPtySession(shell="cmd.exe", cols=100, rows=30)
    sess.start()
    try:
        assert sess.pid
        sess.write("echo win_marker_%USERNAME%_ok\r\n")
        out = await _read_until(sess, "_ok")
        assert "win_marker_" in out
        sess.resize(120, 40)
        assert sess.poll() is None  # still running
    finally:
        code = await sess.aclose()
    assert sess.poll() is not None or code is None  # ended


async def test_scrubbed_secrets_do_not_reach_cmd(monkeypatch):
    from terminal.pty_session import WinPtySession

    monkeypatch.setenv("A2A_AUTH_TOKEN", "do-not-leak")
    sess = WinPtySession(shell="cmd.exe", scrub_env=["A2A_AUTH_TOKEN"])
    sess.start()
    try:
        sess.write("echo tok=[%A2A_AUTH_TOKEN%]\r\n")
        out = await _read_until(sess, "tok=[")
        await asyncio.sleep(0.5)
        out += await _read_until(sess, "]")
        assert "do-not-leak" not in out
    finally:
        await sess.aclose()


def test_websocket_bridge_end_to_end(monkeypatch):
    """The full console path on Windows: WS auth → SessionManager → WinPtySession."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from terminal import api
    from terminal.sessions import SessionManager

    mgr = SessionManager()
    monkeypatch.setattr(api, "MANAGER", mgr)
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(api.build_router({"shell": "cmd.exe"}), prefix="/plugins/terminal")
    with TestClient(app) as c:
        with c.websocket_connect("/plugins/terminal/ws") as ws:
            ws.send_json({"type": "auth", "token": "", "cols": 100, "rows": 30})
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "input", "data": "echo bridge_win_ok\r\n"})
            got = ""
            for _ in range(500):
                m = ws.receive_json()
                if m.get("type") == "data":
                    got += m["data"]
                    if "bridge_win_ok" in got.replace("echo bridge_win_ok", ""):
                        break
            assert "bridge_win_ok" in got
            ws.send_json({"type": "close"})
        c.portal.call(mgr.close_all)
