"""A PTY-backed shell session — stdlib only (``pty``/``os``/``fcntl``/``termios``),
Unix-first (Linux/macOS).

No protoAgent host imports and no pip deps, so the suite spawns real PTYs in CI. The
session owns a child shell behind a pseudo-terminal: read its output off the master
fd (in a thread, so the event loop never blocks), write keystrokes to it, resize it
(``TIOCSWINSZ``), and reap the process group on close.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import termios

# Default TERM env so colour + 256-colour CLIs behave inside the terminal.
_TERM_ENV = {
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "TERM_PROGRAM": "protoagent-terminal",
}


def default_shell() -> str:
    """The shell to spawn when none is configured: ``$SHELL`` then ``/bin/bash``."""
    return os.environ.get("SHELL") or "/bin/bash"


class PtyError(Exception):
    """A PTY lifecycle failure (start / resize)."""


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
    ):
        self.shell = shell or default_shell()
        self.cwd = cwd or os.getcwd()
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self._env_overrides = env_overrides or {}
        self._scrub_env = set(scrub_env or [])
        self.pid: int | None = None
        self._fd: int | None = None
        self._exit_code: int | None = None

    # ── env ─────────────────────────────────────────────────────────────────────
    def _build_env(self) -> dict[str, str]:
        """The child's env: the server's env + TERM defaults + overrides, minus the
        scrubbed keys (so the operator's own secrets don't leak into the shell)."""
        env = {k: v for k, v in os.environ.items() if k not in self._scrub_env}
        env.update(_TERM_ENV)
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
                os.execvpe(self.shell, [self.shell], env)
            except OSError:
                os._exit(127)
        self.pid = pid
        self._fd = fd
        self.resize(self.cols, self.rows)

    async def read(self, n: int = 65536) -> bytes:
        """A chunk of shell output; ``b""`` on EOF (the child exited). The blocking
        read runs in a thread so the event loop keeps serving other sessions."""
        if self._fd is None:
            return b""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._blocking_read, n)

    def _blocking_read(self, n: int) -> bytes:
        try:
            return os.read(self._fd, n)
        except OSError as exc:
            # On Linux the PTY master read raises EIO (not EOF) once the child exits.
            if exc.errno == errno.EIO:
                return b""
            raise

    def write(self, data: str | bytes) -> None:
        """Send keystrokes/paste to the shell."""
        if self._fd is None:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        try:
            os.write(self._fd, data)
        except OSError:
            pass  # the shell may have just exited; the read loop will see EOF

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
        """Terminate the shell's process group (SIGTERM, then SIGKILL after a grace),
        close the master fd, and reap. Returns the exit code. Idempotent."""
        pid = self.pid
        if pid is not None:
            self._signal_group(signal.SIGTERM)
            for _ in range(20):  # ~1s grace
                if self.poll() is not None:
                    break
                await asyncio.sleep(0.05)
            if self.poll() is None:
                self._signal_group(signal.SIGKILL)
                try:
                    await asyncio.get_running_loop().run_in_executor(None, self._reap, pid)
                except Exception:  # noqa: BLE001
                    pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        return self._exit_code

    def _signal_group(self, sig: int) -> None:
        if self.pid is None:
            return
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except OSError:
            try:
                os.kill(self.pid, sig)
            except OSError:
                pass

    def _reap(self, pid: int) -> None:
        try:
            _, status = os.waitpid(pid, 0)
            self._exit_code = os.waitstatus_to_exitcode(status)
            self.pid = None
        except OSError:
            self.pid = None
