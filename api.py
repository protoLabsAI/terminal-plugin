"""Terminal HTTP + WebSocket — the view page and the PTY bridge.

ONE router on the PUBLIC ``/plugins/terminal`` prefix (the page is an iframe load that
can't carry a bearer). The WebSocket verifies the operator bearer ITSELF from a
``?token=`` query param against the host's configured token (``auth.token`` /
``A2A_AUTH_TOKEN``) — a browser WS can't set an Authorization header. When the host has
no bearer set (loopback dev) the WS is open on the bound interface and a warning fires.

Wire protocol (JSON, modelled on protoMaker's terminal):
  client → server: {input, data} · {resize, cols, rows} · {ping}
  server → client: {connected, shell, cwd} · {data, data} · {exit, exitCode} · {pong}
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path

from fastapi import WebSocket  # module-level so the websocket route's annotation resolves

from .pty_session import open_session
from .view import PAGE

log = logging.getLogger("protoagent.plugins.terminal")

# Vendored xterm assets served locally (offline — no CDN). Whitelisted by name.
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
_VENDOR_TYPES = {
    "xterm.js": "application/javascript",
    "xterm.css": "text/css",
    "addon-fit.js": "application/javascript",
    "addon-web-links.js": "application/javascript",
    "addon-canvas.js": "application/javascript",
}


def expected_token() -> str:
    """The operator bearer a WS must match — the host's configured token. Lazy host
    import so the suite stays host-free; falls back to the ``A2A_AUTH_TOKEN`` env."""
    tok = ""
    try:
        from runtime.state import STATE

        if STATE.graph_config is not None:
            tok = getattr(STATE.graph_config, "auth_token", "") or ""
    except Exception:  # noqa: BLE001 — no host (tests) → fall through to the env
        tok = ""
    return (tok or os.environ.get("A2A_AUTH_TOKEN", "")).strip()


def verify_token(expected: str, provided: str) -> bool:
    """May this WS connect? No host token configured ⇒ open (loopback dev). Otherwise
    the provided ``?token=`` must match the operator bearer (constant-time compare)."""
    if not expected:
        return True
    return bool(provided) and hmac.compare_digest(expected, provided)


def scrub_keys() -> list[str]:
    """Host secrets to strip from the child shell's env so they don't leak into it."""
    agent = os.environ.get("AGENT_NAME", "protoagent").upper()
    return [f"{agent}_API_KEY", "A2A_AUTH_TOKEN", "WORKSTACEAN_API_KEY"]


def build_router(cfg: dict):
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    router = APIRouter()
    shell = (cfg or {}).get("shell") or ""
    cwd = (cfg or {}).get("cwd") or ""

    @router.get("/view", response_class=HTMLResponse)
    async def _view():
        return HTMLResponse(PAGE)

    @router.get("/static/{name}")
    async def _static(name: str):
        # Vendored xterm assets (offline). Whitelisted — no path traversal.
        media = _VENDOR_TYPES.get(name)
        path = _VENDOR_DIR / name
        if media is None or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type=media)

    @router.websocket("/ws")
    async def _ws(ws: WebSocket):
        if not verify_token(expected_token(), ws.query_params.get("token", "")):
            await ws.close(code=4001)  # auth failed — bad/missing operator bearer
            return
        if not expected_token():
            log.warning(
                "[terminal] WS connected with NO operator bearer set — the shell is open on "
                "the bound interface; set auth.token / A2A_AUTH_TOKEN before exposing it"
            )
        await ws.accept()
        await _bridge(ws, shell=shell, cwd=cwd)

    return router


async def _bridge(ws, *, shell: str, cwd: str) -> None:
    """Bridge a WebSocket to a fresh PTY for its lifetime."""
    from fastapi import WebSocketDisconnect

    sess = open_session(shell=shell, cwd=cwd, scrub_env=scrub_keys())
    try:
        sess.start()
    except Exception as exc:  # noqa: BLE001
        await _safe_send(ws, {"type": "exit", "exitCode": 1, "error": f"failed to start shell: {exc}"})
        await _safe_close(ws)
        return
    await _safe_send(ws, {"type": "connected", "shell": sess.shell, "cwd": sess.cwd})

    async def _pump_out():
        # Shell output → ws, until EOF (child exit), then announce exit + close so the
        # receive loop below unblocks.
        while True:
            chunk = await sess.read()
            if not chunk:
                code = sess.poll()
                await _safe_send(ws, {"type": "exit", "exitCode": code if code is not None else 0})
                await _safe_close(ws)
                return
            await _safe_send(ws, {"type": "data", "data": chunk.decode("utf-8", "replace")})

    out_task = asyncio.create_task(_pump_out())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "input":
                sess.write(msg.get("data", ""))
            elif kind == "resize":
                sess.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
            elif kind == "ping":
                await _safe_send(ws, {"type": "pong"})
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        log.warning("[terminal] WS bridge error", exc_info=True)
    finally:
        out_task.cancel()
        await sess.aclose()
        await _safe_close(ws)


async def _safe_send(ws, obj: dict) -> None:
    try:
        await ws.send_text(json.dumps(obj))
    except Exception:  # noqa: BLE001
        pass


async def _safe_close(ws) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
