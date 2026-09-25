"""Best-effort facts about a live process: its command name and working directory.
Used to describe terminals to the agent ("vim, in ~/dev/app"). Never raises; unknown
comes back as "". POSIX only (Linux /proc, macOS ps/lsof)."""

from __future__ import annotations

import os
import subprocess
import sys


def _run(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def command_name(pid: int | None) -> str:
    """The short command name ("vim", "npm", "zsh") of ``pid``."""
    if not pid or sys.platform == "win32":
        return ""
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return os.path.basename(_run(["ps", "-o", "comm=", "-p", str(pid)]).strip()).lstrip("-")


def cwd_of(pid: int | None) -> str:
    """The current working directory of ``pid`` (follows the shell's ``cd``)."""
    if not pid or sys.platform == "win32":
        return ""
    if os.path.isdir("/proc"):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return ""
    for line in _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""
