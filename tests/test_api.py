"""API tests — the token gate, the view route, and a REAL WebSocket↔PTY round-trip
through FastAPI's TestClient (it spawns an actual /bin/sh and echoes through it)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from terminal import api
from terminal.sessions import SessionManager


@pytest.fixture(autouse=True)
def _fresh_manager(monkeypatch):
    """Each test gets its own session registry (shells now outlive sockets)."""
    mgr = SessionManager()
    monkeypatch.setattr(api, "MANAGER", mgr)
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    return mgr


@pytest.fixture
def client(_fresh_manager):
    """A TestClient held open for the whole test — one event loop, so a session's pump
    task survives across websocket_connect calls — that ends every shell on teardown."""

    def make(cfg=None):
        c = TestClient(_app(cfg))
        c.__enter__()
        made.append(c)
        return c

    made = []
    yield make
    for c in made:
        c.portal.call(_fresh_manager.close_all)
        c.__exit__(None, None, None)


def _read_until(ws, marker, limit=400):
    got = ""
    for _ in range(limit):
        m = ws.receive_json()
        if m.get("type") == "data":
            got += m["data"]
            if marker in got:
                return got
    raise AssertionError(f"{marker!r} never arrived; got {got!r}")


def _auth(ws, token="", session=None):
    ws.send_json({"type": "auth", "token": token, "session": session, "cols": 80, "rows": 24})
    m = ws.receive_json()
    assert m["type"] == "connected", m
    return m


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
    assert "__TERMINAL_CONFIG__" not in r.text  # the placeholder is always filled


def test_view_bakes_in_the_configured_font_and_scrollback():
    r = TestClient(_app({"font_size": 17, "scrollback": 12345})).get("/plugins/terminal/view")
    assert '"fontSize": 17' in r.text and '"scrollback": 12345' in r.text


def test_view_reads_config_live_through_a_callable():
    cfg = {"font_size": 11}
    c = TestClient(_app(lambda: cfg))
    assert '"fontSize": 11' in c.get("/plugins/terminal/view").text
    cfg["font_size"] = 19  # a Settings save — no re-mount
    assert '"fontSize": 19' in c.get("/plugins/terminal/view").text


def test_resolve_fills_defaults_and_clamps():
    r = api.resolve({"font_size": 999, "scrollback": "junk", "keep_alive_minutes": -5, "shell": ""})
    assert r["font_size"] == 32 and r["scrollback"] == api.DEFAULTS["scrollback"]
    assert r["keep_alive_minutes"] == 0 and r["shell"] == ""
    assert api.resolve(None) == api.resolve({}) == {**api.DEFAULTS}


def test_render_page_cannot_be_broken_out_of_the_script_tag(monkeypatch):
    monkeypatch.setattr(api, "PAGE", "<script>var C = __TERMINAL_CONFIG__;</script>")
    out = api.render_page({"font_size": 13, "scrollback": 5000})
    assert out.count("</script>") == 1


def test_vendored_assets_served_locally():
    c = TestClient(_app())
    js = c.get("/plugins/terminal/static/xterm.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert c.get("/plugins/terminal/static/xterm.css").status_code == 200
    assert c.get("/plugins/terminal/static/addon-fit.js").status_code == 200
    assert c.get("/plugins/terminal/static/addon-canvas.js").status_code == 200
    # whitelist only — an unknown name is 404 (no traversal)
    assert c.get("/plugins/terminal/static/secret.py").status_code == 404


# ── the bearer gate over the wire (first frame, never the URL) ──────────────────


def test_ws_rejects_a_missing_token_when_one_is_required(monkeypatch, client):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "s3cret")
    c = client()
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        ws.send_json({"type": "auth", "token": ""})
        assert ws.receive_json() == {"type": "error", "message": "unauthorized"}
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4001


def test_ws_ignores_a_token_in_the_url(monkeypatch, client):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "s3cret")
    c = client()
    with c.websocket_connect("/plugins/terminal/ws?token=s3cret") as ws:
        ws.send_json({"type": "auth"})  # the URL token no longer counts
        assert ws.receive_json()["type"] == "error"


def test_ws_first_frame_must_be_auth(monkeypatch, client):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "s3cret")
    c = client()
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        ws.send_json({"type": "input", "data": "ls\n"})
        assert ws.receive_json()["type"] == "error"


def test_ws_auth_times_out(monkeypatch, client):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(api, "AUTH_TIMEOUT", 0.2)
    c = client()
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        assert ws.receive_json()["type"] == "error"


def test_ws_accepts_the_matching_token(monkeypatch, client):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "s3cret")
    c = client()
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        m = _auth(ws, token="s3cret")
        assert m["session"] and m["resumed"] is False


# ── a real shell round-trip ─────────────────────────────────────────────────────


def test_ws_round_trip_with_a_real_shell(client):
    c = client({"shell": "/bin/cat"})  # cat echoes input deterministically
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        _auth(ws)
        ws.send_json({"type": "input", "data": "ws_marker_42\n"})
        _read_until(ws, "ws_marker_42")
        # resize must not break the stream; ping → pong
        ws.send_json({"type": "resize", "cols": 100, "rows": 30})
        ws.send_json({"type": "ping"})
        assert any(ws.receive_json().get("type") == "pong" for _ in range(50))


# ── persistence: the shell outlives the socket ─────────────────────────────────


def test_a_dropped_socket_detaches_and_reattach_replays(client, _fresh_manager):
    c = client({"shell": "/bin/cat"})
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        sid = _auth(ws)["session"]
        ws.send_json({"type": "input", "data": "before_drop\n"})
        _read_until(ws, "before_drop")
    # the socket is gone — the shell is not
    assert _fresh_manager.get(sid) is not None
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        m = _auth(ws, session=sid)
        assert m["session"] == sid and m["resumed"] is True
        _read_until(ws, "before_drop")  # the replay of what it printed earlier
        ws.send_json({"type": "input", "data": "after_reattach\n"})
        _read_until(ws, "after_reattach")  # and it is the same live shell


def test_an_unknown_session_gets_a_fresh_shell(client):
    c = client({"shell": "/bin/cat"})
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        m = _auth(ws, session="no-such-session")
        assert m["resumed"] is False and m["session"] != "no-such-session"


def test_close_ends_the_shell(client, _fresh_manager):
    c = client({"shell": "/bin/cat"})
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        sid = _auth(ws)["session"]
        ws.send_json({"type": "close"})
        with pytest.raises(WebSocketDisconnect):
            for _ in range(50):
                ws.receive_json()
    assert _fresh_manager.get(sid) is None


def test_keep_alive_zero_ends_the_shell_on_disconnect(client, _fresh_manager):
    c = client({"shell": "/bin/cat", "keep_alive_minutes": 0})
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        sid = _auth(ws)["session"]
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
    for _ in range(100):  # the server-side finally runs just after the client closes
        if _fresh_manager.get(sid) is None:
            break
        c.portal.call(_sleep, 0.02)
    assert _fresh_manager.get(sid) is None


async def _sleep(t):
    import asyncio

    await asyncio.sleep(t)


def test_a_second_viewer_takes_over(client, _fresh_manager):
    c = client({"shell": "/bin/cat", "keep_alive_minutes": 0})
    with c.websocket_connect("/plugins/terminal/ws") as first:
        sid = _auth(first)["session"]
        with c.websocket_connect("/plugins/terminal/ws") as second:
            assert _auth(second, session=sid)["resumed"] is True
            assert first.receive_json() == {"type": "detached", "reason": "attached elsewhere"}
            second.send_json({"type": "input", "data": "still_alive\n"})
            _read_until(second, "still_alive")
        # the kicked viewer's disconnect must NOT end the shell it no longer owns —
        # only the second (the owner) disconnecting does, with keep_alive=0.


def test_the_session_limit_is_enforced(client):
    c = client({"shell": "/bin/cat", "max_sessions": 1})
    with c.websocket_connect("/plugins/terminal/ws") as a:
        _auth(a)
        with c.websocket_connect("/plugins/terminal/ws") as b:
            b.send_json({"type": "auth", "token": ""})
            m = b.receive_json()
            assert m["type"] == "error" and "limit" in m["message"]


def test_the_shell_exiting_reports_and_forgets_the_session(client, _fresh_manager):
    c = client({"shell": "/bin/sh"})
    with c.websocket_connect("/plugins/terminal/ws") as ws:
        sid = _auth(ws)["session"]
        ws.send_json({"type": "input", "data": "exit 3\n"})
        for _ in range(400):
            m = ws.receive_json()
            if m["type"] == "exit":
                assert m["exitCode"] == 3
                break
        else:
            raise AssertionError("no exit frame")
    assert _fresh_manager.get(sid) is None
