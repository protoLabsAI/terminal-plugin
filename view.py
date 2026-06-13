"""The terminal console view — a self-contained xterm.js page served at
``/plugins/terminal/view`` (by the api.py router).

Four rules (ADR 0026/0042): served on the PUBLIC path · the WebSocket is the gated
channel (it carries the operator bearer as a ?token= param) · slug-aware base (works
on the host window AND through the fleet proxy) · links the DS plugin-kit.

THEME: the terminal is themed from protoAgent's design system. The page reads the
console's ``--pl-*`` CSS tokens (set by the DS kit's protoagent:init handshake) and
maps them onto xterm's theme object — background/foreground/cursor/selection + the 16
ANSI colours — and RE-APPLIES on a live re-theme (a MutationObserver on :root). So
the terminal always matches the console's theme.

No build step — vanilla JS; xterm.js + addons load from jsDelivr (vendoring is a
follow-up). ``PAGE`` is the HTML; api.py returns it on GET /view.
"""

from __future__ import annotations

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<script>
  // RULE 3 — slug-aware base: "" on the host window, "/agents/<slug>" through the
  // fleet proxy. Split on the prefix this page is served under; every asset + the WS
  // URL hangs off it.
  var BASE = location.pathname.split("/plugins/")[0];
  // RULE 4 — link the DS kit CSS off BASE so the chrome themes live off --pl-* tokens.
  (function(){ var l=document.createElement("link"); l.rel="stylesheet";
    l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
</script>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg,#ededed);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);font-size:12px}
  .wrap{display:flex;flex-direction:column;height:100%}
  .bar{display:flex;align-items:center;gap:var(--pl-space-3,10px);padding:6px 12px;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border,#26262b);flex:0 0 auto}
  .bar .t{font-weight:600;color:var(--pl-color-fg,#ededed)} .bar .sp{flex:1}
  .meta{color:var(--pl-color-fg-muted,#9a9aa5);font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:11px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--pl-color-fg-muted,#9a9aa5);margin-right:6px}
  .dot.ok{background:var(--pl-color-status-success,#4ade80)} .dot.bad{background:var(--pl-color-status-error,#f87171)}
  button.pl{background:var(--pl-color-bg-raised,#1a1a1f);color:var(--pl-color-fg,#ededed);
    border:var(--pl-border-width,1px) solid var(--pl-color-border,#26262b);border-radius:var(--pl-radius,6px);
    padding:3px 10px;font-size:11px;cursor:pointer}
  #term{flex:1 1 auto;min-height:0;padding:6px 8px;background:var(--pl-color-bg,#0a0a0c)}
  #err{padding:12px;color:var(--pl-color-status-error,#f87171)}
</style>
</head><body><div class="wrap">
  <div class="bar">
    <span class="t">Terminal</span>
    <span class="meta" id="shell"></span>
    <span class="sp"></span>
    <span class="meta"><span class="dot" id="dot"></span><span id="status">connecting…</span></span>
    <button class="pl" id="reconnect" style="display:none" onclick="window.__reconnect()">Reconnect</button>
  </div>
  <div id="term"></div>
  <div id="err" hidden></div>
</div>
<script type="module">
// The DS kit owns the protoagent:init handshake (operator bearer + theme tokens) and
// the slug-aware token. ESM module → dynamic import. Fallback to a tokenless shim on
// an older host with no /_ds (the terminal still works on a no-auth/loopback host).
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(cb){ cb && cb(); }, getToken(){ return ""; } }; }

let Terminal, FitAddon, WebLinksAddon;
try {
  ({ Terminal } = await import("https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/+esm"));
  ({ FitAddon } = await import("https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/+esm"));
  ({ WebLinksAddon } = await import("https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/+esm"));
} catch (e) {
  document.getElementById("err").hidden = false;
  document.getElementById("err").textContent = "Could not load xterm.js (offline?). " + e;
  throw e;
}

const $ = (id) => document.getElementById(id);

// ── theme: map protoAgent's --pl-* tokens onto xterm's theme object ────────────
const css = (name, fb) => (getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb);
function xtermTheme(){
  return {
    background: css("--pl-color-bg", "#0a0a0c"),
    foreground: css("--pl-color-fg", "#ededed"),
    cursor: css("--pl-color-accent", "#9b87f2"),
    cursorAccent: css("--pl-color-bg", "#0a0a0c"),
    selectionBackground: "rgba(155,135,242,0.35)",
    // 16 ANSI — status tokens where they map cleanly, sensible defaults otherwise, so
    // colourful CLI output (git, ls, grep…) reads well against the console's palette.
    black: css("--pl-color-bg-raised", "#1a1a1f"),
    red: css("--pl-color-status-error", "#f87171"),
    green: css("--pl-color-status-success", "#4ade80"),
    yellow: css("--pl-color-status-warning", "#fbbf24"),
    blue: css("--pl-color-status-info", "#60a5fa"),
    magenta: css("--pl-color-accent", "#c084fc"),
    cyan: "#22d3ee",
    white: css("--pl-color-fg-muted", "#cbd5e1"),
    brightBlack: "#475569", brightRed: "#fca5a5", brightGreen: "#86efac", brightYellow: "#fde68a",
    brightBlue: "#93c5fd", brightMagenta: "#d8b4fe", brightCyan: "#67e8f9",
    brightWhite: css("--pl-color-fg", "#f8fafc"),
  };
}

const term = new Terminal({
  cursorBlink: true, fontSize: 13, scrollback: 5000, allowProposedApi: true,
  fontFamily: "var(--pl-font-mono), Menlo, Monaco, 'Courier New', monospace",
  theme: xtermTheme(),
});
const fitAddon = new FitAddon();
term.loadAddon(fitAddon);
term.loadAddon(new WebLinksAddon());
term.open($("term"));
function applyTheme(){ try { term.options.theme = xtermTheme(); } catch (e) {} }
// Live re-theme: the kit re-sets the --pl-* vars on :root → rebuild the xterm theme.
new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["style", "class"] });

// ── transport: a WebSocket to the PTY bridge, carrying the operator bearer ──────
let ws = null;
function status(text, cls){ $("status").textContent = text; $("dot").className = "dot" + (cls ? " " + cls : ""); }
function send(obj){ if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
function fit(){ try { fitAddon.fit(); } catch (e) {} send({ type: "resize", cols: term.cols, rows: term.rows }); }

function wsUrl(){
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const tok = (kit.getToken && kit.getToken()) || "";
  return proto + "//" + location.host + BASE + "/plugins/terminal/ws?token=" + encodeURIComponent(tok);
}

let exited = false;
function connect(){
  $("reconnect").style.display = "none";
  status("connecting…");
  ws = new WebSocket(wsUrl());
  ws.onopen = () => { status("connected", "ok"); fit(); term.focus(); };
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.type === "data") term.write(m.data);
    else if (m.type === "connected") { $("shell").textContent = (m.shell || "") + "  " + (m.cwd || ""); status("connected", "ok"); }
    else if (m.type === "exit") { exited = true; term.write("\r\n\x1b[2m[process exited" + (m.exitCode != null ? " (" + m.exitCode + ")" : "") + "]\x1b[0m\r\n"); }
  };
  ws.onclose = () => { status(exited ? "exited" : "disconnected", "bad"); $("reconnect").style.display = ""; };
  ws.onerror = () => status("error", "bad");
}
window.__reconnect = () => { exited = false; term.reset(); connect(); };

term.onData((d) => send({ type: "input", data: d }));
new ResizeObserver(() => fit()).observe($("term"));
setInterval(() => send({ type: "ping" }), 30000);

// Boot once, on whichever fires first: the handshake (bearer + theme arrive with it,
// so the WS authenticates) or a short timer (no-handshake / standalone).
let booted = false;
function boot(){ if (booted) return; booted = true; applyTheme(); connect(); }
kit.initPluginView(() => { applyTheme(); boot(); });
setTimeout(boot, 1000);
</script></body></html>"""
