"""Terminal HTTP + WebSocket — the view page and the PTY bridge.

ONE router on the PUBLIC ``/plugins/terminal`` prefix (the page is an iframe load that
can't carry a bearer). The WebSocket verifies the operator bearer ITSELF from a
``?token=`` query param against the host's configured token (``auth.token`` /
``A2A_AUTH_TOKEN``) — a browser WS can't set an Authorization header. When the host has
no bearer set (loopback dev) the WS is open on the bound interface and a warning fires.

AUTH is the FIRST message, not the URL: a ``?token=`` rides in access logs, browser
history and proxies (the view bridge's own rule is "never a token in the URL"). The
socket is accepted, then must send ``{type:"auth", token, session?, cols?, rows?}``
within ``AUTH_TIMEOUT`` seconds or it is closed with 4001.

SESSIONS outlive the socket (see ``sessions.py``): ``auth.session`` reattaches to a
live shell (replaying its buffered output); none/unknown spawns a new one. A dropped
socket only detaches; ``{type:"close"}`` kills the shell.

Wire protocol (JSON, modelled on protoMaker's terminal):
  client → server: {auth, token, session?, cols?, rows?} · {input, data} ·
                   {resize, cols, rows} · {ping} · {close}
  server → client: {connected, session, shell, cwd, resumed} · {data, data} ·
                   {exit, exitCode} · {detached, reason} · {error, message} · {pong}
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path

from fastapi import WebSocket  # module-level so the websocket route's annotation resolves

from .sessions import MANAGER, SessionLimitError
from .view import PAGE

log = logging.getLogger("protoagent.plugins.terminal")

AUTH_TIMEOUT = 10.0  # seconds a fresh socket has to send its auth frame

# Config defaults — mirror protoagent.plugin.yaml's ``config:`` block.
DEFAULTS = {
    "shell": "",
    "cwd": "",
    "scrollback": 5000,
    "font_size": 13,
    "keep_alive_minutes": 30,
    "max_sessions": 12,
}

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


def _num(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def resolve(cfg: dict | None) -> dict:
    """The effective config: defaults, overlaid with the set (non-blank) values, with
    the numbers clamped to sane ranges."""
    raw = {**DEFAULTS, **{k: v for k, v in (cfg or {}).items() if v not in (None, "")}}
    return {
        "shell": str(raw["shell"] or ""),
        "cwd": str(raw["cwd"] or ""),
        "scrollback": _num(raw["scrollback"], DEFAULTS["scrollback"], 100, 200_000),
        "font_size": _num(raw["font_size"], DEFAULTS["font_size"], 8, 32),
        "keep_alive_minutes": _num(raw["keep_alive_minutes"], DEFAULTS["keep_alive_minutes"], 0, 7 * 24 * 60),
        "max_sessions": _num(raw["max_sessions"], DEFAULTS["max_sessions"], 1, 64),
    }


def render_page(cfg: dict) -> str:
    """The view page with the client-side config baked in (font size + scrollback), so
    a Settings change shows up on the next view load."""
    client = {"fontSize": cfg["font_size"], "scrollback": cfg["scrollback"]}
    # json.dumps output is safe inside <script> once "</" can't close the tag.
    return PAGE.replace("__TERMINAL_CONFIG__", json.dumps(client).replace("</", "<\\/"))


def build_router(cfg):
    """The public router. ``cfg`` is the config dict, or a zero-arg callable returning
    it (``registry.live_config``) so Settings edits apply without a restart."""
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    router = APIRouter()
    current = cfg if callable(cfg) else (lambda: cfg)

    def conf() -> dict:
        try:
            return resolve(current())
        except Exception:  # noqa: BLE001 — a broken config read must not take the terminal down
            log.warning("[terminal] reading config failed; using defaults", exc_info=True)
            return resolve({})

    @router.get("/view", response_class=HTMLResponse)
    async def _view():
        return HTMLResponse(render_page(conf()), headers={"Cache-Control": "no-store"})

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
        await ws.accept()
        hello = await _receive_auth(ws)
        if hello is None or not verify_token(expected_token(), str(hello.get("token") or "")):
            await _safe_send(ws, {"type": "error", "message": "unauthorized"})
            await _safe_close(ws, code=4001)  # bad/missing operator bearer
            return
        if not expected_token():
            log.warning(
                "[terminal] WS connected with NO operator bearer set — the shell is open on "
                "the bound interface; set auth.token / A2A_AUTH_TOKEN before exposing it"
            )
        await _bridge(ws, hello, conf)

    return router


async def _receive_auth(ws) -> dict | None:
    """The socket's first frame, if it is a well-formed auth frame in time."""
    try:
        msg = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT))
    except Exception:  # noqa: BLE001 — timeout, disconnect, junk → unauthenticated
        return None
    return msg if isinstance(msg, dict) and msg.get("type") == "auth" else None


async def _bridge(ws, hello: dict, conf) -> None:
    """Attach a WebSocket to a session (existing by id, else a fresh shell) for the
    socket's lifetime. Disconnecting detaches; ``{type:"close"}`` kills the shell."""
    from fastapi import WebSocketDisconnect

    cfg = conf()
    # The reaper re-reads the config each tick, so a Settings change applies live.
    MANAGER.ensure_reaper(lambda: conf()["keep_alive_minutes"] * 60)

    sess = MANAGER.get(str(hello.get("session") or ""))
    resumed = sess is not None
    if sess is None:
        try:
            sess = MANAGER.create(
                shell=cfg["shell"],
                cwd=cfg["cwd"],
                cols=_num(hello.get("cols"), 80, 1, 1000),
                rows=_num(hello.get("rows"), 24, 1, 1000),
                scrub_env=scrub_keys(),
                max_sessions=cfg["max_sessions"],
            )
        except SessionLimitError as exc:
            await _safe_send(ws, {"type": "error", "message": str(exc)})
            await _safe_close(ws)
            return
        except Exception as exc:  # noqa: BLE001
            await _safe_send(ws, {"type": "exit", "exitCode": 1, "error": f"failed to start shell: {exc}"})
            await _safe_close(ws)
            return

    queue: asyncio.Queue = asyncio.Queue()
    sess.attach(queue, resumed=resumed)
    writer = asyncio.create_task(_write_out(ws, queue))
    killed = False
    try:
        while not writer.done():
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "input":
                data = msg.get("data")
                if isinstance(data, str):  # anything else is a malformed frame — ignore it
                    sess.write(data)
            elif kind == "resize":
                sess.resize(_num(msg.get("cols"), 80, 1, 1000), _num(msg.get("rows"), 24, 1, 1000))
            elif kind == "ping":
                queue.put_nowait({"type": "pong"})
            elif kind == "close":
                killed = True
                break
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        log.warning("[terminal] WS bridge error", exc_info=True)
    finally:
        writer.cancel()
        try:
            await writer  # let it finish unwinding before the shell (and its fd) go away
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        still_ours = sess.detach(queue)
        # Re-read keep-alive NOW, not at connect: a Settings change made while this tab
        # was open must govern what its disconnect does.
        if killed or (still_ours and not conf()["keep_alive_minutes"]):
            await MANAGER.close(sess.id)
        await _safe_close(ws)


async def _write_out(ws, queue: asyncio.Queue) -> None:
    """Drain a viewer queue onto its socket. Ends (closing the socket) after an
    ``exit`` or ``detached`` frame — the session is over or moved elsewhere."""
    while True:
        msg = await queue.get()
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:  # noqa: BLE001 — socket gone; the receive loop sees the disconnect
            return
        if msg.get("type") in ("exit", "detached"):
            await _safe_close(ws)
            return


async def _safe_send(ws, obj: dict) -> None:
    try:
        await ws.send_text(json.dumps(obj))
    except Exception:  # noqa: BLE001
        pass


async def _safe_close(ws, code: int = 1000) -> None:
    try:
        await ws.close(code=code)
    except Exception:  # noqa: BLE001
        pass
