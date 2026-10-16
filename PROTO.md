# PROTO.md — coder grounding for the terminal plugin

Canonical instructions for coding agents working in this repo. Read this before
you touch anything. Everything here is drawn from `README.md`, `pyproject.toml`,
`requirements-dev.txt`, `.github/workflows/ci.yml`, `.github/workflows/release.yml`
and `tests/test_packaging.py` — not invented. When those files and this one
disagree, the source files win; fix this doc.

## 1. What this is

A **protoAgent console plugin** that drops a real terminal into the console: an
[xterm.js](https://xtermjs.org) **console view** wired to a live **PTY shell** over a
**WebSocket**. The backend is a genuine interactive PTY (TUIs, colour, resize,
`Ctrl-C`), the frontend is a vendored xterm.js page themed from the console's
`--pl-*` design tokens.

The agent can also work with the terminal through **four tools** (`tools.py`),
gated by the `agent_access` setting (`off | read | run`):

- `terminal_list` — the open tabs: cwd, what's running, which one the user is looking at.
- `terminal_read` — a tab's recent output as plain text ("what's this error?").
- `terminal_run` — runs a command **visibly** in its own **Agent** tab (created on
  demand, reused), waits for the prompt to return, hands back the output.
- `terminal_open` — opens a tab (optionally in a directory, with a name) and raises
  the Terminal view.

## 2. Layout

The authoritative file map is the **Layout table in `README.md`** — read it there.
Quick orientation:

- `pty_session.py` — the PTY session: POSIX (stdlib `pty`) + Windows (`pywinpty`) behind `open_session()`.
- `sessions.py` — the session manager: shells that outlive their socket (replay buffer, attach/takeover, keep-alive reaper).
- `api.py` — the routers: public `/view` page, `/static/*` assets, bearer-gated `/ws` bridge, gated sessions list.
- `tools.py` — the four agent tools, gated by `agent_access`.
- `textutil.py` / `procinfo.py` — raw output → plain text; a process's command name + cwd (POSIX-only).
- `view.py` — the page shell (markup, styles, bootstrap).
- `web/terminal.js` — the view app (tabs, reconnect, keys, find, context menu, theme).
- `web/logic.js` / `web/layout.js` — **pure view logic**, covered by node tests.
- `vendor/` — vendored xterm.js + addons + css (versions in `vendor/VERSIONS.md`).
- `__init__.py` — `register()`: mounts the router + a shutdown hook that ends every shell.

## 3. Build / test

The gate **is CI** (`.github/workflows/ci.yml`). Run these four, in order; they must
all pass:

```bash
ruff check .
ruff format --check .
pytest -q
node --test tests/js/*.test.mjs
```

Dev dependencies are in `requirements-dev.txt` plus `ruff`
(`pip install -r requirements-dev.txt ruff`). Node 22, Python 3.12 in CI.

- The suite is **host-free** (only `requirements-dev.txt` — the host normally provides
  fastapi + langchain-core) and **drives real PTYs**, so it exercises the actual shell
  bridge. Every test is bounded by a 60s pytest timeout (`pyproject.toml`) — nothing
  may hang.
- Pure view logic lives in `web/logic.js` / `web/layout.js` and is tested with
  `node --test tests/js/*.test.mjs`.
- The **Windows backend** (`pywinpty`) is tested **only in CI's `windows` job**
  (`tests/test_winpty.py`, run with `tests/test_packaging.py` + `tests/test_textutil.py`).
  The POSIX suites skip themselves there.
- Once the `make gate` target exists, `make gate` is the local gate — it runs exactly
  the four commands above.

## 4. Hard rules

- **VERSION LOCKSTEP.** `version` in `pyproject.toml` and `protoagent.plugin.yaml`
  must match — `tests/test_packaging.py::test_manifest_and_pyproject_versions_agree`
  enforces it. Every **user-visible** PR bumps **both** and **names the version in the
  PR title**, e.g. `fix: … (v0.9.2)`. Follow the repo history + `release.yml`; don't
  invent a scheme. (Docs-only changes like this one do not bump.)

- **NO RUNTIME PIP DEPS ON POSIX.** The PTY backend is stdlib only
  (`pty`/`os`/`fcntl`/`termios`); the host provides fastapi + langchain-core. **Windows**
  needs only `pywinpty`, declared via the manifest's `requires_pip` — never add a
  runtime dependency.

- **xterm.js is VENDORED** in `vendor/` (versions in `vendor/VERSIONS.md`) and served
  locally so the plugin works offline / airgapped. **Never load from a CDN**, **never
  hand-edit vendor files**, and use the **real vendored assets** — do not write a
  lookalike shim in their place.

- **Security invariants** — don't weaken any of these:
  - The PTY WebSocket authenticates with a **single-use, ~30s ticket** sent as the
    socket's **first frame** (`{type:"auth", ticket}`), minted at
    `POST /api/plugins/terminal/ticket` (the host's bearer-gated `/api/plugins/*`
    prefix). A socket that doesn't authenticate within 10s is closed.
  - **No credentials in URLs** — they land in access logs, history and proxies.
  - **Never send the operator/hub bearer down a fleet-proxied socket** (PR #14, v0.9.2).
    The ticket flow is what lets the hub swap the operator bearer for the member's
    fleet token; `authCredential` refuses the raw token behind the fleet proxy.
  - `public_paths` exempts **only** `/plugins/terminal/static/` (the vendored,
    non-secret assets) — nothing else.
  - Operator/agent **secrets are scrubbed** from the child shell's environment
    (`<AGENT>_API_KEY`, `A2A_AUTH_TOKEN`, …).

- **Agent tools are gated by `agent_access`** (`off | read | run`), and
  **`terminal_run` must never type into a tab where a program holds the foreground**
  (vim, a build, a REPL) — it would feed keystrokes into that program.

- **Pure view logic belongs in `web/logic.js` / `web/layout.js`**, where the node
  tests can cover it. Keep key maps, the token→ANSI palette, labels and split-pane
  layout math out of the DOM-bound `web/terminal.js`.

- **Windows parity.** Login shells, the foreground guard and `procinfo` (the cwd /
  running program) are **POSIX-only** — keep that in mind so Windows degrades
  gracefully rather than breaking.

## 5. Do / don't

**Do**

- Run all four gate commands before you call a change done.
- Bump `pyproject.toml` **and** `protoagent.plugin.yaml` together on any user-visible
  PR, and name the version in the PR title.
- Put testable logic in `web/logic.js` / `web/layout.js` and add a `node --test` case.
- Add a Python test that drives the real PTY / WS for backend behaviour.
- Keep the WS ticket/first-frame auth, the fleet-token swap and env scrubbing intact.
- Treat the change as **additive** — leave existing guards, fallbacks and defaults in
  place unless the task explicitly says to remove them.

**Don't**

- Add a runtime pip dependency on POSIX (Windows's only dep is `pywinpty`).
- Load anything from a CDN, or hand-edit / fake out files under `vendor/`.
- Put credentials in a URL, or send the operator/hub bearer down a fleet-proxied socket.
- Widen `public_paths` past `/plugins/terminal/static/`.
- Let `terminal_run` type into a foreground program, or ungate the agent tools.
- Bump the version on a docs-only change, or let `pyproject.toml` and
  `protoagent.plugin.yaml` drift out of lockstep.
