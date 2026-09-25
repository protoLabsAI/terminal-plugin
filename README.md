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
  rename inline). Each tab is attached to its own PTY shell; closing a tab ends only
  that shell.
- **Shells that survive** — a shell outlives the connection that opened it. Switching
  to another console view keeps the terminal mounted in the background; a reload, a
  network blip or a server-side reconnect reattaches each tab to its shell and
  replays the output you missed. A shell with nothing attached keeps running for
  `keep_alive_minutes` (default 30), then is ended. Opening the same terminal in a
  second window takes it over (press any key in the first to take it back). When a
  shell exits, press any key for a new one.
- **Settings** — shell, starting directory, font size, scrollback, keep-alive and the
  shell limit are editable in **Settings ▸ Plugins ▸ Terminal**, and apply without a
  restart (font/scrollback on the next view load, shell/cwd to the next new shell).
  A new shell starts in your **home folder** unless you set a starting directory
  (`~/code` and `$VARS` expand; a folder that does not exist falls back to home, with
  a one-line notice in the terminal).
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
- **Gates the WebSocket on the operator bearer, via a single-use ticket.** Before each
  connect the page calls `POST /api/plugins/terminal/ticket` — an ordinary HTTP route on
  the host's bearer-gated `/api/plugins/*` prefix — and sends the ticket it gets back as
  the socket's **first frame** (`{type:"auth", ticket}`). Tickets are random, live ~30s,
  and are burned on first use. This is what makes the terminal work on a **fleet
  member** reached through the hub: the hub authenticates the ticket request and
  forwards it with the fleet service token the member expects, whereas the operator's
  own bearer never matches a member's. A direct connection may still send
  `{type:"auth", token}`, verified against the host's configured token (`auth.token` /
  `A2A_AUTH_TOKEN`). A socket that doesn't authenticate within 10s is closed. (A
  browser WebSocket can't send an Authorization header; credentials are deliberately
  kept out of the URL, where they would land in access logs and history.)
- **Exempts only the static xterm assets** (`public_paths: /plugins/terminal/static/`)
  so they load on a token-gated deployment; they're vendored, non-secret files.
- **Caps live shells** (`max_sessions`, default 12) and ends detached shells after
  `keep_alive_minutes`; every shell is ended when the server shuts down.
- **When the host has no bearer set** (loopback dev), the WebSocket is open on the
  bound interface and logs a warning. **Only enable this on a token-gated or
  loopback-only deployment.**
- Scrubs the operator/agent secrets (`<AGENT>_API_KEY`, `A2A_AUTH_TOKEN`, …) from the
  child shell's environment.

## Requirements

- **protoAgent ≥ 0.82.0** (console views, WebSocket-through-the-fleet-proxy #883, live
  config, and background-mounted views #1640).
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
python -m server plugin install https://github.com/protoLabsAI/terminal-plugin --ref v0.5.2
# then pick it up live: hit "Sync" in the console Plugins panel, or have the agent call
# reload_plugins (plugin-devkit). It hot-mounts — no restart.
```

Optional config in `config/langgraph-config.yaml` (all have sane defaults):

```yaml
# enabled by default; to turn it OFF: plugins: { disabled: [terminal] }
terminal:
  shell: ""                 # blank → $SHELL, then /bin/bash
  cwd: ""                   # blank → your home folder; ~ and $VARS expand
  scrollback: 5000
  font_size: 13
  keep_alive_minutes: 30    # detached shells live this long; 0 = end on disconnect
  max_sessions: 12
```

(Or edit them in **Settings ▸ Plugins ▸ Terminal**.)

Then open the **Terminal** rail icon. (Make sure the host has an operator bearer set —
`auth.token` or `A2A_AUTH_TOKEN` — before binding a non-loopback interface.)

## Layout

| File | What |
|---|---|
| `pty_session.py` | the PTY shell session: POSIX (stdlib `pty`) + Windows (`pywinpty`, experimental) behind `open_session()` |
| `sessions.py` | the session manager: shells that outlive their socket — replay buffer, attach/takeover, keep-alive reaper |
| `api.py` | the router: the public `/view` page (config baked in), vendored `/static/*` assets, the bearer-gated `/ws` attach bridge |
| `view.py` | the xterm.js page — four rules + the `--pl-*` → xterm theme mapping |
| `vendor/` | the vendored xterm.js + addons + css (served offline) |
| `__init__.py` | `register()` — mounts the router (on live config) + a shutdown hook that ends every shell |

## Roadmap

Multi-session tabs on persistent shells, a real PTY, themed + offline, configurable from
Settings. Next: agent integration (let the agent open a terminal / run a command in one),
console keybindings + a context menu + search, split panes, a fully token-derived ANSI
palette, and validating the experimental Windows backend. PRs welcome.

Enabled by default once installed (the WS bearer gate is the protection) — disable
with `plugins.disabled: [terminal]`.
