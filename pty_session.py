"""A PTY-backed shell session.

Two backends behind one interface (start / read / write / resize / poll / aclose):
- ``PtySession`` — POSIX (Linux/macOS), stdlib only (``pty``/``os``/``fcntl``/
  ``termios``); no pip deps, so the suite spawns real PTYs in CI.
- ``WinPtySession`` — Windows, via the optional ``pywinpty`` package (``requires_pip`` in
  the manifest); validated by the CI windows job (tests/test_winpty.py, a real cmd.exe).

``open_session(...)`` picks the right backend for the platform. The POSIX session owns
a child shell behind a pseudo-terminal: read its output off the master fd (in a thread,
so the loop never blocks), write keystrokes, resize (``TIOCSWINSZ``), reap the group.
"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import struct
import sys

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
            # On Linux the PTY master read raises EIO (not EOF) once the child exits; EAGAIN/
            # EBADF mean aclose() has taken the fd over (drain → close) — the session is over.
            if exc.errno in (errno.EIO, errno.EAGAIN, errno.EBADF):
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

    def _build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in self._scrub_env}
        env.update(_TERM_ENV)
        env.update(self._env_overrides)
        return env

    def start(self) -> None:
        try:
            from winpty import PtyProcess  # optional dep (requires_pip on Windows)
        except ImportError as exc:
            raise PtyError("pywinpty not installed — `pip install pywinpty` (Windows)") from exc
        self._proc = PtyProcess.spawn(
            self.shell, cwd=self.cwd, env=self._build_env(), dimensions=(self.rows, self.cols)
        )
        self.pid = getattr(self._proc, "pid", None)

    async def read(self, n: int = 65536) -> bytes:
        if self._proc is None:
            return b""
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, self._proc.read, n)
        except EOFError:
            return b""
        except Exception:  # noqa: BLE001 — treat any read failure as EOF
            return b""
        if not data:
            return b""
        return data.encode("utf-8", "replace") if isinstance(data, str) else data

    def write(self, data: str | bytes) -> None:
        if self._proc is None:
            return
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        try:
            self._proc.write(data)
        except Exception:  # noqa: BLE001
            pass

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
            self._proc = None
        return self._exit_code


def open_session(**kw):
    """Construct the right PTY session for the platform: ``WinPtySession`` on Windows
    (pywinpty), else the POSIX ``PtySession``."""
    return WinPtySession(**kw) if sys.platform == "win32" else PtySession(**kw)
