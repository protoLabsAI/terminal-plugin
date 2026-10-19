"""Core ticket-binding contract, with host fakes and a real WebSocket/PTY bridge."""

import asyncio
import contextlib
import sys
import time

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from terminal import api
from terminal.sessions import SessionManager


class Guard:
    """Host lifecycle fake: inspect late binding, close, then cancel a stuck receiver.

    Core 0.210.0 owns the timer and liveness check; this fake exercises the plugin's
    binding and unwind contract without importing the host or duplicating its auth.
    """

    def __init__(self, app):
        self.app = app
        self.live = True
        self.scopes = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            return await self.app(scope, receive, send)
        scope.setdefault("state", {})
        self.scopes.append(scope)
        task = asyncio.create_task(self.app(scope, receive, send))

        async def watch():
            while True:
                await asyncio.sleep(0.02)
                if scope["state"].get("device_id") and not self.live:
                    await send({"type": "websocket.close", "code": 1008})
                    task.cancel()
                    return

        watcher = asyncio.create_task(watch())
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


@pytest.fixture
def host(monkeypatch, socket_guard_host):
    mgr = SessionManager()
    monkeypatch.setattr(api, "MANAGER", mgr)
    monkeypatch.setattr(api, "_tickets", {})
    monkeypatch.setenv("A2A_AUTH_TOKEN", "operator")
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        credential = request.headers.get("authorization")
        if credential not in ("Bearer device", "Bearer operator"):
            return JSONResponse({}, status_code=401)
        if credential == "Bearer device":
            request.state.device_id = "trusted-device"
        return await call_next(request)

    cfg = {"shell": "cmd.exe" if sys.platform == "win32" else "/bin/sh", "login_shell": False, "keep_alive_minutes": 0}
    app.include_router(api.build_router(lambda: cfg), prefix="/plugins/terminal")
    app.include_router(api.build_api_router(), prefix="/api/plugins/terminal")
    guard = Guard(app)
    bound = []
    bind = socket_guard_host.bind_device

    def record(ws, did):
        bound.append(did)
        bind(ws, did)

    monkeypatch.setattr(socket_guard_host, "bind_device", record)
    with TestClient(guard) as client:
        try:
            yield client, mgr, guard, bound, cfg
        finally:
            client.portal.call(mgr.close_all)


def mint(client, credential="device"):
    response = client.post(
        "/api/plugins/terminal/ticket?device_id=forged",
        headers={"Authorization": f"Bearer {credential}", "X-Device-Id": "forged"},
        json={"device_id": "forged"},
    )
    assert response.status_code == 200
    return response.json()["ticket"]


def wait_detached(client, session):
    for _ in range(100):
        if not session.attached:
            return
        client.portal.call(asyncio.sleep, 0.02)
    pytest.fail("guard close did not detach the PTY")


@pytest.mark.parametrize("keep_alive", [0, 30])
@pytest.mark.parametrize("reason", ["revoked", "expired"])
def test_bound_socket_closes_and_cleans_up(host, keep_alive, reason):
    client, mgr, guard, bound, cfg = host
    cfg["keep_alive_minutes"] = keep_alive
    ticket = mint(client)
    with client.websocket_connect("/plugins/terminal/ws") as ws:
        ws.send_json({"type": "auth", "ticket": ticket, "device_id": "forged"})
        session = mgr.get(ws.receive_json()["session"])
        assert bound == ["trusted-device"]
        assert ticket not in api._tickets
        ws.send_json({"type": "input", "data": "echo device_roundtrip\r\n"})
        while "device_roundtrip" not in ws.receive_json().get("data", ""):
            pass
        started = time.monotonic()
        guard.live = False  # both revoke and idle expiry become not-live in core
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                ws.receive_json()
        assert exc.value.code == 1008, reason
        assert time.monotonic() - started < 2
    wait_detached(client, session)
    for _ in range(100):
        if keep_alive or mgr.get(session.id) is None:
            break
        client.portal.call(asyncio.sleep, 0.02)
    assert (mgr.get(session.id) is session) == bool(keep_alive)
    if keep_alive:
        assert session.pty.poll() is None


@pytest.mark.parametrize("kind", ["unknown", "expired", "replayed", "wrong-first-frame"])
def test_invalid_ticket_never_binds_or_starts_shell(host, kind):
    client, mgr, guard, bound, cfg = host
    ticket = mint(client)
    if kind == "unknown":
        ticket = "unknown"
    elif kind == "expired":
        api._tickets[ticket] = api._Ticket(0, "trusted-device")
    elif kind == "replayed":
        assert api.consume_ticket(ticket)
    with client.websocket_connect("/plugins/terminal/ws") as ws:
        ws.send_json({"type": "input" if kind == "wrong-first-frame" else "auth", "ticket": ticket})
        assert ws.receive_json()["message"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4001
    assert not bound
    assert not mgr.list()


@pytest.mark.parametrize("mode", ["operator-ticket", "bearer", "legacy-fallback", "loopback"])
def test_nondevice_paths_remain_unbound(host, monkeypatch, mode):
    client, mgr, guard, bound, cfg = host
    hello = {"type": "auth", "device_id": "forged"}
    if mode == "operator-ticket":
        hello["ticket"] = mint(client, "operator")
    elif mode == "loopback":
        monkeypatch.delenv("A2A_AUTH_TOKEN")
        hello["ticket"] = "junk"
    else:
        hello["token"] = "operator"
        if mode == "legacy-fallback":
            hello["ticket"] = "junk"
    guard.live = False
    with client.websocket_connect("/plugins/terminal/ws") as ws:
        ws.send_json(hello)
        assert ws.receive_json()["type"] == "connected"
        client.portal.call(asyncio.sleep, 0.06)
        ws.send_json({"type": "ping"})
        while ws.receive_json()["type"] != "pong":
            pass
        assert not bound
        assert not guard.scopes[-1]["state"].get("device_id")


def test_unauthenticated_mint_cannot_capture_identity(host):
    client, mgr, guard, bound, cfg = host
    assert client.post("/api/plugins/terminal/ticket", headers={"X-Device-Id": "forged"}).status_code == 401
    assert not api._tickets
