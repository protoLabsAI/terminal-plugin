"""The terminal console view — the page served at ``/plugins/terminal/view``.

Four rules (ADR 0026/0042): served on the PUBLIC path · the WebSocket is the gated
channel (the operator bearer rides its FIRST frame, never the URL) · slug-aware base
(works on the host window AND through the fleet proxy) · links the DS plugin-kit.

This module is only the page SHELL — markup, styles, and a bootstrap that loads the DS
kit, the VENDORED xterm bundles (offline, no CDN) and then the app. The app itself is
plain ES modules under ``web/`` (served from ``/plugins/terminal/static/``):
``web/terminal.js`` (tabs, split panes, sessions, keys, find, menu, theme), plus the
pure, ``node --test``-tested ``web/logic.js`` (keys, theme, labels) and ``web/layout.js``
(split-pane trees). No build step.

``PAGE`` is a template: api.render_page fills ``__TERMINAL_CONFIG__`` with the
client-side config on every load, so a Settings change applies on the next view load.
"""

from __future__ import annotations

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal</title>
<script>
  // Server-rendered client config — api.render_page fills it.
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
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);font-size:12px;overflow:hidden}
  .wrap{display:flex;flex-direction:column;height:100%}
  .bar{display:flex;align-items:center;gap:6px;padding:4px 8px;flex:0 0 auto;min-width:0;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border,#26262b)}
  .tabs{display:flex;gap:4px;overflow-x:auto;min-width:0;flex:0 1 auto;scrollbar-width:none}
  .tabs::-webkit-scrollbar{display:none}
  .tab{display:flex;align-items:center;gap:6px;padding:3px 8px;border-radius:var(--pl-radius,6px);
    background:transparent;color:var(--pl-color-fg-muted,#9a9aa5);border:1px solid transparent;
    cursor:pointer;font-size:11px;white-space:nowrap;flex:0 0 auto;user-select:none}
  .tab:hover{background:var(--pl-color-bg-raised,#1a1a1f)}
  .tab.active{background:var(--pl-color-bg-raised,#1a1a1f);color:var(--pl-color-fg,#ededed);
    border-color:var(--pl-color-border,#26262b)}
  .tab .tdot{width:6px;height:6px;border-radius:50%;background:var(--pl-color-fg-subtle,#6b6b76);flex:0 0 auto}
  .tab .tdot.ok{background:var(--pl-color-status-success,#4ade80)} .tab .tdot.bad{background:var(--pl-color-status-error,#f87171)}
  .tab .rename{font:inherit;width:14ch;padding:0 2px;background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg,#ededed);
    border:1px solid var(--pl-color-accent,#9b87f2);border-radius:3px;outline:none}
  .tab .x{opacity:.6;font-size:13px;line-height:1} .tab .x:hover{opacity:1;color:var(--pl-color-status-error,#f87171)}
  button.pl{background:var(--pl-color-bg-raised,#1a1a1f);color:var(--pl-color-fg,#ededed);
    border:var(--pl-border-width,1px) solid var(--pl-color-border,#26262b);border-radius:var(--pl-radius,6px);
    padding:2px 9px;font-size:13px;cursor:pointer;line-height:1.2;flex:0 0 auto}
  .sp{flex:1 1 0;min-width:8px}
  .meta{color:var(--pl-color-fg-muted,#9a9aa5);font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:11px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto;direction:rtl;text-align:left}
  .stat{color:var(--pl-color-fg-muted,#9a9aa5);font-size:11px;white-space:nowrap;flex:0 0 auto}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--pl-color-fg-muted,#9a9aa5);margin-right:6px}
  .dot.ok{background:var(--pl-color-status-success,#4ade80)} .dot.bad{background:var(--pl-color-status-error,#f87171)}
  #terms{position:relative;flex:1 1 auto;min-height:0}
  /* one body per tab; its layout tree renders as nested flex rows/cols of panes */
  .tabbody{position:absolute;inset:0;display:none;background:var(--pl-color-bg,#0a0a0c)}
  .tabbody.active{display:flex}
  .split{display:flex;min-width:0;min-height:0}
  .split.row{flex-direction:row} .split.col{flex-direction:column}
  .pane{position:relative;min-width:0;min-height:0;background:var(--pl-color-bg,#0a0a0c)}
  .pane .xhost{position:absolute;inset:6px 8px}
  .tabbody.multi .pane .xhost{inset:4px 6px}
  .tabbody.multi .pane:not(.active){opacity:.62}
  .tabbody.multi .pane:not(.active):hover{opacity:.8}
  .divider{flex:0 0 5px;background:var(--pl-color-border,#26262b);position:relative;z-index:2}
  .split.row>.divider{cursor:col-resize;margin:0 -2px;border-left:2px solid var(--pl-color-bg,#0a0a0c);border-right:2px solid var(--pl-color-bg,#0a0a0c)}
  .split.col>.divider{cursor:row-resize;margin:-2px 0;border-top:2px solid var(--pl-color-bg,#0a0a0c);border-bottom:2px solid var(--pl-color-bg,#0a0a0c)}
  .divider:hover{background:var(--pl-color-accent,#9b87f2)}
  .drag-shield{position:fixed;inset:0;z-index:50} .drag-shield.row{cursor:col-resize} .drag-shield.col{cursor:row-resize}
  /* new panes are opened here (laid out, invisible) so xterm can measure before placement */
  #staging{position:absolute;inset:0;visibility:hidden;pointer-events:none;overflow:hidden}
  #staging>.pane{position:absolute;inset:0}
  .tab .npanes{font-size:10px;line-height:1;padding:1px 4px;border-radius:8px;color:var(--pl-color-fg-muted,#9a9aa5);
    border:1px solid var(--pl-color-border,#26262b)}
  #err{padding:12px;color:var(--pl-color-status-error,#f87171)}
  /* find bar — floats over the top-right of the active terminal */
  #find{position:absolute;top:6px;right:14px;z-index:6;display:flex;align-items:center;gap:4px;padding:4px;
    background:var(--pl-color-bg-raised,#1a1a1f);border:1px solid var(--pl-color-border-strong,#3a3a42);
    border-radius:var(--pl-radius,6px);box-shadow:0 4px 16px rgba(0,0,0,.35)}
  #find[hidden]{display:none}
  #find input{font:inherit;font-size:12px;width:22ch;padding:3px 6px;background:var(--pl-color-bg,#0a0a0c);
    color:var(--pl-color-fg,#ededed);border:1px solid var(--pl-color-border,#26262b);border-radius:4px;outline:none}
  #find input:focus{border-color:var(--pl-color-focus,var(--pl-color-accent,#9b87f2))}
  #find.miss input{border-color:var(--pl-color-status-error,#f87171)}
  #find button{font:inherit;font-size:11px;min-width:24px;padding:2px 6px;background:transparent;color:var(--pl-color-fg-muted,#9a9aa5);
    border:1px solid transparent;border-radius:4px;cursor:pointer}
  #find button:hover{color:var(--pl-color-fg,#ededed);background:var(--pl-color-bg-hover,#24242a)}
  #find button.on{color:var(--pl-color-accent-fg,var(--pl-color-fg,#ededed));background:var(--pl-color-accent,#9b87f2)}
  #findn{min-width:5ch;text-align:right;color:var(--pl-color-fg-muted,#9a9aa5);font-size:11px;font-variant-numeric:tabular-nums}
</style>
</head><body><div class="wrap">
  <div class="bar">
    <div class="tabs" id="tabs" role="tablist" aria-label="Terminals"></div>
    <button class="pl" id="newtab" title="New terminal" aria-label="New terminal">+</button>
    <span class="sp"></span>
    <span class="meta" id="shell"></span>
    <span class="stat"><span class="dot" id="dot"></span><span id="status">connecting…</span></span>
  </div>
  <div id="terms">
    <div id="staging" aria-hidden="true"></div>
    <div id="find" hidden role="search">
      <input id="findq" type="text" placeholder="Find" aria-label="Find in terminal" spellcheck="false" autocomplete="off">
      <span id="findn"></span>
      <button id="findCase" title="Match case" aria-pressed="false">Aa</button>
      <button id="findRe" title="Regular expression" aria-pressed="false">.*</button>
      <button id="findPrev" title="Previous (Shift+Enter)" aria-label="Previous match">↑</button>
      <button id="findNext" title="Next (Enter)" aria-label="Next match">↓</button>
      <button id="findClose" title="Close (Esc)" aria-label="Close find">×</button>
    </div>
  </div>
  <div id="err" hidden></div>
</div>
<script type="module">
// The DS kit owns the protoagent:init handshake (operator bearer + theme tokens) and the
// slug-aware token. ESM module → dynamic import. Fallback to a tokenless shim (standalone).
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(cb){ cb && cb(); }, getToken(){ return ""; } }; }

// Load the VENDORED xterm UMD bundles (served by this plugin — offline), then read their
// globals: xterm spreads its exports onto window (→ window.Terminal); each addon exposes
// window.<Name>.<Name>. Optional addons degrade: a missing one just disables its feature.
function loadScript(src){
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; s.onload = res; s.onerror = () => rej(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}
const ST = BASE + "/plugins/terminal/static/";
const fail = (e) => { const el = document.getElementById("err"); el.hidden = false; el.textContent = "Could not load the terminal. " + e; throw e; };
try {
  await loadScript(ST + "xterm.js");
  await Promise.all(["addon-fit.js", "addon-web-links.js", "addon-canvas.js"].map((f) => loadScript(ST + f)));
  await Promise.allSettled(["addon-webgl.js", "addon-search.js", "addon-unicode11.js"].map((f) => loadScript(ST + f)));
} catch (e) { fail(e); }
const g = (name) => (window[name] && window[name][name]) || null;
const xt = {
  Terminal: window.Terminal, FitAddon: g("FitAddon"), WebLinksAddon: g("WebLinksAddon"), CanvasAddon: g("CanvasAddon"),
  WebglAddon: g("WebglAddon"), SearchAddon: g("SearchAddon"), Unicode11Addon: g("Unicode11Addon"),
};
let app;
try { app = await import(ST + "terminal.js"); } catch (e) { fail(e); }
app.boot({ kit, BASE, CFG: window.TERMINAL_CONFIG || {}, xt });
</script></body></html>"""
