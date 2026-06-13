# Terminal — a full terminal in the protoAgent console

A **protoAgent plugin** that drops a real terminal into the console: an
[xterm.js](https://xtermjs.org) view wired to a live **PTY shell** over a WebSocket.
It's **themed from the protoAgent design system** — the terminal reads the console's
`--pl-*` tokens and re-themes live, so it always matches your console.

Install into any protoAgent agent from this git URL — it's not tied to one agent.

## What it does

- A left-rail **Terminal** view (ADR 0026) — an xterm.js page (fit + clickable-links
  addons) served by the plugin, connected to a shell over a WebSocket.
- A **real PTY** on the backend — stdlib `pty` (no pip deps), so it's a genuine
  interactive shell: TUIs, colour, resize, `Ctrl-C`, the works. The wire protocol
  mirrors protoMaker's terminal (`data`/`exit`/`connected` ⇄ `input`/`resize`/`ping`).
- **protoAgent theming** — the xterm theme (background/foreground/cursor/selection +
  the 16 ANSI colours) is built from the console's `--pl-*` tokens on the DS-kit
  handshake and re-applied on every live re-theme.

## Security — read this before enabling

A terminal is **interactive shell access on the host**. This plugin:

- **Ships DISABLED.** Enabling it is a deliberate trust decision (install ≠ enable).
- **Gates the WebSocket on the operator bearer.** The page gets the bearer from the
  DS-kit handshake and opens `…/ws?token=<bearer>`; the server verifies it against the
  host's configured token (`auth.token` / `A2A_AUTH_TOKEN`) — the same token the
  console uses — so only the authenticated operator gets a shell. (A browser WebSocket
  can't send an Authorization header, so the token rides as a query param and is
  checked in the handler.)
- **When the host has no bearer set** (loopback dev), the WebSocket is open on the
  bound interface and logs a warning. **Only enable this on a token-gated or
  loopback-only deployment.**
- Scrubs the operator/agent secrets (`<AGENT>_API_KEY`, `A2A_AUTH_TOKEN`, …) from the
  child shell's environment.

## Requirements

- **protoAgent ≥ 0.27.0** (console views + WebSocket-through-the-fleet-proxy, #883).
- A **Unix PTY** (Linux/macOS). Windows is not supported (no `pywinpty` yet).
- No pip deps (the PTY is stdlib). xterm.js loads from jsDelivr — needs network at
  view-load time (vendoring is a planned follow-up for airgapped installs).

## Install

```bash
python -m server plugin install https://github.com/protoLabsAI/terminal-plugin --ref main
python -m server plugin enable terminal          # the trust decision; then restart
```

Then in `config/langgraph-config.yaml`:

```yaml
plugins:
  enabled: [terminal]

terminal:
  shell: ""        # blank → $SHELL, then /bin/bash
  cwd: ""          # blank → the server's cwd
  scrollback: 5000
  font_size: 13
```

Open the **Terminal** rail icon. (Make sure the host has an operator bearer set —
`auth.token` or `A2A_AUTH_TOKEN` — before exposing the port.)

## Layout

| File | What |
|---|---|
| `pty_session.py` | the stdlib-`pty` shell session (spawn / read / write / resize / reap) |
| `api.py` | the router: the public `/view` page + the bearer-gated `/ws` PTY bridge |
| `view.py` | the xterm.js page — four rules + the `--pl-*` → xterm theme mapping |
| `__init__.py` | `register()` — mounts the one router |

## Roadmap

v1 is a solid single terminal session per view. Possible next steps: multi-session
tabs, split panes, a search overlay, vendored xterm assets (offline), Windows
(`pywinpty`). PRs welcome.

Ships **disabled**; nothing runs until you enable it.
