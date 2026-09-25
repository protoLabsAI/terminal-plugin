"""Persistent terminal sessions — a shell outlives the WebSocket that opened it.

Before v0.5.0 a PTY lived exactly as long as its WebSocket, so anything that dropped
the socket — switching rail views, a reload, a network blip — killed the shell and
whatever was running in it. Now the ``SessionManager`` owns the PTYs:

- Each ``Session`` runs its OWN output pump (independent of any socket) into a bounded
  replay buffer, so output produced while nobody is watching is kept.
- A WebSocket *attaches* to a session (new, or an existing one by id). Attaching
  replays the buffer, then streams live. One viewer at a time — a second attach takes
  the session over and the first socket is told it was detached.
- A socket dropping only *detaches*. The shell keeps running; after
  ``keep_alive`` seconds with no viewer it is reaped. ``keep_alive=0`` restores the
  old behaviour (kill on disconnect).
- An explicit close (the tab's ×) kills the shell at once.

Ordering: a viewer is an ``asyncio.Queue`` drained by the socket's writer task.
``attach`` enqueues the replay and installs the queue in one synchronous step, and
the pump only ever enqueues, so replay-then-live can never interleave or drop output.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import secrets
import time
from collections import deque

from .pty_session import open_session
from .textutil import alt_screen_after

log = logging.getLogger("protoagent.plugins.terminal")

DEFAULT_BUFFER_CHARS = 256 * 1024  # replay buffer per session (decoded text)
HIGH_WATER = 64  # frames queued for a viewer before the pump stops reading the PTY
REDRAW_NUDGE = 0.05  # seconds between the two halves of a resume redraw nudge


class SessionLimitError(Exception):
    """Too many live sessions — refuse to spawn another shell."""


class Session:
    """One live shell + its replay buffer + (at most) one attached viewer."""

    def __init__(self, sid: str, pty, *, buffer_chars: int = DEFAULT_BUFFER_CHARS):
        self.id = sid
        self.pty = pty
        self.detached_at: float | None = time.monotonic()  # no viewer yet
        self.exit_code: int | None = None
        self.exited = False
        self._buffer: deque[str] = deque()
        self._buffered = 0
        self._buffer_chars = max(1024, int(buffer_chars))
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._viewer: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None
        self._redraw_pending = False
        self.alt_screen = False  # a full-screen program (vim, less, htop) is up

    @property
    def shell(self) -> str:
        return self.pty.shell

    @property
    def cwd(self) -> str:
        return self.pty.cwd

    @property
    def attached(self) -> bool:
        return self._viewer is not None

    # ── output ───────────────────────────────────────────────────────────────────
    def start_pump(self, on_exit) -> None:
        self._pump = asyncio.create_task(self._run_pump(on_exit))

    async def _run_pump(self, on_exit) -> None:
        # Shell output → buffer + the attached viewer, until EOF (the child exited).
        try:
            while True:
                chunk = await self.pty.read()
                if not chunk:
                    break
                # Incremental decode: a multi-byte char split across two reads must not
                # turn into two U+FFFD replacement glyphs.
                self._emit(self._decoder.decode(chunk))
                # Flow control: while the viewer is behind, stop reading. The PTY buffer
                # fills and the program blocks on write — what a real terminal does —
                # instead of an unbounded queue growing under `yes` or a huge `cat`.
                while self._viewer is not None and self._viewer.qsize() > HIGH_WATER:
                    await asyncio.sleep(0.01)
            self._emit(self._decoder.decode(b"", final=True))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a read failure ends the session like EOF
            log.debug("[terminal] session %s read failed", self.id, exc_info=True)
        code = self.pty.poll()
        self.exit_code = code if code is not None else 0
        self.exited = True
        self._send({"type": "exit", "exitCode": self.exit_code})
        on_exit(self)

    def _emit(self, text: str) -> None:
        if not text:
            return
        if "\x1b[?" in text:
            self.alt_screen = alt_screen_after(text, self.alt_screen)
        self._buffer.append(text)
        self._buffered += len(text)
        while self._buffered > self._buffer_chars and len(self._buffer) > 1:
            self._buffered -= len(self._buffer.popleft())
        if self._buffered > self._buffer_chars:  # one oversized chunk — keep its tail
            only = self._buffer.popleft()[-self._buffer_chars :]
            self._buffer.append(only)
            self._buffered = len(only)
        self._send({"type": "data", "data": text})

    def replay(self) -> str:
        """The buffered output. When the buffer has wrapped, start at a line boundary so
        the replay doesn't open mid-escape-sequence."""
        text = "".join(self._buffer)
        if self._buffered >= self._buffer_chars:
            nl = text.find("\n")
            if 0 <= nl < len(text) - 1:
                text = text[nl + 1 :]
        return text

    def _send(self, msg: dict) -> None:
        if self._viewer is not None:
            self._viewer.put_nowait(msg)

    # ── viewers ──────────────────────────────────────────────────────────────────
    def attach(self, queue: asyncio.Queue, *, resumed: bool) -> None:
        """Make ``queue`` THE viewer: kick any previous one, enqueue ``connected`` +
        the replay, then live output. Synchronous — nothing can interleave."""
        if self._viewer is not None and self._viewer is not queue:
            self._viewer.put_nowait({"type": "detached", "reason": "attached elsewhere"})
        self._viewer = queue
        self.detached_at = None
        # A reattaching viewer replays raw output, which can't faithfully rebuild a
        # full-screen app's screen (the alt-screen switch may be long gone from the
        # buffer). Ask the program to repaint on the viewer's first resize — only when a
        # full-screen program is up: at a plain prompt the replay is already right, and
        # an extra SIGWINCH just makes zsh reprint its prompt (a stray "%" per reload).
        self._redraw_pending = resumed and self.alt_screen
        queue.put_nowait(
            {"type": "connected", "session": self.id, "shell": self.shell, "cwd": self.cwd, "resumed": resumed}
        )
        backlog = self.replay()
        if backlog:
            queue.put_nowait({"type": "data", "data": backlog})
        if self.exited:
            queue.put_nowait({"type": "exit", "exitCode": self.exit_code})

    def detach(self, queue: asyncio.Queue) -> bool:
        """Drop ``queue`` if it is still the viewer. False when a takeover already
        replaced it — the session belongs to someone else now, so leave it be."""
        if self._viewer is not queue:
            return False
        self._viewer = None
        self.detached_at = time.monotonic()
        return True

    # ── input ────────────────────────────────────────────────────────────────────
    def write(self, data) -> None:
        self.pty.write(data)

    def resize(self, cols: int, rows: int) -> None:
        if self._redraw_pending and rows > 1:
            # Two size changes → SIGWINCH even when the final size equals the old one, so
            # vim/htop/less/TUI agents redraw their whole screen for the new viewer.
            self._redraw_pending = False
            self.pty.resize(cols, rows - 1)
            asyncio.get_running_loop().call_later(REDRAW_NUDGE, self.pty.resize, cols, rows)
            return
        self._redraw_pending = False
        self.pty.resize(cols, rows)

    async def aclose(self) -> None:
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.pty.aclose()


class SessionManager:
    """The registry of live sessions (one per server process)."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._reaper: asyncio.Task | None = None

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self._sessions.get(sid)
        return None if s is None or s.exited else s

    def create(
        self,
        *,
        shell: str = "",
        cwd: str = "",
        cols: int = 80,
        rows: int = 24,
        scrub_env: list[str] | None = None,
        login: bool = False,
        max_sessions: int = 0,
        buffer_chars: int = DEFAULT_BUFFER_CHARS,
    ) -> Session:
        """Spawn a shell and start its pump. Raises ``SessionLimitError`` past
        ``max_sessions`` (0 = unlimited) and whatever the PTY raises on spawn."""
        if max_sessions and len(self._sessions) >= max_sessions:
            raise SessionLimitError(f"session limit reached ({max_sessions}) — close a terminal first")
        pty = open_session(shell=shell, cwd=cwd, cols=cols, rows=rows, scrub_env=scrub_env, login=login)
        pty.start()
        sid = secrets.token_urlsafe(12)
        sess = Session(sid, pty, buffer_chars=buffer_chars)
        self._sessions[sid] = sess
        sess.start_pump(self._forget)
        return sess

    def _forget(self, sess: Session) -> None:
        # The shell exited on its own: drop it from the registry (its attached viewer
        # already got the exit frame) and reap the PTY in the background.
        if self._sessions.get(sess.id) is sess:
            del self._sessions[sess.id]
        asyncio.get_running_loop().create_task(sess.pty.aclose())

    async def close(self, sid: str) -> None:
        sess = self._sessions.pop(sid, None)
        if sess is not None:
            await sess.aclose()

    async def close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        for sid in list(self._sessions):
            await self.close(sid)

    async def reap_detached(self, keep_alive: float) -> int:
        """Close every session that has had no viewer for longer than ``keep_alive``
        seconds. Returns how many were reaped."""
        now = time.monotonic()
        stale = [
            s.id for s in self._sessions.values() if s.detached_at is not None and now - s.detached_at >= keep_alive
        ]
        for sid in stale:
            log.info("[terminal] reaping detached session %s", sid)
            await self.close(sid)
        return len(stale)

    def ensure_reaper(self, keep_alive_fn, interval: float = 30.0) -> None:
        """Start the periodic reaper on the running loop (idempotent). ``keep_alive_fn``
        is read each tick so a Settings change applies without a restart."""
        if self._reaper is not None and not self._reaper.done():
            return

        async def _loop():
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.reap_detached(keep_alive_fn())
                except Exception:  # noqa: BLE001
                    log.warning("[terminal] reaper tick failed", exc_info=True)

        self._reaper = asyncio.get_running_loop().create_task(_loop())


MANAGER = SessionManager()
