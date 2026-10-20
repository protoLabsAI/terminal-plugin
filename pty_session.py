"""A PTY-backed shell session.

Two backends behind one interface (start / read / write / resize / poll / aclose):
- ``PtySession`` — POSIX (Linux/macOS), stdlib only (``pty``/``os``/``fcntl``/
  ``termios``); no pip deps, so the suite spawns real PTYs in CI.
- ``WinPtySession`` — Windows, via the optional ``pywinpty`` package (``requires_pip`` in
  the manifest); validated by the CI windows job (tests/test_winpty.py, a real cmd.exe).

``open_session(...)`` picks the right backend for the platform. The POSIX session owns
a child shell behind a pseudo-terminal: read its output off the master fd, write
keystrokes, resize (``TIOCSWINSZ``), reap the group.

**No PTY I/O ever runs on the event loop.** Reads AND writes each have their own
per-session thread. A write to a PTY blocks whenever the child stops reading its input
(a program in raw mode that never reads, a full input queue) — and, worse, the child can
only consume input as fast as its echo/redraw OUTPUT is drained, which is the loop's own
pump. A write on the loop therefore deadlocks the whole server: the loop waits on the
child, the child waits on the loop (terminal-plugin#20 — a member frozen until restart).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import logging
import os
import select
import signal
import struct
import sys
import threading
from collections import deque
from collections.abc import Callable

# Unix PTY primitives — guarded so the module still imports on Windows (which uses the
# pywinpty backend). The POSIX PtySession references these only at runtime, on POSIX.
if sys.platform != "win32":
    import fcntl
    import pty
    import termios

# Default TERM env so colour + 256-colour CLIs behave inside the terminal.
_TERM_ENV = {
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "TERM_PROGRAM": "protoagent-terminal",
}


def utf8_locale(env: dict[str, str]) -> dict[str, str]:
    """A UTF-8 locale for the child when the server has none. A server launched from a GUI
    (the desktop app, launchd) often has no LANG at all, so the shell falls back to the C
    locale: UTF-8 input/output, emoji, and box-drawing break, and tools like `ls` mangle
    names. Only fills a gap — an effective UTF-8 locale is left exactly as it is.

    POSIX precedence is LC_ALL > LC_CTYPE > LANG, so setting LANG alone would be silently
    shadowed by a non-UTF-8 LC_ALL / LC_CTYPE (e.g. "C"); those are overridden too."""
    utf8 = lambda v: "utf-8" in v.lower() or "utf8" in v.lower()  # noqa: E731
    current = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if utf8(current):
        return {}
    value = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    out = {"LANG": value}
    for key in ("LC_ALL", "LC_CTYPE"):
        if env.get(key) and not utf8(env[key]):
            out[key] = value
    return out


def login_argv0(shell: str) -> str:
    """argv[0] for a LOGIN shell: the basename with a leading dash ("-zsh") — the
    convention every terminal (Terminal.app, iTerm, login(1)) uses. A login shell reads
    ~/.zprofile / ~/.bash_profile, so PATH (Homebrew, pyenv, nvm…) matches the operator's
    usual terminal even when the server itself was started with a bare environment."""
    return "-" + os.path.basename(shell)


# Path helpers behind one seam, so a test can swap in ``ntpath`` to exercise the
# Windows spelling (``%USERPROFILE%``) on a POSIX runner.
_ospath = os.path


def home_dir() -> str:
    """The user's home folder (``~`` — ``$HOME`` on POSIX, ``%USERPROFILE%`` on
    Windows), or the server's working directory when home can't be resolved."""
    try:
        home = _ospath.expanduser("~")
    except Exception:  # noqa: BLE001 — a broken passwd entry etc.; fall back below
        home = ""
    if home and home != "~" and _ospath.isdir(home):
        return home
    return os.getcwd()


def display_path(path: str) -> str:
    """A path as a tab label: under home it's ``~``-relative ("~", "~/dev/app") — what
    shells print in their titles — else the path as-is."""
    if not path:
        return ""
    try:
        # Normalize FIRST and work on the normalized forms throughout — slicing the raw
        # path by the home prefix's length breaks on "./" / "../" / doubled separators.
        clean = _ospath.normpath(path)
        home = _ospath.normpath(home_dir())
    except (TypeError, ValueError):
        return path
    norm, h = _ospath.normcase(clean), _ospath.normcase(home)
    if norm == h:
        return "~"
    sep = _ospath.sep
    prefix = h.rstrip(sep) + sep
    if norm.startswith(prefix):
        return "~" + sep + clean[len(prefix) :]
    return clean


def resolve_cwd(cwd: str = "") -> tuple[str, str]:
    """Where a new shell starts, plus a one-line notice ('' when none).

    Blank → the user's home folder (NOT the server's cwd: the desktop app launches its
    servers from ``/``). A configured path has ``~`` and env vars expanded
    (``~/code``, ``$HOME/code``, ``%USERPROFILE%\\code``); if it isn't an existing
    directory the shell starts in home instead and the notice says so."""
    home = home_dir()
    raw = (cwd or "").strip()
    if not raw:
        return home, ""
    path = _ospath.expandvars(_ospath.expanduser(raw))
    if _ospath.isdir(path):
        return path, ""
    return home, f"Starting directory {raw!r} not found — started in {home}"


def default_shell() -> str:
    """The shell to spawn when none is configured: ``$SHELL`` then ``/bin/bash``."""
    return os.environ.get("SHELL") or "/bin/bash"


log = logging.getLogger("protoagent.plugins.terminal")

WRITE_CHUNK = 1024  # bytes per write syscall — small, so a stall is noticed between chunks
MAX_PENDING_INPUT = 4 << 20  # queued-but-unwritten input per session before keystrokes drop
_POLL = 0.2  # seconds the writer thread waits for the PTY to accept input before rechecking


class PtyError(Exception):
    """A PTY lifecycle failure (start / resize / a closed session)."""


class PtyWriteTimeout(PtyError):
    """The PTY stopped accepting input: ``written`` of ``total`` bytes went in before the
    deadline. The rest is abandoned — never typed later, except at most the one chunk
    (``WRITE_CHUNK``) already handed to the kernel, which lands if the PTY drains."""

    def __init__(self, written: int, total: int, timeout: float):
        self.written, self.total, self.timeout = written, total, timeout
        super().__init__(f"the terminal stopped accepting input — {written} of {total} bytes written in {timeout:g}s")


class _Input:
    __slots__ = ("data", "done", "written", "cancelled")

    def __init__(self, data: bytes, done: concurrent.futures.Future | None):
        self.data, self.done, self.written, self.cancelled = data, done, 0, False


class InputWriter:
    """Feeds input to a PTY from ONE dedicated thread, strictly in order, so the event
    loop never makes a write syscall. ``write_chunk(bytes) -> int`` does the (possibly
    blocking) write; ``ready(timeout) -> bool`` says whether the PTY can take input now
    (POSIX: ``select``), so the thread notices a close/cancel between waits instead of
    sitting in a blocked write.

    ``submit`` never blocks. A submit carrying a Future resolves it when every byte is
    in (or fails it); ``cancel`` abandons an item that has not finished."""

    def __init__(self, write_chunk: Callable[[bytes], int], ready: Callable[[float], bool] | None = None, *, name=""):
        self._write_chunk = write_chunk
        self._ready = ready or (lambda _t: True)
        self._name = name or "terminal-input"
        self._cond = threading.Condition()
        self._queue: deque[_Input] = deque()
        self._pending = 0  # bytes queued but not yet written
        self._closed = False
        self._warned = False  # one warning per backlog-full episode, not one per keystroke
        self._thread: threading.Thread | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def submit(self, data: bytes, done: concurrent.futures.Future | None = None) -> _Input | None:
        """Queue ``data``. None (and ``done`` failed) when the writer is closed or the
        backlog is full — keystrokes into a PTY that has stopped reading are dropped
        rather than queued without bound."""
        with self._cond:
            if self._closed or self._pending + len(data) > MAX_PENDING_INPUT:
                reason = "the terminal is closed" if self._closed else "the terminal's input backlog is full"
                if done is not None and not done.done():
                    done.set_exception(PtyError(reason))
                elif not self._closed and not self._warned:
                    self._warned = True
                    log.warning("[terminal] input dropped: %s (the shell isn't reading)", reason)
                return None
            self._warned = False
            item = _Input(data, done)
            self._queue.append(item)
            self._pending += len(data)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._cond.notify()
            return item

    def cancel(self, item: _Input) -> None:
        with self._cond:
            item.cancelled = True

    def close(self, join: float = 0.0) -> None:
        """Stop: fail whatever is queued and let the thread exit. ``join`` waits that
        long for it (so the caller can close the fd only once nothing can write to it)."""
        with self._cond:
            self._closed = True
            pending = list(self._queue)
            self._queue.clear()
            self._pending = 0
            self._cond.notify_all()
        for item in pending:
            _fail(item, PtyError("the terminal is closed"))
        t = self._thread
        if join and t is not None and t is not threading.current_thread():
            t.join(join)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                item = self._queue[0]
            err = self._feed(item)
            with self._cond:
                if self._queue and self._queue[0] is item:
                    self._queue.popleft()
                    self._pending -= len(item.data)
                if err is not None:  # the PTY is gone — nothing queued can be written
                    self._closed = True
                    rest = list(self._queue)
                    self._queue.clear()
                    self._pending = 0
            if err is not None:
                _fail(item, err)
                for other in rest:
                    _fail(other, err)
                return
            if item.cancelled:
                _fail(item, PtyError("cancelled"))
            elif item.done is not None and not item.done.done():
                item.done.set_result(item.written)

    def _feed(self, item: _Input) -> Exception | None:
        """Write one item, chunk by chunk. None on success or cancel; the error when the
        PTY is gone (EIO / EBADF / closed)."""
        view = memoryview(item.data)
        while item.written < len(view):
            if self._closed:
                return PtyError("the terminal is closed")
            if item.cancelled:
                return None
            try:
                if not self._ready(_POLL):
                    continue  # still full — recheck closed/cancelled, then wait again
                n = self._write_chunk(bytes(view[item.written : item.written + WRITE_CHUNK]))
            except InterruptedError:
                continue
            except BlockingIOError:  # aclose() switched the fd to non-blocking to drain it
                return PtyError("the terminal is closing")
            except Exception as exc:  # noqa: BLE001 — EIO (child gone), EBADF, a backend error
                return PtyError(f"the terminal stopped accepting input: {exc}")
            item.written += max(0, int(n or 0))
        return None


def _fail(item: _Input, exc: Exception) -> None:
    if item.done is not None and not item.done.done():
        item.done.set_exception(exc)


async def send_input(writer: InputWriter, data: bytes, timeout: float) -> int:
    """Queue ``data`` on ``writer`` and wait (without blocking the loop) until it is all
    written. Raises ``PtyWriteTimeout`` after ``timeout`` seconds — the unwritten rest is
    abandoned so a half-typed command can't finish typing itself later — and
    ``PtyError`` when the terminal is closed or gone."""
    done: concurrent.futures.Future = concurrent.futures.Future()
    item = writer.submit(data, done)
    fut = asyncio.wrap_future(done)
    # After a timeout nobody awaits it any more; consume its late outcome quietly.
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())
    if item is None:
        return await fut  # raises the submit failure
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except asyncio.TimeoutError:
        writer.cancel(item)
        raise PtyWriteTimeout(item.written, len(data), timeout) from None


class PtySession:
    """One child shell behind a PTY. Construct, ``start()``, then ``read()`` /
    ``write()`` / ``resize()`` / ``aclose()``."""

    def __init__(
        self,
        *,
        shell: str = "",
        cwd: str = "",
        cols: int = 80,
        rows: int = 24,
        env_overrides: dict[str, str] | None = None,
        scrub_env: list[str] | None = None,
        login: bool = False,
    ):
        self.shell = shell or default_shell()
        self.login = bool(login)
        self.cwd, self.cwd_notice = resolve_cwd(cwd)
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self._env_overrides = env_overrides or {}
        self._scrub_env = set(scrub_env or [])
        self.pid: int | None = None
        self._fd: int | None = None
        self._exit_code: int | None = None
        self._writer: InputWriter | None = None
        # Reads get their OWN thread: a pump parks in os.read for the session's whole life,
        # and on the loop's shared default executor every open tab would hold one of its
        # few workers — enough tabs and the host's to_thread / DNS calls starve.
        self._reads: concurrent.futures.ThreadPoolExecutor | None = None

    # ── env ─────────────────────────────────────────────────────────────────────
    def _build_env(self) -> dict[str, str]:
        """The child's env: the server's env + TERM defaults + overrides, minus the
        scrubbed keys (so the operator's own secrets don't leak into the shell)."""
        env = {k: v for k, v in os.environ.items() if k not in self._scrub_env}
        env.update(_TERM_ENV)
        env.update(utf8_locale(env))
        env.update(self._env_overrides)
        return env

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Fork a child shell on a fresh PTY (the child is its own session leader, so
        the whole process group can be signalled on close). Raises ``PtyError``."""
        if self.pid is not None:
            raise PtyError("session already started")
        env = self._build_env()
        try:
            pid, fd = pty.fork()
        except OSError as exc:
            raise PtyError(f"pty.fork failed: {exc}")
        if pid == 0:  # child — becomes the shell (or exits 127 if exec fails)
            try:
                os.chdir(self.cwd)
            except OSError:
                pass
            try:
                argv0 = login_argv0(self.shell) if self.login else self.shell
                os.execvpe(self.shell, [argv0], env)
            except OSError:
                os._exit(127)
        self.pid = pid
        self._fd = fd
        self._writer = InputWriter(self._write_chunk, self._writable, name=f"terminal-input-{pid}")
        self._reads = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"terminal-read-{pid}")
        self.resize(self.cols, self.rows)

    async def read(self, n: int = 65536) -> bytes:
        """A chunk of shell output; ``b""`` on EOF (the child exited). The blocking
        read runs on the session's own thread so the event loop keeps serving."""
        if self._fd is None or self._reads is None:
            return b""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._reads, self._blocking_read, n)
        except RuntimeError:  # the read executor was shut down by aclose()
            return b""

    def _blocking_read(self, n: int) -> bytes:
        try:
            return os.read(self._fd, n)
        except OSError as exc:
            # On Linux the PTY master read raises EIO (not EOF) once the child exits; EAGAIN/
            # EBADF mean aclose() has taken the fd over (drain → close) — the session is over.
            if exc.errno in (errno.EIO, errno.EAGAIN, errno.EBADF):
                return b""
            raise

    def write(self, data: str | bytes) -> None:
        """Send keystrokes/paste to the shell. Never blocks: the bytes are queued for the
        session's input thread (a shell that has stopped reading can't stall the loop)."""
        if self._writer is None or self._fd is None:
            return
        self._writer.submit(_as_bytes(data))

    async def send(self, data: str | bytes, timeout: float = 10.0) -> int:
        """Type ``data`` and wait until the PTY has taken all of it. Raises
        ``PtyWriteTimeout`` when it stops accepting input for ``timeout`` seconds, and
        ``PtyError`` when the session is closed."""
        if self._writer is None or self._fd is None:
            raise PtyError("the terminal is closed")
        return await send_input(self._writer, _as_bytes(data), timeout)

    def _writable(self, timeout: float) -> bool:
        fd = self._fd
        if fd is None:
            raise PtyError("the terminal is closed")
        return bool(select.select([], [fd], [], timeout)[1])

    def _write_chunk(self, chunk: bytes) -> int:
        fd = self._fd
        if fd is None:
            raise PtyError("the terminal is closed")
        return os.write(fd, chunk)

    def resize(self, cols: int, rows: int) -> None:
        """Set the PTY window size (``TIOCSWINSZ``) so the shell + TUIs reflow."""
        self.cols, self.rows = max(1, int(cols)), max(1, int(rows))
        if self._fd is None:
            return
        winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        try:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def foreground_pgid(self) -> int | None:
        """The terminal's foreground process group — the shell's own pid while it sits at
        a prompt, a job's group while a program runs. None when unknown."""
        if self._fd is None:
            return None
        try:
            return os.tcgetpgrp(self._fd)
        except OSError:
            return None

    def poll(self) -> int | None:
        """The child's exit code if it has exited (reaped non-blocking), else None."""
        if self.pid is None:
            return self._exit_code
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.pid = None
            return self._exit_code
        if pid == 0:
            return None  # still running
        self.pid = None
        self._exit_code = os.waitstatus_to_exitcode(status)
        return self._exit_code

    async def aclose(self) -> int | None:
        """End the shell like closing a terminal window: SIGHUP + SIGTERM to its process
        group, SIGKILL after a grace, then close the master fd. Returns the exit code.
        Idempotent, and NEVER blocks indefinitely.

        While waiting it keeps DRAINING the master. On macOS a process that exits with
        unread tty output blocks inside exit() until the master side reads it — so with
        the reader already gone, a blocking ``waitpid`` deadlocks forever (a busy shell,
        a build spewing output, a login shell mid-profile). Draining lets it finish."""
        if self.pid is not None:
            # Interactive shells ignore SIGTERM; SIGHUP is what a closing terminal sends,
            # and it reaches the jobs in the group too.
            self._signal_group(signal.SIGHUP)
            self._signal_group(signal.SIGTERM)
            if not await self._wait_exit(1.0):
                self._signal_group(signal.SIGKILL)
                await self._wait_exit(2.0)
        if self._writer is not None:
            # The child is gone, so a write in flight fails (EIO) at once; wait briefly for
            # the input thread to let go BEFORE the fd number can be closed and reused.
            await asyncio.to_thread(self._writer.close, 1.0)
        if self._reads is not None:
            self._reads.shutdown(wait=False, cancel_futures=True)
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self.pid is not None:
            await self._wait_exit(0.5)  # one more chance to reap now the master is gone
        return self._exit_code

    async def _wait_exit(self, timeout: float) -> bool:
        """Drain + poll until the child is reaped or ``timeout`` passes."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self._drain()
            if self.poll() is not None or self.pid is None:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)

    def _drain(self) -> None:
        """Read and discard whatever output is pending on the master, without blocking."""
        if self._fd is None:
            return
        try:
            os.set_blocking(self._fd, False)
            for _ in range(64):
                if not os.read(self._fd, 65536):
                    return
        except OSError:  # EAGAIN (drained), EIO (slave gone), EBADF — all mean "done"
            return

    def _signal_group(self, sig: int) -> None:
        """Signal the shell's process group — but ONLY when the child really leads its
        own group. Right after fork there is a window before the child's setsid() where
        it is still in OUR group; ``killpg(getpgid(child))`` then would signal the
        server's own process group (the server, and whatever launched it). In that
        window, signal just the child."""
        pid = self.pid
        if pid is None:
            return
        try:
            if os.getpgid(pid) == pid and pid != os.getpgrp():
                os.killpg(pid, sig)
                return
        except OSError:
            pass
        try:
            os.kill(pid, sig)
        except OSError:
            pass


class WinPtySession:
    """Windows backend via the optional ``pywinpty`` package, validated by the CI windows
    job. Same interface as ``PtySession``, but on top of ``winpty.PtyProcess``
    (method-based read/write, not an fd). No ``foreground_pgid`` (no POSIX job control),
    and ``login`` is accepted but meaningless here."""

    def __init__(
        self,
        *,
        shell: str = "",
        cwd: str = "",
        cols: int = 80,
        rows: int = 24,
        env_overrides: dict[str, str] | None = None,
        scrub_env: list[str] | None = None,
        login: bool = False,  # POSIX-only concept; accepted for a uniform interface
    ):
        self.shell = shell or os.environ.get("COMSPEC") or "cmd.exe"
        self.cwd, self.cwd_notice = resolve_cwd(cwd)
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self._env_overrides = env_overrides or {}
        self._scrub_env = set(scrub_env or [])
        self.pid: int | None = None
        self._proc = None
        self._exit_code: int | None = None
        self._writer: InputWriter | None = None
        self._reads: concurrent.futures.ThreadPoolExecutor | None = None

    def _build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in self._scrub_env}
        env.update(_TERM_ENV)
        env.update(self._env_overrides)
        return env

    def _write_chunk(self, chunk: bytes) -> int:
        proc = self._proc
        if proc is None:
            raise PtyError("the terminal is closed")
        # Decode leniently per chunk: a UTF-8 sequence split at a chunk edge degrades to
        # U+FFFD rather than failing; WRITE_CHUNK is far above a typed line.
        proc.write(chunk.decode("utf-8", "replace"))
        return len(chunk)

    def start(self) -> None:
        try:
            from winpty import PtyProcess  # optional dep (requires_pip on Windows)
        except ImportError as exc:
            raise PtyError("pywinpty not installed — `pip install pywinpty` (Windows)") from exc
        self._proc = PtyProcess.spawn(
            self.shell, cwd=self.cwd, env=self._build_env(), dimensions=(self.rows, self.cols)
        )
        self.pid = getattr(self._proc, "pid", None)
        self._writer = InputWriter(self._write_chunk, name=f"terminal-input-{self.pid}")
        self._reads = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"terminal-read-{self.pid}")

    async def read(self, n: int = 65536) -> bytes:
        if self._proc is None or self._reads is None:
            return b""
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(self._reads, self._proc.read, n)
        except EOFError:
            return b""
        except Exception:  # noqa: BLE001 — treat any read failure as EOF
            return b""
        if not data:
            return b""
        return data.encode("utf-8", "replace") if isinstance(data, str) else data

    def write(self, data: str | bytes) -> None:
        if self._proc is None or self._writer is None:
            return
        self._writer.submit(_as_bytes(data))

    async def send(self, data: str | bytes, timeout: float = 10.0) -> int:
        if self._proc is None or self._writer is None:
            raise PtyError("the terminal is closed")
        return await send_input(self._writer, _as_bytes(data), timeout)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(1, int(cols)), max(1, int(rows))
        if self._proc is None:
            return
        try:
            self._proc.setwinsize(self.rows, self.cols)
        except Exception:  # noqa: BLE001
            pass

    def poll(self) -> int | None:
        if self._proc is None:
            return self._exit_code
        try:
            if self._proc.isalive():
                return None
            self._exit_code = self._proc.exitstatus
        except Exception:  # noqa: BLE001
            pass
        return self._exit_code

    async def aclose(self) -> int | None:
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
            if self._writer is not None:
                await asyncio.to_thread(self._writer.close, 1.0)
            if self._reads is not None:
                self._reads.shutdown(wait=False, cancel_futures=True)
            self._proc = None
        return self._exit_code


def _as_bytes(data: str | bytes) -> bytes:
    return data.encode("utf-8", "replace") if isinstance(data, str) else bytes(data)


def open_session(**kw):
    """Construct the right PTY session for the platform: ``WinPtySession`` on Windows
    (pywinpty), else the POSIX ``PtySession``."""
    return WinPtySession(**kw) if sys.platform == "win32" else PtySession(**kw)
