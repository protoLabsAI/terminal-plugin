"""API tests — the token gate, the view route, and a REAL WebSocket↔PTY round-trip
through FastAPI's TestClient (it spawns an actual /bin/sh and echoes through it)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from terminal import api


def _app(cfg=None):
    app = FastAPI()
    # `cat` echoes its stdin deterministically (no shell prompt/init/echo race), so the
    # WS round-trip is stable across platforms — the shell behaviour itself is covered
    # by test_pty_session against /bin/sh.
    app.include_router(api.build_router(cfg or {"shell": "/bin/cat"}), prefix="/plugins/terminal")
    return app


# ── the bearer gate (pure) ──────────────────────────────────────────────────────


def test_verify_token():
    assert api.verify_token("", "anything") is True  # no host token → open (loopback dev)
    assert api.verify_token("s3cret", "s3cret") is True
    assert api.verify_token("s3cret", "nope") is False
    assert api.verify_token("s3cret", "") is False


def test_scrub_keys_targets_the_operator_secrets(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "roxy")
    keys = api.scrub_keys()
    assert "ROXY_API_KEY" in keys and "A2A_AUTH_TOKEN" in keys


def test_expected_token_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "abc123")
    assert api.expected_token() == "abc123"  # no host → env fallback


# ── the view route ──────────────────────────────────────────────────────────────


def test_view_served_on_the_public_path():
    c = TestClient(_app())
    r = c.get("/plugins/terminal/view")
    assert r.status_code == 200 and "xterm" in r.text.lower()


# ── the bearer gate over the wire ───────────────────────────────────────────────


def test_ws_rejects_a_missing_token_when_one_is_required(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "s3cret")
    c = TestClient(_app())
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/plugins/terminal/ws") as ws:
            ws.receive_json()  # the server closes 4001 before accept → raises


def test_ws_accepts_the_matching_token(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "s3cret")
    c = TestClient(_app())
    with c.websocket_connect("/plugins/terminal/ws?token=s3cret") as ws:
        assert ws.receive_json()["type"] == "connected"


# ── a real shell round-trip ─────────────────────────────────────────────────────


def test_ws_round_trip_with_a_real_shell(monkeypatch):
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)  # no token → open
    c = TestClient(_app({"shell": "/bin/cat"}))  # cat echoes input deterministically
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "input", "data": "ws_marker_42\n"})
        got = ""
        for _ in range(300):
            m = ws.receive_json()
            if m.get("type") == "data":
                got += m["data"]
                if "ws_marker_42" in got:
                    break
        assert "ws_marker_42" in got
        # resize must not break the stream; ping → pong
        ws.send_json({"type": "resize", "cols": 100, "rows": 30})
        ws.send_json({"type": "ping"})
        pong = False
        for _ in range(50):
            if ws.receive_json().get("type") == "pong":
                pong = True
                break
        assert pong
