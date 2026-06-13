"""The terminal console view — a self-contained xterm.js page served at
``/plugins/terminal/view`` (by the api.py router).

Four rules (ADR 0026/0042): served on the PUBLIC path · the WebSocket is the gated
channel (it carries the operator bearer as a ?token= param) · slug-aware base (works
on the host window AND through the fleet proxy) · links the DS plugin-kit.

TABS: several independent sessions in one view. Each tab owns its own xterm + fit
addon + WebSocket — and each WebSocket gets its own PTY shell on the backend (one PTY
per connection), so closing a tab kills only that shell.

THEME: every terminal is themed from protoAgent's design system. The page reads the
console's ``--pl-*`` CSS tokens and maps them onto xterm's theme — background/
foreground/cursor/selection + the 16 ANSI colours — and RE-APPLIES on a live re-theme
(a MutationObserver on :root), across all open tabs.

No build step — vanilla JS; xterm.js + addons are VENDORED and served by this plugin
(offline). ``PAGE`` is the HTML; api.py returns it on GET /view.
"""

from __future__ import annotations

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal</title>
<script>
  // RULE 3 — slug-aware base ("" on host, "/agents/<slug>" through the fleet proxy).
  var BASE = location.pathname.split("/plugins/")[0];
  // Stylesheets, BASE-prefixed: the VENDORED xterm css (offline) + the DS kit css (rule 4).
  (function(){
    ["/plugins/terminal/static/xterm.css", "/_ds/plugin-kit.css"].forEach(function(p){
      var l=document.createElement("link"); l.rel="stylesheet"; l.href=BASE+p; document.head.appendChild(l);
    });
  })();
</script>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg,#ededed);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);font-size:12px}
  .wrap{display:flex;flex-direction:column;height:100%}
  .bar{display:flex;align-items:center;gap:6px;padding:4px 8px;flex:0 0 auto;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border,#26262b)}
  .tabs{display:flex;gap:4px;overflow-x:auto;max-width:70%}
  .tab{display:flex;align-items:center;gap:6px;padding:3px 8px;border-radius:var(--pl-radius,6px);
    background:transparent;color:var(--pl-color-fg-muted,#9a9aa5);border:1px solid transparent;
    cursor:pointer;font-size:11px;white-space:nowrap}
  .tab:hover{background:var(--pl-color-bg-raised,#1a1a1f)}
  .tab.active{background:var(--pl-color-bg-raised,#1a1a1f);color:var(--pl-color-fg,#ededed);
    border-color:var(--pl-color-border,#26262b)}
  .tab .x{opacity:.6;font-size:13px;line-height:1} .tab .x:hover{opacity:1;color:var(--pl-color-status-error,#f87171)}
  button.pl{background:var(--pl-color-bg-raised,#1a1a1f);color:var(--pl-color-fg,#ededed);
    border:var(--pl-border-width,1px) solid var(--pl-color-border,#26262b);border-radius:var(--pl-radius,6px);
    padding:2px 9px;font-size:13px;cursor:pointer;line-height:1.2}
  .sp{flex:1}
  .meta{color:var(--pl-color-fg-muted,#9a9aa5);font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:11px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--pl-color-fg-muted,#9a9aa5);margin-right:6px}
  .dot.ok{background:var(--pl-color-status-success,#4ade80)} .dot.bad{background:var(--pl-color-status-error,#f87171)}
  #terms{position:relative;flex:1 1 auto;min-height:0}
  .termpane{position:absolute;inset:0;padding:6px 8px;background:var(--pl-color-bg,#0a0a0c);display:none}
  .termpane.active{display:block}
  #err{padding:12px;color:var(--pl-color-status-error,#f87171)}
</style>
</head><body><div class="wrap">
  <div class="bar">
    <div class="tabs" id="tabs"></div>
    <button class="pl" id="newtab" title="New terminal">+</button>
    <span class="sp"></span>
    <span class="meta" id="shell"></span>
    <span class="meta"><span class="dot" id="dot"></span><span id="status">connecting…</span></span>
  </div>
  <div id="terms"></div>
  <div id="err" hidden></div>
</div>
<script type="module">
// The DS kit owns the protoagent:init handshake (operator bearer + theme tokens) and
// the slug-aware token. ESM module → dynamic import. Fallback to a tokenless shim.
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(cb){ cb && cb(); }, getToken(){ return ""; } }; }

// Load the VENDORED xterm UMD bundles (served by this plugin — offline), then read
// their globals: xterm spreads its exports onto window (→ window.Terminal); the addons
// expose window.<Name>.<Name>.
function loadScript(src){
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; s.onload = res; s.onerror = () => rej(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}
const ST = BASE + "/plugins/terminal/static/";
let Terminal, FitAddon, WebLinksAddon, CanvasAddon;
try {
  await loadScript(ST + "xterm.js");
  await Promise.all([
    loadScript(ST + "addon-fit.js"),
    loadScript(ST + "addon-web-links.js"),
    loadScript(ST + "addon-canvas.js"),
  ]);
  Terminal = window.Terminal;
  FitAddon = window.FitAddon.FitAddon;
  WebLinksAddon = window.WebLinksAddon.WebLinksAddon;
  CanvasAddon = window.CanvasAddon.CanvasAddon;
} catch (e) {
  document.getElementById("err").hidden = false;
  document.getElementById("err").textContent = "Could not load the terminal assets. " + e;
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
function applyTheme(){ const t = xtermTheme(); for (const s of sessions.values()){ try { s.term.options.theme = t; } catch (e) {} } }
// Live re-theme: the kit re-sets the --pl-* vars on :root → rebuild every tab's theme.
new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["style", "class"] });

// ── sessions (tabs) — each owns an xterm + fit + WebSocket (→ its own PTY) ──────
const sessions = new Map();   // id → { id, name, term, fit, ws, el, status, statusCls, exited, meta }
let activeId = null;
let counter = 0;

function setStatus(text, cls){ $("status").textContent = text; $("dot").className = "dot" + (cls ? " " + cls : ""); }

function wsUrl(){
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const tok = (kit.getToken && kit.getToken()) || "";
  return proto + "//" + location.host + BASE + "/plugins/terminal/ws?token=" + encodeURIComponent(tok);
}

function fit(s){ try { s.fit.fit(); } catch (e) {} if (s.ws && s.ws.readyState === 1) s.ws.send(JSON.stringify({ type: "resize", cols: s.term.cols, rows: s.term.rows })); }

function renderTabs(){
  const tabs = $("tabs"); tabs.innerHTML = "";
  for (const s of sessions.values()){
    const b = document.createElement("button");
    b.className = "tab" + (s.id === activeId ? " active" : ""); b.dataset.id = s.id;
    const lbl = document.createElement("span"); lbl.className = "lbl"; lbl.textContent = s.name;
    const x = document.createElement("span"); x.className = "x"; x.textContent = "×"; x.title = "Close";
    b.appendChild(lbl); b.appendChild(x); tabs.appendChild(b);
    b.onclick = (e) => { if (e.target === x) closeSession(s.id); else switchTo(s.id); };
    b.ondblclick = (e) => { if (e.target !== x){ const n = prompt("Rename terminal", s.name); if (n){ s.name = n; renderTabs(); } } };
  }
}

function switchTo(id){
  activeId = id;
  for (const s of sessions.values()) s.el.classList.toggle("active", s.id === id);
  renderTabs();
  const s = sessions.get(id);
  if (s){ fit(s); s.term.focus(); $("shell").textContent = s.meta || ""; setStatus(s.status || "…", s.statusCls); }
}

function connect(s){
  s.ws = new WebSocket(wsUrl());
  s.ws.onopen = () => { s.status = "connected"; s.statusCls = "ok"; if (s.id === activeId) setStatus("connected", "ok"); fit(s); };
  s.ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.type === "data") s.term.write(m.data);
    else if (m.type === "connected"){ s.meta = (m.shell || "") + "  " + (m.cwd || ""); if (s.id === activeId) $("shell").textContent = s.meta; }
    else if (m.type === "exit"){ s.exited = true; s.term.write("\r\n\x1b[2m[process exited" + (m.exitCode != null ? " (" + m.exitCode + ")" : "") + "]\x1b[0m\r\n"); }
  };
  s.ws.onclose = () => { s.status = s.exited ? "exited" : "disconnected"; s.statusCls = "bad"; if (s.id === activeId) setStatus(s.status, "bad"); };
  s.ws.onerror = () => { s.status = "error"; s.statusCls = "bad"; if (s.id === activeId) setStatus("error", "bad"); };
}

// Resolve the mono font CONCRETELY: the canvas renderer builds a canvas font string,
// which can't resolve a CSS var() — so read --pl-font-mono now and append fallbacks.
const monoVar = css("--pl-font-mono", "").replace(/['"]/g, "").trim();
const MONO = (monoVar ? monoVar + ", " : "") + "Menlo, Monaco, 'Courier New', monospace";

function newSession(){
  const id = "t" + (++counter);
  const el = document.createElement("div"); el.className = "termpane"; el.dataset.id = id; $("terms").appendChild(el);
  const term = new Terminal({
    cursorBlink: true, fontSize: 13, scrollback: 5000, allowProposedApi: true,
    fontFamily: MONO,
    lineHeight: 1.0,        // flush rows
    customGlyphs: true,     // draw block/box glyphs as exact cell-filling shapes (needs canvas/webgl)
    theme: xtermTheme(),
  });
  const fitA = new FitAddon(); term.loadAddon(fitA); term.loadAddon(new WebLinksAddon()); term.open(el);
  // The CANVAS renderer is what makes customGlyphs work — block/box art renders flush
  // (the default DOM renderer draws them from the font, which leaves seams). Best-effort:
  // a renderer failure just falls back to the DOM renderer.
  try { term.loadAddon(new CanvasAddon()); } catch (e) {}
  const s = { id, name: "Terminal " + counter, term, fit: fitA, ws: null, el, status: "connecting…", statusCls: "", exited: false, meta: "" };
  term.onData((d) => { if (s.ws && s.ws.readyState === 1) s.ws.send(JSON.stringify({ type: "input", data: d })); });
  sessions.set(id, s);
  connect(s);
  switchTo(id);
  return s;
}

function closeSession(id){
  const s = sessions.get(id); if (!s) return;
  try { if (s.ws) s.ws.close(); } catch (e) {}
  try { s.term.dispose(); } catch (e) {}
  s.el.remove(); sessions.delete(id);
  if (activeId === id){
    const next = sessions.keys().next().value;
    if (next) switchTo(next); else newSession();  // always keep at least one terminal
  } else renderTabs();
}

$("newtab").onclick = () => newSession();
new ResizeObserver(() => { const s = sessions.get(activeId); if (s) fit(s); }).observe($("terms"));
setInterval(() => { for (const s of sessions.values()) if (s.ws && s.ws.readyState === 1) s.ws.send(JSON.stringify({ type: "ping" })); }, 30000);

// Boot once: open the first tab (the handshake supplies bearer + theme first if present).
let booted = false;
function boot(){ if (booted) return; booted = true; applyTheme(); newSession(); }
kit.initPluginView(() => { applyTheme(); boot(); });
setTimeout(boot, 1000);
</script></body></html>"""
