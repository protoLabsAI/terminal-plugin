"""The terminal console view — a self-contained xterm.js page served at
``/plugins/terminal/view`` (by the api.py router).

Four rules (ADR 0026/0042): served on the PUBLIC path · the WebSocket is the gated
channel (the operator bearer rides its FIRST frame, never the URL) · slug-aware base
(works on the host window AND through the fleet proxy) · links the DS plugin-kit.

TABS: several independent sessions in one view. Each tab owns its own xterm + fit
addon + WebSocket, attached to its own server-side shell by id. Shells OUTLIVE the
socket (sessions.py): the view asks to stay mounted while hidden, remembers its tabs
across reloads, and reattaches (replaying missed output) after any drop. Only the
tab's × ends the shell.

THEME: every terminal is themed from protoAgent's design system. The page reads the
console's ``--pl-*`` CSS tokens and maps them onto xterm's theme — background/
foreground/cursor/selection + the 16 ANSI colours — and RE-APPLIES on a live re-theme
(a MutationObserver on :root), across all open tabs.

No build step — vanilla JS; xterm.js + addons are VENDORED and served by this plugin
(offline). ``PAGE`` is the HTML template; api.render_page fills ``__TERMINAL_CONFIG__``
and returns it on GET /view.
"""

from __future__ import annotations

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal</title>
<script>
  // Server-rendered client config (font size, scrollback) — api.render_page fills it.
  var TERMINAL_CONFIG = __TERMINAL_CONFIG__;
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
  .tab .rename{font:inherit;width:12ch;padding:0 2px;background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg,#ededed);
    border:1px solid var(--pl-color-accent,#9b87f2);border-radius:3px;outline:none}
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
catch (e) { kit = { initPluginView(cb){ cb && cb(); }, getToken(){ return ""; }, apiFetch(p, i){ return fetch(BASE + p, i); } }; }

// A fresh single-use WS ticket from the GATED ticket route. kit.apiFetch awaits the
// console's bearer handshake and resolves the slug-aware base, so this is authenticated
// by the host — and, through the fleet hub, re-authenticated with the fleet token the
// member expects (the in-band operator token alone never matches a member's bearer).
// null ⇒ couldn't mint (older plugin, network): the auth frame falls back to the token.
// "denied" ⇒ the host refused us (401/403).
async function fetchTicket(){
  try {
    const r = await kit.apiFetch("/api/plugins/terminal/ticket", { method: "POST" });
    if (r.status === 401 || r.status === 403) return "denied";
    if (!r.ok) return null;
    const j = await r.json();
    return (j && typeof j.ticket === "string" && j.ticket) || null;
  } catch (e) { return null; }
}

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

// ── sessions (tabs) — each owns an xterm + fit + WebSocket, ATTACHED to a server-side
// shell by id. The shell outlives the socket (v0.5.0): switching views, a reload or a
// network blip only detaches; reconnecting reattaches and replays the output since.
const CFG = Object.assign({ fontSize: 13, scrollback: 5000 }, window.TERMINAL_CONFIG || {});
const sessions = new Map();   // id → { id, name, sessionId, term, fit, ws, el, status, statusCls, exited, detached, closing, retry, meta }
let activeId = null;
let counter = 0;

// Tabs (name + server session id) persist per agent (BASE), so a reload reattaches.
const STORE = "protoagent.terminal.tabs:" + (BASE || "/");
function save(){
  try {
    const all = [...sessions.values()];
    localStorage.setItem(STORE, JSON.stringify({
      active: all.findIndex((s) => s.id === activeId),
      tabs: all.map((s) => ({ name: s.name, session: s.sessionId || null })),
    }));
  } catch (e) {}
}
function load(){ try { return JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) { return null; } }

function setStatus(text, cls){ $("status").textContent = text; $("dot").className = "dot" + (cls ? " " + cls : ""); }
function setS(s, text, cls){ s.status = text; s.statusCls = cls; if (s.id === activeId) setStatus(text, cls); }

// The bearer goes in the FIRST frame, never the URL (URLs leak into logs + history).
function wsUrl(){
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + location.host + BASE + "/plugins/terminal/ws";
}

function send(s, obj){ if (s.ws && s.ws.readyState === 1) s.ws.send(JSON.stringify(obj)); }
function fit(s){ try { s.fit.fit(); } catch (e) {} send(s, { type: "resize", cols: s.term.cols, rows: s.term.rows }); }
const dim = (t) => "\r\n\x1b[2m" + t + "\x1b[0m\r\n";

function renderTabs(){
  const tabs = $("tabs"); tabs.innerHTML = "";
  for (const s of sessions.values()){
    const b = document.createElement("div");
    b.className = "tab" + (s.id === activeId ? " active" : ""); b.dataset.id = s.id; b.title = "Double-click to rename";
    const lbl = document.createElement("span"); lbl.className = "lbl"; lbl.textContent = s.name;
    const x = document.createElement("span"); x.className = "x"; x.textContent = "×"; x.title = "Close (ends the shell)";
    b.appendChild(lbl); b.appendChild(x); tabs.appendChild(b);
    b.onclick = (e) => { if (e.target === x) closeSession(s.id); else if (!b.querySelector("input")) switchTo(s.id); };
    b.ondblclick = (e) => { if (e.target !== x) rename(s, lbl); };
  }
}

// Inline rename (a sandboxed iframe cannot rely on a native dialog).
function rename(s, lbl){
  const inp = document.createElement("input"); inp.className = "rename"; inp.value = s.name;
  lbl.replaceWith(inp); inp.focus(); inp.select();
  let done = false;
  const finish = (commit) => {
    if (done) return; done = true;
    const v = inp.value.trim(); if (commit && v) { s.name = v; save(); }
    renderTabs(); s.term.focus();
  };
  inp.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); };
  inp.onblur = () => finish(true);
  inp.onclick = (e) => e.stopPropagation();
}

function switchTo(id){
  activeId = id;
  for (const s of sessions.values()) s.el.classList.toggle("active", s.id === id);
  // Toggle, don't rebuild: re-rendering the tab bar between the two clicks of a
  // double-click would swap the node out from under it and the rename would never fire.
  const tabEls = $("tabs").querySelectorAll(".tab");
  if (tabEls.length !== sessions.size) renderTabs();
  else tabEls.forEach((el) => el.classList.toggle("active", el.dataset.id === id));
  save();
  const s = sessions.get(id);
  if (s){ fit(s); s.term.focus(); $("shell").textContent = s.meta || ""; setStatus(s.status || "…", s.statusCls); }
}

async function connect(s){
  clearTimeout(s.retryTimer);
  const gen = (s.gen = (s.gen || 0) + 1);   // a newer connect() supersedes this one
  s.detached = false; s.fatal = "";
  setS(s, s.retry ? "reconnecting…" : "connecting…", "");
  // EVERY connect (first open, reconnect, reattach) mints its own ticket — they're single-use.
  const ticket = await fetchTicket();
  if (gen !== s.gen || s.closing) return;
  if (ticket === "denied") return setS(s, "unauthorized", "bad");
  const ws = new WebSocket(wsUrl());
  s.ws = ws;
  ws.onopen = () => {
    const hello = { type: "auth", session: s.sessionId || null, cols: s.term.cols, rows: s.term.rows };
    if (ticket) hello.ticket = ticket;
    else hello.token = (kit.getToken && kit.getToken()) || "";   // direct-connection fallback
    ws.send(JSON.stringify(hello));
  };
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.type === "data") s.term.write(m.data);
    else if (m.type === "connected"){
      const lost = s.sessionId && !m.resumed;   // we asked for a shell that is gone
      if (m.resumed || lost) s.term.reset();   // a resume's replay repaints from scratch
      if (lost) s.term.write(dim("[the previous shell ended — this is a new one]"));
      s.sessionId = m.session; s.retry = 0; s.exited = false; save();
      if (m.notice) s.term.write(dim("[" + m.notice + "]"));
      s.meta = (m.shell || "") + "  " + (m.cwd || "");
      if (s.id === activeId) $("shell").textContent = s.meta;
      setS(s, m.resumed ? "reattached" : "connected", "ok");
      fit(s);
    }
    else if (m.type === "exit"){
      s.exited = true; s.sessionId = null; save();
      s.term.write(dim("[process exited" + (m.exitCode != null ? " (" + m.exitCode + ")" : "") + (m.error ? ": " + m.error : "") + " — press any key for a new shell]"));
    }
    else if (m.type === "detached"){ s.detached = true; s.term.write(dim("[opened in another window — press any key to take it back]")); }
    else if (m.type === "error"){ s.fatal = m.message || "error"; s.term.write("\r\n\x1b[31m" + s.fatal + "\x1b[0m\r\n"); }
  };
  ws.onclose = (e) => {
    if (s.ws !== ws || s.closing) return;   // superseded, or the tab was closed
    if (s.exited) return setS(s, "exited", "bad");
    if (s.detached) return setS(s, "detached", "bad");
    if (e.code === 4001) return setS(s, "unauthorized", "bad");
    if (s.fatal) return setS(s, "error", "bad");
    // An unexpected drop: the shell is still alive server-side — reattach with backoff.
    const delay = Math.min(10000, 500 * Math.pow(2, s.retry++));
    setS(s, "reconnecting…", "");
    s.retryTimer = setTimeout(() => connect(s), delay);
  };
}

// Resolve the mono font CONCRETELY: the canvas renderer builds a canvas font string,
// which can't resolve a CSS var() — so read --pl-font-mono now and append fallbacks.
const monoVar = css("--pl-font-mono", "").replace(/['"]/g, "").trim();
const MONO = (monoVar ? monoVar + ", " : "") + "Menlo, Monaco, 'Courier New', monospace";

function newSession(saved){
  const id = "t" + (++counter);
  const el = document.createElement("div"); el.className = "termpane"; el.dataset.id = id; $("terms").appendChild(el);
  const term = new Terminal({
    cursorBlink: true, fontSize: CFG.fontSize, scrollback: CFG.scrollback, allowProposedApi: true,
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
  const s = { id, name: (saved && saved.name) || "Terminal " + counter, sessionId: (saved && saved.session) || null,
              term, fit: fitA, ws: null, el, status: "connecting…", statusCls: "", exited: false, detached: false,
              closing: false, retry: 0, retryTimer: 0, fatal: "", meta: "" };
  term.onData((d) => {
    if (s.exited){ s.exited = false; s.term.reset(); connect(s); return; }   // any key → a new shell
    if (s.detached){ connect(s); return; }                                  // any key → take it back
    send(s, { type: "input", data: d });
  });
  sessions.set(id, s);
  try { fitA.fit(); } catch (e) {}   // size before connecting so the shell spawns at the right size
  connect(s);
  if (!saved) switchTo(id);
  return s;
}

function closeSession(id){
  const s = sessions.get(id); if (!s) return;
  s.closing = true; clearTimeout(s.retryTimer);
  send(s, { type: "close" });   // ends the shell (a bare disconnect would only detach)
  try { if (s.ws) s.ws.close(); } catch (e) {}
  try { s.term.dispose(); } catch (e) {}
  s.el.remove(); sessions.delete(id);
  if (activeId === id){
    const next = sessions.keys().next().value;
    if (next) switchTo(next); else newSession();  // always keep at least one terminal
  } else { renderTabs(); save(); }
}

$("newtab").onclick = () => newSession();
new ResizeObserver(() => { const s = sessions.get(activeId); if (s) fit(s); }).observe($("terms"));
setInterval(() => { for (const s of sessions.values()) send(s, { type: "ping" }); }, 30000);

// Stay mounted while another console view is showing (bridge `background: true`) —
// otherwise the console unmounts this iframe and every terminal drops.
function stayMounted(){
  if (window.parent !== window) parent.postMessage({ type: "protoagent:subscribe", patterns: [], background: true }, "*");
}

// Boot once: restore the saved tabs (reattaching their shells) or open a fresh one.
// The handshake supplies bearer + theme first when present.
let booted = false;
function boot(){
  if (booted) return; booted = true;
  applyTheme(); stayMounted();
  const saved = load();
  if (saved && Array.isArray(saved.tabs) && saved.tabs.length){
    for (const t of saved.tabs) newSession(t);
    const ids = [...sessions.keys()];
    switchTo(ids[saved.active] || ids[0]);
  } else newSession();
}
kit.initPluginView(() => {
  applyTheme(); stayMounted(); boot();
  // The handshake can land after the boot fallback already tried with no bearer — retry
  // any tab that was turned away now that a token is here.
  for (const s of sessions.values()) if (s.status === "unauthorized") { s.retry = 0; connect(s); }
});
setTimeout(boot, 1000);
</script></body></html>"""
