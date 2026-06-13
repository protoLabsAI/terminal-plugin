# Terminal — a full terminal in the protoAgent console

A **protoAgent plugin** that drops a real terminal into the console: an
[xterm.js](https://xtermjs.org) view wired to a live **PTY shell** over a WebSocket.
It's **themed from the protoAgent design system** — the terminal reads the console's
`--pl-*` tokens and re-themes live, so it always matches your console.

Install into any protoAgent agent from this git URL — it's not tied to one agent.

## What it does

- A left-rail **Terminal** view (ADR 0026) — an xterm.js page (fit + clickable-links
  addons) served by the plugin, connected to a shell over a WebSocket.
- **Tabs** — run several sessions in one view (+ to add, × to close, double-click to
  rename). Each tab owns its own xterm + WebSocket, and each WebSocket gets its own
  PTY shell — closing a tab kills only that shell.
- A **real PTY** on the backend — stdlib `pty` (no pip deps), so it's a genuine
  interactive shell: TUIs, colour, resize, `Ctrl-C`, the works. The wire protocol
  mirrors protoMaker's terminal (`data`/`exit`/`connected` ⇄ `input`/`resize`/`ping`).
- **protoAgent theming** — the xterm theme (background/foreground/cursor/selection +
  the 16 ANSI colours) is built from the console's `--pl-*` tokens on the DS-kit
  handshake and re-applied on every live re-theme.
- **Crisp block art** — the canvas renderer with `customGlyphs` draws block/box-drawing
  glyphs as exact cell-filling shapes, so contiguous block art (e.g. the Claude Code
  splash) renders **flush, no seams** (the default DOM renderer draws them from the
  font, which leaves gaps).

## Security — read this before enabling

A terminal is **interactive shell access on the host**. This plugin:

- **Enabled by default** — once installed it's on. That's safe because the WebSocket
  is bearer-gated (below) and protoAgent only binds a non-loopback interface when a
  token is set, so an un-gated shell is always loopback-local. Disable it explicitly
  (`plugins.disabled: [terminal]`) if you don't want a terminal.
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
- **Linux/macOS** — stdlib PTY, no pip deps. **Windows is EXPERIMENTAL** (untested in
  CI): it uses `pywinpty` — `python -m server plugin install-deps terminal` on Windows,
  then validate. The POSIX path is the supported, tested one.
- xterm.js + addons are **vendored** (`vendor/`) and served locally by the plugin —
  **works offline / airgapped**, no CDN.

## Install — no restart needed

Easiest: the console **Plugins** panel — paste the git URL, install. It's enabled by
default, its router **hot-mounts** (#822), and the **Terminal** rail icon appears from
runtime-status without a console rebuild (#853). No restart.

Or from the CLI against a running server:

```bash
python -m server plugin install https://github.com/protoLabsAI/terminal-plugin --ref v0.1.1
# then pick it up live: hit "Sync" in the console Plugins panel, or have the agent call
# reload_plugins (plugin-devkit). It hot-mounts — no restart.
```

Optional config in `config/langgraph-config.yaml` (all have sane defaults):

```yaml
# enabled by default; to turn it OFF: plugins: { disabled: [terminal] }
terminal:
  shell: ""        # blank → $SHELL, then /bin/bash
  cwd: ""          # blank → the server's cwd
  scrollback: 5000
  font_size: 13
```

Then open the **Terminal** rail icon. (Make sure the host has an operator bearer set —
`auth.token` or `A2A_AUTH_TOKEN` — before binding a non-loopback interface.)

## Layout

| File | What |
|---|---|
| `pty_session.py` | the PTY shell session: POSIX (stdlib `pty`) + Windows (`pywinpty`, experimental) behind `open_session()` |
| `api.py` | the router: the public `/view` page, vendored `/static/*` assets, the bearer-gated `/ws` PTY bridge |
| `view.py` | the xterm.js page — four rules + the `--pl-*` → xterm theme mapping |
| `vendor/` | the vendored xterm.js + addons + css (served offline) |
| `__init__.py` | `register()` — mounts the one router |

## Roadmap

Multi-session tabs, a real PTY, themed + offline. Possible next steps: split panes, a
search overlay, and validating the experimental Windows backend. PRs welcome.

Enabled by default once installed (the WS bearer gate is the protection) — disable
with `plugins.disabled: [terminal]`.
