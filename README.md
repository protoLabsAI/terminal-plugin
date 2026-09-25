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
- A **real PTY** on the backend — stdlib `pty` (no pip deps), so it's a genuine
  interactive shell: TUIs, colour, resize, `Ctrl-C`, the works. The wire protocol
  mirrors protoMaker's terminal (`data`/`exit`/`connected` ⇄ `input`/`resize`/`ping`).
- **protoAgent theming** — the xterm theme (background/foreground/cursor/selection +
  the 16 ANSI colours) is built from the console's `--pl-*` tokens on the DS-kit
  handshake and re-applied on every live re-theme.
- **Fast, crisp rendering** — the WebGL renderer (canvas when WebGL is unavailable or
  loses its context) with `customGlyphs` draws block/box-drawing glyphs as exact
  cell-filling shapes, so block art (e.g. the Claude Code splash) renders flush.
  Emoji and CJK get correct widths (Unicode 11 tables).
- **Behaves like your usual terminal** — shells start as **login shells**, so
  `~/.zprofile` / `~/.bash_profile` run and PATH (Homebrew, pyenv, nvm…) matches
  Terminal/iTerm even when protoAgent was launched from the desktop app. A UTF-8
  locale is supplied when the server has none. Full-screen apps (vim, htop, Claude Code)
  repaint when you reattach. A flood of output (`yes`, a huge `cat`) is flow-controlled
  instead of buffering without limit.
- **Console-native** — find in scrollback (⌘F), the console's own right-click menu
  (copy, paste, select all, find, clear, tabs), and console shortcuts like the ⌘⇧K
  palette keep working while the terminal has focus. Tabs follow the running program's
  title (shown as the cwd's tail) until you rename them.

## Keyboard

| | macOS | Linux / Windows |
|---|---|---|
| Copy / paste | ⌘C / ⌘V | Ctrl+Shift+C / Ctrl+Shift+V |
| Select all | ⌘A | Ctrl+Shift+A |
| Find (Enter / Shift+Enter: next / previous) | ⌘F, then ⌘G / ⌘⇧G | Ctrl+Shift+F, then Ctrl+Shift+G |
| Clear | ⌘K | right-click ▸ Clear (Ctrl+L in the shell) |
| New tab / close tab | ⌘T / ⌘W | Ctrl+Shift+T / Ctrl+Shift+W |
| Next / previous tab | Ctrl+Tab / Ctrl+Shift+Tab, ⌘⇧] / ⌘⇧[ | Ctrl+Tab / Ctrl+Shift+Tab, Ctrl+PgDn / Ctrl+PgUp |
| Go to tab 1–9 | ⌘1 … ⌘9 (⌘9 = last) | Ctrl+1 … Ctrl+9 |
| Font size bigger / smaller / reset | ⌘= / ⌘- / ⌘0 | Ctrl+= / Ctrl+- / Ctrl+0 |

Everything else goes to the shell, except console shortcuts: on macOS any other ⌘ chord
(⌘⇧K palette, ⌘, Settings…); elsewhere Ctrl+Shift chords and Ctrl+, — plain Ctrl+<key>
(Ctrl-C, Ctrl-R, Ctrl-D…) always reaches the shell. (In a regular browser tab, the
browser keeps ⌘T/⌘W/⌘N for itself; the desktop app passes them through.) Middle-click a
tab to close it; double-click to rename it.

## Security — read this before enabling

A terminal is **interactive shell access on the host**. This plugin:

- **Enabled by default** — once installed it's on. That's safe because the WebSocket
  is bearer-gated (below) and protoAgent only binds a non-loopback interface when a
  token is set, so an un-gated shell is always loopback-local. Disable it explicitly
  (`plugins.disabled: [terminal]`) if you don't want a terminal.
- **Gates the WebSocket on the operator bearer.** The page gets the bearer from the
  DS-kit handshake and sends it as the socket's **first frame** (`{type:"auth", token}`);
  the server verifies it against the host's configured token (`auth.token` /
  `A2A_AUTH_TOKEN`) — the same token the console uses — so only the authenticated
  operator gets a shell. A socket that doesn't authenticate within 10s is closed. (A
  browser WebSocket can't send an Authorization header; the token is deliberately kept
  out of the URL, where it would land in access logs and history.)
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
python -m server plugin install https://github.com/protoLabsAI/terminal-plugin --ref v0.6.0
# then pick it up live: hit "Sync" in the console Plugins panel, or have the agent call
# reload_plugins (plugin-devkit). It hot-mounts — no restart.
```

Optional config in `config/langgraph-config.yaml` (all have sane defaults):

```yaml
# enabled by default; to turn it OFF: plugins: { disabled: [terminal] }
terminal:
  shell: ""                 # blank → $SHELL, then /bin/bash
  cwd: ""                   # blank → the server's cwd
  scrollback: 5000
  font_size: 13
  keep_alive_minutes: 30    # detached shells live this long; 0 = end on disconnect
  max_sessions: 12
  login_shell: true         # read ~/.zprofile etc, like Terminal/iTerm
  font_family: ""           # blank → the console's mono font
  cursor_style: block       # block | bar | underline
  option_as_meta: false     # macOS: Option sends Meta (Option+B/F word jumps)
  copy_on_select: false
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
| `view.py` | the page shell — markup, styles, and the bootstrap that loads the kit, xterm and the app |
| `web/terminal.js` | the view app — tabs, sessions/reconnect, keys, find, context menu, theme |
| `web/logic.js` | pure view logic (key map, token → ANSI palette, labels) — `node --test tests/js/*.test.mjs` |
| `vendor/` | the vendored xterm.js 5.5 + addons + css (served offline; versions in `vendor/VERSIONS.md`) |
| `__init__.py` | `register()` — mounts the router (on live config) + a shutdown hook that ends every shell |

## Roadmap

Next: agent integration (the agent can open a terminal, run a command in one, and read
what's on screen), split panes, and validating the experimental Windows backend.
PRs welcome.

Enabled by default once installed (the WS bearer gate is the protection) — disable
with `plugins.disabled: [terminal]`.
