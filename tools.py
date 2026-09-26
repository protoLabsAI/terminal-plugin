"""Agent tools — the agent works WITH the operator's terminal, in plain sight.

The agent already has a hidden shell (core ``run_command``). These tools are the visible
counterpart: what the agent runs appears in a terminal tab the operator can watch, scroll
and take over, and the agent can read what the operator is looking at ("what's this error
in my terminal?"). Gated by the ``agent_access`` setting, read LIVE on every call:

- ``off``  — every tool refuses.
- ``read`` — ``terminal_list`` / ``terminal_read`` only.
- ``run``  — also ``terminal_run`` / ``terminal_open`` (the default).

Guardrails:
- ``terminal_run`` types into the agent's OWN tab ("Agent") unless told otherwise, so it
  never lands in the middle of something the operator is typing.
- It refuses any tab where a program holds the foreground (vim, a build, a REPL): typing
  a command there would feed keystrokes into that program.
- Running never switches the operator's console view; ``terminal_open`` (an explicit
  "show me a terminal") does.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time

from .procinfo import command_name, cwd_of
from .sessions import MANAGER, SessionLimitError
from .textutil import clean, tail_lines

MAX_OUTPUT_CHARS = 12_000  # what one tool result may carry back into the context
AGENT_TAB = "Agent"

ACCESS_LEVELS = ("off", "read", "run")


def _access(conf) -> str:
    level = str(conf().get("agent_access", "run") or "run").lower()
    return level if level in ACCESS_LEVELS else "run"


def _refuse(level_needed: str, level: str) -> str:
    return (
        f"The terminal is set to agent access '{level}' — this needs '{level_needed}'. "
        "The operator can change it in Settings ▸ Plugins ▸ Terminal (Agent access)."
    )


def _resolve(target: str):
    """A session by selector: "active" (the tab the operator last looked at), "agent"
    (the agent's own tab) or an id. None when there's no such live session."""
    t = (target or "active").strip()
    if t == "active":
        return MANAGER.active() or (MANAGER.list()[-1] if MANAGER.list() else None)
    if t == "agent":
        return MANAGER.agent_session()
    return MANAGER.get(t)


async def _describe(sess) -> dict:
    fg = sess.busy()
    shell_pid = sess.pty.pid
    running, cwd = await asyncio.gather(
        asyncio.to_thread(command_name, fg) if fg else asyncio.sleep(0, ""),
        asyncio.to_thread(cwd_of, fg or shell_pid),
    )
    return {
        "id": sess.id,
        "name": sess.name or ("Agent" if sess.origin == "agent" else ""),
        "origin": sess.origin,
        "cwd": cwd or sess.cwd,
        "running": running or "",
        "full_screen": sess.alt_screen,
        "attached": sess.attached,
    }


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"[… {len(text) - limit} earlier chars omitted …]\n" + text[-limit:]


def build_tools(registry, conf):
    """The four tools, bound to this registry (navigate/emit) and live config."""
    from langchain_core.tools import tool

    from .api import scrub_keys

    def _open(cwd: str, *, name: str, origin: str, focus: bool):
        cfg = conf()
        sess = MANAGER.create(
            shell=cfg.get("shell") or "",
            cwd=os.path.expanduser(cwd) if cwd else (cfg.get("cwd") or ""),
            login=bool(cfg.get("login_shell", True)),
            scrub_env=scrub_keys(),
            max_sessions=int(cfg.get("max_sessions") or 0),
            name=name,
            origin=origin,
            pending_adopt=True,
        )
        # A live view adds the tab now (bus → page); a view loaded later adopts it from
        # GET /api/plugins/terminal/sessions.
        try:
            registry.emit("session_opened", {"session": sess.id, "name": name, "focus": focus})
        except Exception:  # noqa: BLE001 — the tab still appears on the next view load
            pass
        return sess

    @tool
    async def terminal_list() -> str:
        """List the operator's open terminal tabs: id, name, working directory, what's
        running in each, and which one they're looking at. Use before terminal_read /
        terminal_run when you need a specific tab."""
        level = _access(conf)
        if level == "off":
            return _refuse("read", level)
        sessions = MANAGER.list()
        if not sessions:
            return "No terminals are open. terminal_open starts one; terminal_run opens the Agent tab."
        active = MANAGER.active()
        rows = []
        for s in sessions:
            d = await _describe(s)
            flags = [f for f, on in (("active", s is active), ("agent's tab", s.origin == "agent")) if on]
            what = d["running"] or "shell prompt"
            if d["full_screen"]:
                what += " (full-screen)"
            label = d["name"] or "terminal"
            rows.append(f"- {d['id']} · {label} · {d['cwd']} · {what}" + (f" [{', '.join(flags)}]" if flags else ""))
        return "Open terminals:\n" + "\n".join(rows)

    @tool
    async def terminal_read(session: str = "active", lines: int = 80) -> str:
        """Read what's in a terminal — its recent output as plain text (colours and cursor
        codes stripped). ``session`` is "active" (the tab the operator is looking at —
        right for "what's this error?"), "agent" (your own tab), or an id from
        terminal_list. ``lines``: how many trailing lines (max 400)."""
        level = _access(conf)
        if level == "off":
            return _refuse("read", level)
        sess = _resolve(session)
        if sess is None:
            return f"No open terminal matches {session!r}. terminal_list shows what's open."
        d = await _describe(sess)
        body = tail_lines(clean(sess.replay()), max(1, min(int(lines), 400)))
        head = f"Terminal {d['id']} · {d['cwd']} · {d['running'] or 'shell prompt'}"
        if d["full_screen"]:
            head += (
                "\n(A full-screen program is up — this is its raw output stream, which "
                "may not match the screen exactly.)"
            )
        return head + "\n\n" + (_clip(body) or "(no output yet)")

    @tool
    async def terminal_run(command: str, session: str = "agent", cwd: str = "", wait_seconds: float = 30) -> str:
        """Run a shell command VISIBLY in one of the operator's terminal tabs and return
        its output. Prefer this over a hidden shell when the operator should see or keep
        the result (a dev server, tests, a build, anything long-running or interactive).

        ``session``: "agent" (default — your own "Agent" tab, created if needed), "new"
        (a fresh tab), "active" (the tab the operator is looking at — only when they ask
        you to use it), or an id from terminal_list. ``cwd``: run it there (``cd`` first).
        ``wait_seconds``: how long to wait for it to finish (0-300). A command still
        running then (a server, a watcher) keeps running; you get the output so far.

        Refuses a tab where a program holds the foreground (vim, a build) — it would type
        into that program. Does not switch the operator's view."""
        level = _access(conf)
        if level != "run":
            return _refuse("run", level)
        command = (command or "").rstrip("\n")
        if not command.strip():
            return "Nothing to run — `command` is empty."
        target = (session or "agent").strip()
        try:
            if target == "new":
                sess = _open(cwd, name=AGENT_TAB, origin="agent", focus=False)
                cwd = ""  # it started there
            elif target == "agent":
                sess = MANAGER.agent_session()
                if sess is None:
                    sess = _open(cwd, name=AGENT_TAB, origin="agent", focus=False)
                    cwd = ""
            else:
                sess = _resolve(target)
                if sess is None:
                    return f"No open terminal matches {target!r}. terminal_list shows what's open."
        except SessionLimitError as exc:
            return f"Can't open another terminal: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't start a shell: {exc}"

        await _settle(sess)
        fg = sess.busy()
        if fg:
            name = await asyncio.to_thread(command_name, fg)
            return (
                f"Terminal {sess.id} is busy running `{name or 'a program'}` — typing there would go "
                "into that program. Wait for it (terminal_read to check), or use session='new'."
            )
        line = f"cd {shlex.quote(os.path.expanduser(cwd))} && {command}" if cwd else command
        start = sess.emitted
        sess.write(line + "\r")
        finished = await _wait_for_prompt(sess, max(0.0, min(float(wait_seconds), 300.0)))
        raw, complete = sess.text_since(start)
        out = clean(raw)
        # Drop the echoed command line itself (the first line of the output).
        first, _, rest = out.partition("\n")
        if line.strip()[:40] in first:
            out = rest
        out = tail_lines(out, 10_000) if out.strip() else ""
        status = "finished" if finished else f"still running after {wait_seconds:g}s — it keeps running in the tab"
        prefix = "" if complete else "[output began before the replay buffer's window]\n"
        return f"Ran in terminal {sess.id} ({status}):\n$ {line}\n" + prefix + (_clip(out) or "(no output)")

    @tool
    async def terminal_open(cwd: str = "", name: str = "") -> str:
        """Open a new terminal tab for the operator — optionally in ``cwd`` and with a tab
        ``name`` — and bring the Terminal view up. Use when the operator asks for a
        terminal ("open a terminal in the plugin repo")."""
        level = _access(conf)
        if level != "run":
            return _refuse("run", level)
        path = os.path.expanduser(cwd) if cwd else ""
        if path and not os.path.isdir(path):
            return f"No such directory: {cwd}"
        try:
            sess = _open(path, name=name.strip(), origin="operator", focus=True)
        except SessionLimitError as exc:
            return f"Can't open another terminal: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't start a shell: {exc}"
        try:
            registry.navigate("terminal")
        except Exception:  # noqa: BLE001
            pass
        return f"Opened terminal {sess.id}" + (f" in {path}" if path else "") + " — it's in the Terminal view."

    return [terminal_list, terminal_read, terminal_run, terminal_open]


async def _settle(sess, quiet: float = 0.4, limit: float = 3.0) -> None:
    """Let a just-opened shell finish starting (profile scripts, the first prompt) before
    typing into it, so the command isn't echoed into a half-drawn prompt."""
    deadline = time.monotonic() + limit
    last, still = sess.emitted, time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        if sess.emitted != last:
            last, still = sess.emitted, time.monotonic()
        elif sess.emitted and time.monotonic() - still >= quiet:
            return


async def _wait_for_prompt(sess, timeout: float) -> bool:
    """Wait until the command has finished: the shell holds the foreground again and the
    output has been quiet briefly. True when it finished within ``timeout``."""
    start = time.monotonic()
    deadline = start + timeout
    saw_busy = False
    last, still = sess.emitted, time.monotonic()
    while True:
        if sess.exited:
            return True
        now = time.monotonic()
        if sess.emitted != last:
            last, still = sess.emitted, now
        busy = sess.busy() is not None
        saw_busy = saw_busy or busy
        # Builtins (cd, echo) never leave the shell's group — for those, settle on quiet.
        if not busy and (saw_busy or now - start >= 1.0) and now - still >= 0.3:
            return True
        if now >= deadline:
            return False
        await asyncio.sleep(0.1)
