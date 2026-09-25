// The terminal view app. Loaded by the page's bootstrap (view.py) AFTER the DS kit and the
// vendored xterm bundles; `boot(ctx)` receives { kit, BASE, CFG, xt } where xt holds the
// xterm constructors. Pure logic lives in logic.js (keys, theme, labels) and layout.js
// (split-pane trees).
//
// Model: TABS hold a layout tree of PANES (layout.js). Each pane is one xterm attached over
// its own WebSocket to a server-side shell by id. Shells OUTLIVE the socket (sessions.py):
// the view stays mounted while hidden, remembers tabs + layouts across reloads, and
// reattaches (replaying missed output) after a drop. Closing a pane or tab ends its shells.

import { keyAction, buildTheme, tabLabel, clampFont } from "./logic.js";
import { leaf, panesOf, split, remove, neighbor, resize, mapPanes, validate } from "./layout.js";

const IS_MAC = /Mac|iP(hone|ad|od)/.test(navigator.platform || navigator.userAgent || "");
const $ = (id) => document.getElementById(id);
const dim = (t) => "\r\n\x1b[2m" + t + "\x1b[0m\r\n";

export function boot({ kit, BASE, CFG, xt }) {
  const { Terminal, FitAddon, WebLinksAddon, CanvasAddon, WebglAddon, SearchAddon, Unicode11Addon } = xt;
  const inFrame = window.parent !== window;
  const post = (msg) => { if (inFrame) parent.postMessage(msg, "*"); };

  // ── theme: every colour from the console's --pl-* tokens ─────────────────────
  // Resolve each token to RGB through a canvas pixel: a token may be hex, rgb(), oklch()
  // or color-mix(), and getComputedStyle hands custom properties back unresolved.
  const probe = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const tokenRGB = (name, fallback) => {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
    probe.clearRect(0, 0, 1, 1);
    probe.fillStyle = fallback; probe.fillStyle = raw;   // an invalid value keeps the fallback
    probe.fillRect(0, 0, 1, 1);
    const [r, g, b] = probe.getImageData(0, 0, 1, 1).data;
    return [r, g, b];
  };
  function xtermTheme() {
    return buildTheme({
      bg: tokenRGB("--pl-color-bg", "#0a0a0c"),
      fg: tokenRGB("--pl-color-fg", "#ededed"),
      fgMuted: tokenRGB("--pl-color-fg-muted", "#9a9aa5"),
      fgSubtle: tokenRGB("--pl-color-fg-subtle", "#6b6b76"),
      bgRaised: tokenRGB("--pl-color-bg-raised", "#1a1a1f"),
      accent: tokenRGB("--pl-color-accent", "#9b87f2"),
      error: tokenRGB("--pl-color-status-error", "#f87171"),
      success: tokenRGB("--pl-color-status-success", "#4ade80"),
      warning: tokenRGB("--pl-color-status-warning", "#fbbf24"),
      info: tokenRGB("--pl-color-status-info", "#60a5fa"),
    });
  }
  let theme = xtermTheme();
  function applyTheme() {
    theme = xtermTheme();
    for (const p of panes.values()) { try { p.term.options.theme = theme; } catch (e) {} }
  }
  // Live re-theme: the kit re-sets the --pl-* vars on :root → rebuild every pane's theme.
  new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["style", "class", "data-theme"] });

  // The canvas/WebGL renderers build a font string that can't resolve var() — resolve the
  // mono stack concretely. An explicit font_family setting wins.
  const monoVar = getComputedStyle(document.documentElement).getPropertyValue("--pl-font-mono").replace(/['"]/g, "").trim();
  const MONO = CFG.fontFamily || ((monoVar ? monoVar + ", " : "") + "Menlo, Monaco, 'Courier New', monospace");

  // ── per-agent persisted UI state (tabs, layouts, zoom) ────────────────────────
  const STORE = "protoagent.terminal.tabs:" + (BASE || "/");
  const ZOOM = "protoagent.terminal.fontSize:" + (BASE || "/");
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} };
  let fontSize = clampFont(lsGet(ZOOM) || CFG.fontSize, CFG.fontSize);

  const panes = new Map();   // paneId → pane (one xterm + socket + server shell)
  const tabs = [];           // [{ id, name, customName, root, activePane, el }]
  let activeTabId = null;
  let paneSeq = 0, tabSeq = 0;
  let booted = false;

  const tabById = (id) => tabs.find((t) => t.id === id) || null;
  const activeTab = () => tabById(activeTabId);
  const activePane = () => { const t = activeTab(); return t ? panes.get(t.activePane) || null : null; };

  function save() {
    lsSet(STORE, JSON.stringify({
      active: tabs.findIndex((t) => t.id === activeTabId),
      tabs: tabs.map((t) => ({
        name: t.name, customName: t.customName || null,
        // Session ids ("" = a pane whose shell hasn't connected yet → a fresh one).
        layout: mapPanes(t.root, (id) => { const p = panes.get(id); return p ? (p.sessionId || "") : null; }),
        activeIndex: Math.max(0, panesOf(t.root).indexOf(t.activePane)),
      })),
    }));
  }
  function load() {
    let raw; try { raw = JSON.parse(lsGet(STORE) || "null"); } catch (e) { return []; }
    if (!raw || !Array.isArray(raw.tabs)) return [];
    const out = raw.tabs.map((t) => ({
      name: t && t.name, customName: t && t.customName,
      // v0.5–0.7 saved one session per tab; v0.8+ saves a layout tree.
      layout: (t && validate(t.layout)) || leaf((t && t.session) || ""),
      activeIndex: (t && t.activeIndex) || 0,
    }));
    out.active = raw.active;
    return out;
  }

  // ── status + socket plumbing ──────────────────────────────────────────────────
  // The shell/cwd readout truncates from the LEFT (direction: rtl) so the cwd's tail stays
  // visible; LRM marks keep the path's own punctuation ("/bin/zsh") from being reordered.
  function setMeta(text) { $("shell").textContent = text ? "‎" + text + "‎" : ""; $("shell").title = text || ""; }
  function setStatus(text, cls) { $("status").textContent = text; $("dot").className = "dot" + (cls ? " " + cls : ""); }
  const isActive = (p) => { const t = activeTab(); return !!t && t.activePane === p.id; };
  function setS(p, text, cls) { p.status = text; p.statusCls = cls; if (isActive(p)) setStatus(text, cls); refreshTab(tabById(p.tabId)); }
  // The bearer goes in the FIRST frame, never the URL (URLs leak into logs + history).
  const wsUrl = () => (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + BASE + "/plugins/terminal/ws";
  const send = (p, obj) => { if (p && p.ws && p.ws.readyState === 1) p.ws.send(JSON.stringify(obj)); };

  // Fit only a VISIBLE pane: a hidden one (another tab, or the whole view backgrounded)
  // measures 0×0 and the fit addon would shrink the shell to one row — reflowing every
  // prompt and garbling any TUI running in it.
  function fit(p) {
    if (!p.el.offsetWidth || !p.el.offsetHeight) return;
    try { p.fit.fit(); } catch (e) {}
    if (p.term.cols !== p.sentCols || p.term.rows !== p.sentRows) {
      p.sentCols = p.term.cols; p.sentRows = p.term.rows;
      send(p, { type: "resize", cols: p.term.cols, rows: p.term.rows });
    }
  }
  function fitTab(t) { if (t) for (const id of panesOf(t.root)) { const p = panes.get(id); if (p) fit(p); } }

  // ── the tab bar ───────────────────────────────────────────────────────────────
  function labelOf(t) {
    const p = panes.get(t.activePane);
    return tabLabel({ customName: t.customName, title: p ? p.title : "", name: t.name });
  }
  function renderTabs() {
    const bar = $("tabs"); bar.innerHTML = "";
    for (const t of tabs) {
      const p = panes.get(t.activePane);
      const n = panesOf(t.root).length;
      const b = document.createElement("div");
      b.className = "tab" + (t.id === activeTabId ? " active" : ""); b.dataset.id = t.id;
      b.setAttribute("role", "tab"); b.setAttribute("aria-selected", t.id === activeTabId);
      b.title = (t.customName ? "" : ((p && p.title) || "")) + (p && p.title && !t.customName ? " — " : "") + "double-click to rename";
      const dot = document.createElement("span"); dot.className = "tdot " + ((p && p.statusCls) || "");
      const lbl = document.createElement("span"); lbl.className = "lbl"; lbl.textContent = labelOf(t);
      b.append(dot, lbl);
      if (n > 1) { const c = document.createElement("span"); c.className = "npanes"; c.textContent = n; c.title = n + " panes"; b.append(c); }
      const x = document.createElement("span"); x.className = "x"; x.textContent = "×"; x.title = "Close tab (ends its shells)";
      b.append(x); bar.appendChild(b);
      b.onclick = (e) => { if (e.target === x) closeTab(t.id); else if (!b.querySelector("input")) switchTab(t.id); };
      b.ondblclick = (e) => { if (e.target !== x) rename(t); };
      b.onauxclick = (e) => { if (e.button === 1) closeTab(t.id); };   // middle-click closes
    }
  }
  function refreshTab(t) {
    if (!t) return;
    const el = $("tabs").querySelector('.tab[data-id="' + t.id + '"]');
    if (!el) return renderTabs();
    const p = panes.get(t.activePane);
    const lbl = el.querySelector(".lbl"); if (lbl) lbl.textContent = labelOf(t);
    const dot = el.querySelector(".tdot"); if (dot) dot.className = "tdot " + ((p && p.statusCls) || "");
    // Titles arrive after boot (the shell sets them) and widen tabs — keep the active one
    // in view, or it slides off the strip's edge and can't be seen or clicked.
    if (t.id === activeTabId && el.scrollIntoView) el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  // Inline rename (a sandboxed iframe cannot rely on a native dialog). Clearing the name
  // hands the label back to the program-set title.
  function rename(t) {
    const el = $("tabs").querySelector('.tab[data-id="' + t.id + '"]'); if (!el) return;
    const lbl = el.querySelector(".lbl");
    const inp = document.createElement("input"); inp.className = "rename"; inp.value = t.customName || labelOf(t);
    lbl.replaceWith(inp); inp.focus(); inp.select();
    let done = false;
    const finish = (commit) => {
      if (done) return; done = true;
      if (commit) { t.customName = inp.value.trim() || null; save(); }
      renderTabs(); const p = activePane(); if (p) p.term.focus();
    };
    inp.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); };
    inp.onblur = () => finish(true);
    inp.onclick = (e) => e.stopPropagation();
  }

  // ── layout rendering (split panes) ────────────────────────────────────────────
  // Rebuild a tab body from its tree. Pane elements are MOVED, never recreated, so the
  // xterms (and their scrollback, renderers, sockets) survive every split/close.
  function renderLayout(t) {
    const build = (node, path) => {
      if (node.pane) { const p = panes.get(node.pane); return p ? p.el : document.createElement("div"); }
      const box = document.createElement("div"); box.className = "split " + node.dir;
      node.children.forEach((child, i) => {
        if (i > 0) {
          const d = document.createElement("div"); d.className = "divider";
          d.setAttribute("role", "separator"); d.setAttribute("aria-orientation", node.dir === "row" ? "vertical" : "horizontal");
          d.addEventListener("mousedown", (e) => startDrag(e, t, path, i - 1, box));
          box.appendChild(d);
        }
        const el = build(child, path.concat(i));
        el.style.flex = node.sizes[i] + " 1 0";
        box.appendChild(el);
      });
      return box;
    };
    for (const id of panesOf(t.root)) { const p = panes.get(id); if (p) p.el.style.flex = ""; }
    const root = build(t.root, []);
    root.style.flex = "1 1 0";
    t.el.replaceChildren(root);
    t.el.classList.toggle("multi", panesOf(t.root).length > 1);
    markActivePane(t);
    requestAnimationFrame(() => fitTab(t));
  }
  function markActivePane(t) {
    for (const id of panesOf(t.root)) { const p = panes.get(id); if (p) p.el.classList.toggle("active", id === t.activePane); }
  }

  // Divider drag: live flex updates on the two neighbours, one tree update + save + refit
  // at the end. A full-window shield keeps the drag from being eaten by a canvas.
  function startDrag(e, t, path, i, box) {
    e.preventDefault();
    const kids = [...box.children].filter((c) => !c.classList.contains("divider"));
    const a = kids[i], b = kids[i + 1]; if (!a || !b) return;
    const row = box.classList.contains("row");
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const start = row ? ra.left : ra.top, span = row ? ra.width + rb.width : ra.height + rb.height;
    const growA = parseFloat(a.style.flex) || 0.5, growB = parseFloat(b.style.flex) || 0.5, pair = growA + growB;
    const shield = document.createElement("div"); shield.className = "drag-shield " + (row ? "row" : "col");
    document.body.appendChild(shield);
    let frac = growA / pair;
    const move = (ev) => {
      frac = Math.max(0.08, Math.min(0.92, ((row ? ev.clientX : ev.clientY) - start) / span));
      a.style.flex = pair * frac + " 1 0"; b.style.flex = pair * (1 - frac) + " 1 0";
    };
    const up = () => {
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); shield.remove();
      t.root = resize(t.root, path, i, frac); save(); fitTab(t);
      const p = activePane(); if (p) p.term.focus();
    };
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  }

  // ── focus: tabs and panes ─────────────────────────────────────────────────────
  function switchTab(id) {
    const t = tabById(id); if (!t) return;
    activeTabId = id;
    for (const x of tabs) x.el.classList.toggle("active", x.id === id);
    // Toggle, don't rebuild: re-rendering the tab bar between the two clicks of a
    // double-click would swap the node out from under it and the rename would never fire.
    const tabEls = $("tabs").querySelectorAll(".tab");
    if (tabEls.length !== tabs.length) renderTabs();
    else tabEls.forEach((el) => { const on = el.dataset.id === id; el.classList.toggle("active", on); el.setAttribute("aria-selected", on); });
    const el = $("tabs").querySelector('.tab[data-id="' + id + '"]');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", inline: "nearest" });
    focusPane(t.activePane);
  }
  function focusPane(id) {
    const p = panes.get(id); if (!p) return;
    const t = tabById(p.tabId); if (!t) return;
    if (t.id !== activeTabId) { t.activePane = id; return switchTab(t.id); }
    t.activePane = id;
    markActivePane(t);
    send(p, { type: "focus" });   // "active" for the agent's terminal_read
    closeFind();
    fitTab(t); p.term.focus();
    setMeta(p.meta); setStatus(p.status || "…", p.statusCls);
    refreshTab(t);
    save();
  }
  function cycleTab(delta) {
    if (tabs.length < 2) return;
    const i = tabs.findIndex((t) => t.id === activeTabId);
    switchTab(tabs[(i + delta + tabs.length) % tabs.length].id);
  }
  function cyclePane(delta) {
    const t = activeTab(); if (!t) return;
    focusPane(neighbor(t.root, t.activePane, delta));
  }

  // ── connection ────────────────────────────────────────────────────────────────
  // A fresh single-use WS ticket from the GATED ticket route. kit.apiFetch awaits the
  // console's bearer handshake and resolves the slug-aware base, so this is authenticated
  // by the host — and, through the fleet hub, re-authenticated with the fleet token the
  // member expects (the in-band operator token alone never matches a member's bearer).
  // null ⇒ couldn't mint (older plugin, network): the auth frame falls back to the token.
  // "denied" ⇒ the host refused us (401/403).
  async function fetchTicket() {
    try {
      const r = await kit.apiFetch("/api/plugins/terminal/ticket", { method: "POST" });
      if (r.status === 401 || r.status === 403) return "denied";
      if (!r.ok) return null;
      const j = await r.json();
      return (j && typeof j.ticket === "string" && j.ticket) || null;
    } catch (e) { return null; }
  }

  async function connect(p) {
    clearTimeout(p.retryTimer);
    const gen = (p.gen = (p.gen || 0) + 1);   // a newer connect() supersedes this one
    p.detached = false; p.fatal = ""; p.sentCols = 0; p.sentRows = 0;
    setS(p, p.retry ? "reconnecting…" : "connecting…", "");
    // EVERY connect (first open, reconnect, reattach) mints its own ticket — they're single-use.
    const ticket = await fetchTicket();
    if (gen !== p.gen || p.closing) return;
    if (ticket === "denied") return setS(p, "unauthorized", "bad");
    const ws = new WebSocket(wsUrl());
    p.ws = ws;
    ws.onopen = () => {
      const hello = { type: "auth", session: p.sessionId || null, cols: p.term.cols, rows: p.term.rows, cwd_from: p.cwdFrom || null };
      if (ticket) hello.ticket = ticket;
      else hello.token = (kit.getToken && kit.getToken()) || "";   // direct-connection fallback
      ws.send(JSON.stringify(hello));
      p.cwdFrom = null;   // only a brand-new shell starts "where the other pane is"
    };
    ws.onmessage = (e) => {
      let m; try { m = JSON.parse(e.data); } catch (_) { return; }
      if (m.type === "data") p.term.write(m.data);
      else if (m.type === "connected") {
        const lost = p.sessionId && !m.resumed;   // we asked for a shell that is gone
        if (m.resumed || lost) p.term.reset();   // a resume's replay repaints from scratch
        if (lost) p.term.write(dim("[the previous shell ended — this is a new one]"));
        p.sessionId = m.session; p.retry = 0; p.exited = false;
        const t = tabById(p.tabId);
        if (t && m.name && !t.customName && panesOf(t.root).length === 1) t.customName = m.name;   // "Agent"
        p.origin = m.origin || p.origin;
        save();
        if (isActive(p)) send(p, { type: "focus" });
        p.meta = (m.shell || "") + "  " + (m.cwd || "");
        if (isActive(p)) setMeta(p.meta);
        setS(p, m.resumed ? "reattached" : "connected", "ok");
        fit(p);
      }
      else if (m.type === "exit") {
        p.exited = true; p.sessionId = null; save();
        p.term.write(dim("[process exited" + (m.exitCode != null ? " (" + m.exitCode + ")" : "") + (m.error ? ": " + m.error : "") + " — press any key for a new shell]"));
      }
      else if (m.type === "detached") { p.detached = true; p.term.write(dim("[opened in another window — press any key to take it back]")); }
      else if (m.type === "error") { p.fatal = m.message || "error"; p.term.write("\r\n\x1b[31m" + p.fatal + "\x1b[0m\r\n"); }
    };
    ws.onclose = (e) => {
      if (p.ws !== ws || p.closing) return;   // superseded, or the pane was closed
      if (p.exited) return setS(p, "exited", "bad");
      if (p.detached) return setS(p, "detached", "bad");
      if (e.code === 4001) return setS(p, "unauthorized", "bad");
      if (p.fatal) return setS(p, "error", "bad");
      // An unexpected drop: the shell is still alive server-side — reattach with backoff.
      const delay = Math.min(10000, 500 * Math.pow(2, p.retry++));
      setS(p, "reconnecting…", "");
      p.retryTimer = setTimeout(() => connect(p), delay);
    };
  }

  // ── renderer ──────────────────────────────────────────────────────────────────
  // WebGL first (fastest; draws block/box glyphs as exact cell shapes with customGlyphs),
  // canvas when WebGL is unavailable or its context is lost (browsers cap live contexts —
  // many panes can hit it), and xterm's DOM renderer as the last resort.
  function attachRenderer(term) {
    const canvas = () => { try { term.loadAddon(new CanvasAddon()); } catch (e) {} };
    if (WebglAddon) {
      try {
        const gl = new WebglAddon();
        gl.onContextLoss(() => { try { gl.dispose(); } catch (e) {} canvas(); });
        term.loadAddon(gl);
        return;
      } catch (e) { /* no WebGL here */ }
    }
    canvas();
  }

  // ── panes ─────────────────────────────────────────────────────────────────────
  function newPane(tabId, { session = "", cwdFrom = null } = {}) {
    const id = "p" + (++paneSeq);
    const el = document.createElement("div"); el.className = "pane"; el.dataset.id = id;
    const host = document.createElement("div"); host.className = "xhost"; el.appendChild(host);
    const term = new Terminal({
      cursorBlink: true, cursorStyle: CFG.cursorStyle, fontSize, scrollback: CFG.scrollback,
      fontFamily: MONO, lineHeight: 1.0, customGlyphs: true, allowProposedApi: true,
      macOptionIsMeta: CFG.optionAsMeta, macOptionClickForcesSelection: true,
      rightClickSelectsWord: false, theme,
    });
    const fitA = new FitAddon(); term.loadAddon(fitA);
    // Links open in a new browser tab (the view's sandbox allows popups).
    term.loadAddon(new WebLinksAddon((ev, uri) => { ev.preventDefault(); window.open(uri, "_blank", "noopener"); }));
    if (Unicode11Addon) { try { term.loadAddon(new Unicode11Addon()); term.unicode.activeVersion = "11"; } catch (e) {} }
    const search = SearchAddon ? new SearchAddon() : null;
    if (search) term.loadAddon(search);
    // xterm needs a laid-out host to measure; open into the (possibly detached) element and
    // let the first renderLayout + fit size it.
    $("staging").appendChild(el);
    term.open(host);
    attachRenderer(term);

    const p = { id, tabId, sessionId: session || null, cwdFrom, origin: "operator", title: "", term, fit: fitA, search,
                ws: null, el, status: "connecting…", statusCls: "", exited: false, detached: false, closing: false,
                retry: 0, retryTimer: 0, fatal: "", meta: "", sentCols: 0, sentRows: 0 };

    term.onData((d) => {
      if (p.exited) { p.exited = false; p.term.reset(); connect(p); return; }   // any key → a new shell
      if (p.detached) { connect(p); return; }                                  // any key → take it back
      send(p, { type: "input", data: d });
    });
    term.onBinary((d) => send(p, { type: "input", data: d }));
    // Programs name themselves (OSC 0/2: "vim foo.py", "~/dev") — the tab follows its
    // active pane's title unless the operator renamed it.
    term.onTitleChange((title) => { p.title = title; const t = tabById(p.tabId); if (t && t.activePane === p.id && !t.customName) refreshTab(t); });
    term.onSelectionChange(() => { if (CFG.copyOnSelect && term.hasSelection()) writeClipboard(term.getSelection()); });
    term.attachCustomKeyEventHandler((e) => handleKey(p, e));
    // Clicking into a pane makes it the active one (capture: before xterm eats the event).
    el.addEventListener("mousedown", () => { if (!isActive(p)) focusPane(p.id); }, true);
    el.addEventListener("contextmenu", (e) => { e.preventDefault(); openMenu(p, e); });
    if (search && search.onDidChangeResults) {
      search.onDidChangeResults(({ resultIndex, resultCount }) => {
        if (!isActive(p)) return;
        $("findn").textContent = resultCount ? (resultIndex >= 0 ? resultIndex + 1 : "?") + "/" + resultCount + (resultCount >= 1000 ? "+" : "") : "0";
      });
    }
    panes.set(id, p);
    connect(p);
    return p;
  }

  function disposePane(p) {
    p.closing = true; clearTimeout(p.retryTimer);
    send(p, { type: "close" });   // ends the shell (a bare disconnect would only detach)
    try { if (p.ws) p.ws.close(); } catch (e) {}
    try { p.term.dispose(); } catch (e) {}
    p.el.remove(); panes.delete(p.id);
  }

  // ── tabs ──────────────────────────────────────────────────────────────────────
  function newTab({ name, customName = null, layout = null, activeIndex = 0, focus = true } = {}) {
    const id = "t" + (++tabSeq);
    const el = document.createElement("div"); el.className = "tabbody"; el.dataset.id = id; $("terms").appendChild(el);
    const t = { id, name: name || "Terminal " + tabSeq, customName: customName || null, root: null, activePane: null, el };
    tabs.push(t);
    // The saved tree carries session ids; swap each for a live pane attached to it.
    t.root = mapPanes(layout || leaf(""), (sid) => newPane(id, { session: sid }).id) || leaf(newPane(id).id);
    const ids = panesOf(t.root);
    t.activePane = ids[Math.min(Math.max(0, activeIndex), ids.length - 1)];
    renderLayout(t);
    renderTabs();
    if (focus || tabs.length === 1) switchTab(id); else save();
    return t;
  }

  function closeTab(id) {
    const i = tabs.findIndex((t) => t.id === id); if (i < 0) return;
    const t = tabs[i];
    for (const pid of panesOf(t.root)) { const p = panes.get(pid); if (p) disposePane(p); }
    t.el.remove(); tabs.splice(i, 1);
    if (activeTabId === id) {
      const next = tabs[Math.min(i, tabs.length - 1)];
      if (next) switchTab(next.id); else newTab();   // always keep at least one terminal
    } else { renderTabs(); save(); }
  }

  function splitPane(dir) {
    const t = activeTab(), from = activePane(); if (!t || !from) return;
    // The new shell starts where the pane it split from is (the server resolves its cwd).
    const fresh = newPane(t.id, { cwdFrom: from.sessionId });
    t.root = split(t.root, from.id, fresh.id, dir);
    t.activePane = fresh.id;
    renderLayout(t); renderTabs();
    focusPane(fresh.id);
  }

  function closePane(id) {
    const p = panes.get(id); if (!p) return;
    const t = tabById(p.tabId);
    if (!t || panesOf(t.root).length === 1) return closeTab(p.tabId);   // the last pane closes the tab
    const next = neighbor(t.root, id, -1);
    disposePane(p);
    t.root = remove(t.root, id);
    t.activePane = next;
    renderLayout(t); renderTabs();
    focusPane(next);
  }

  // ── clipboard ─────────────────────────────────────────────────────────────────
  // The console grants the frame clipboard-read/-write (PluginView `allow`).
  function writeClipboard(text) {
    if (!text) return;
    try { navigator.clipboard.writeText(text).catch(() => fallbackCopy(text)); } catch (e) { fallbackCopy(text); }
  }
  function fallbackCopy(text) {
    const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); } catch (e) {} ta.remove();
  }
  async function paste(p) {
    try { const text = await navigator.clipboard.readText(); if (text) p.term.paste(text); } catch (e) { /* no read permission */ }
    p.term.focus();
  }

  // ── font zoom (per agent, persisted) ──────────────────────────────────────────
  function setFont(n) {
    fontSize = clampFont(n, CFG.fontSize);
    lsSet(ZOOM, fontSize === CFG.fontSize ? null : String(fontSize));
    for (const p of panes.values()) { p.term.options.fontSize = fontSize; }
    fitTab(activeTab());
  }

  // ── actions (keys + context menu share these) ─────────────────────────────────
  function run(p, action, arg) {
    switch (action) {
      case "copy": if (p.term.hasSelection()) { writeClipboard(p.term.getSelection()); p.term.clearSelection(); } return;
      case "paste": return void paste(p);
      case "selectAll": return p.term.selectAll();
      case "clear": p.term.clearSelection(); p.term.clear(); return p.term.focus();
      case "find": return openFind();
      case "findNext": return findStep(1);
      case "findPrev": return findStep(-1);
      case "newTab": return void newTab();
      case "closeTab": return closePane(p.id);           // ⌘W: the pane; the tab when it's the last
      case "closeWholeTab": return closeTab(p.tabId);
      case "renameTab": return rename(tabById(p.tabId));
      case "nextTab": return cycleTab(1);
      case "prevTab": return cycleTab(-1);
      case "selectTab": { const pick = arg >= 8 ? tabs[tabs.length - 1] : tabs[arg]; if (pick) switchTab(pick.id); return; }
      case "splitRight": return splitPane("row");
      case "splitDown": return splitPane("col");
      case "focusNextPane": return cyclePane(1);
      case "focusPrevPane": return cyclePane(-1);
      case "zoomIn": return setFont(fontSize + 1);
      case "zoomOut": return setFont(fontSize - 1);
      case "zoomReset": return setFont(CFG.fontSize);
    }
  }

  // xterm asks this for every key event; false = xterm ignores it. We act on keydown only
  // but swallow every phase of a chord we own, so no stray keypress reaches the shell.
  function handleKey(p, e) {
    if (e.isComposing) return true;
    const hit = keyAction(e, IS_MAC);
    if (!hit) return true;
    if (hit.forward) {
      if (e.type === "keydown") post({ type: "protoagent:keydown", combo: hit.forward, editable: true });
      return false;
    }
    // ⌘C with nothing selected isn't a copy — on mac ⌘ never reaches the shell anyway, but
    // off-mac Ctrl+Shift+C with no selection should also do nothing rather than send ^C.
    if (hit.action === "copy" && !p.term.hasSelection()) return false;
    // Native paste on mac: let the browser fire its paste event into xterm's textarea
    // (works without clipboard-read permission, and keeps bracketed paste intact).
    if (hit.action === "paste" && IS_MAC) return false;
    if (e.type === "keydown") { e.preventDefault(); run(p, hit.action, hit.arg); }
    return false;
  }

  // ── find bar (searches the active pane) ───────────────────────────────────────
  const DECOR = () => ({
    matchBackground: theme.selectionInactiveBackground, matchBorder: theme.brightBlack,
    matchOverviewRuler: theme.yellow, activeMatchBackground: theme.selectionBackground,
    activeMatchBorder: theme.cursor, activeMatchColorOverviewRuler: theme.cursor,
  });
  const findOpts = () => ({ caseSensitive: $("findCase").classList.contains("on"), regex: $("findRe").classList.contains("on"), decorations: DECOR(), incremental: false });
  function openFind() {
    const p = activePane(); if (!p || !p.search) return;
    $("find").hidden = false;
    const sel = p.term.hasSelection() ? p.term.getSelection() : "";
    if (sel && !sel.includes("\n")) $("findq").value = sel;
    $("findq").focus(); $("findq").select();
    if ($("findq").value) findStep(1);
  }
  function closeFind() {
    if ($("find").hidden) return;
    $("find").hidden = true;
    for (const p of panes.values()) { try { p.search && p.search.clearDecorations(); } catch (e) {} }
    const p = activePane(); if (p) p.term.focus();
  }
  function findStep(dir) {
    const p = activePane(); const q = $("findq").value;
    if (!p || !p.search) return;
    if (!q) { p.search.clearDecorations(); $("findn").textContent = ""; return; }
    let found = false;
    try { found = dir < 0 ? p.search.findPrevious(q, findOpts()) : p.search.findNext(q, findOpts()); } catch (e) { found = false; }
    $("find").classList.toggle("miss", !found);
  }
  $("findq").addEventListener("input", () => {
    const p = activePane(); const q = $("findq").value;
    if (!p || !p.search) return;
    if (!q) { p.search.clearDecorations(); $("findn").textContent = ""; $("find").classList.remove("miss"); return; }
    let found = false;
    try { found = p.search.findNext(q, { ...findOpts(), incremental: true }); } catch (e) {}
    $("find").classList.toggle("miss", !found);
  });
  $("findq").addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); findStep(e.shiftKey ? -1 : 1); }
    else if (e.key === "Escape") { e.preventDefault(); closeFind(); }
    else if ((IS_MAC ? e.metaKey : e.ctrlKey) && e.key.toLowerCase() === "g") { e.preventDefault(); findStep(e.shiftKey ? -1 : 1); }
    else if ((IS_MAC ? e.metaKey : e.ctrlKey && e.shiftKey) && e.key.toLowerCase() === "f") { e.preventDefault(); $("findq").select(); }
  });
  $("findPrev").onclick = () => findStep(-1);
  $("findNext").onclick = () => findStep(1);
  $("findClose").onclick = () => closeFind();
  for (const id of ["findCase", "findRe"]) {
    $(id).onclick = () => { $(id).classList.toggle("on"); $(id).setAttribute("aria-pressed", $(id).classList.contains("on")); findStep(1); };
  }

  // ── context menu (rendered by the console, ADR 0036) ──────────────────────────
  let menuFor = null;
  function openMenu(p, e) {
    if (!isActive(p)) focusPane(p.id);
    menuFor = p;
    const t = tabById(p.tabId);
    const multi = !!t && panesOf(t.root).length > 1;
    const k = IS_MAC ? { c: "⌘C", v: "⌘V", a: "⌘A", k: "⌘K", f: "⌘F", t: "⌘T", w: "⌘W", r: "⌘D", d: "⌘⇧D" }
                     : { c: "Ctrl+Shift+C", v: "Ctrl+Shift+V", a: "Ctrl+Shift+A", k: "", f: "Ctrl+Shift+F", t: "Ctrl+Shift+T", w: "Ctrl+Shift+W", r: "Ctrl+Shift+E", d: "Ctrl+Shift+O" };
    const sc = (label, key) => label + (key ? "  " + key : "");
    const items = [
      { id: "copy", label: sc("Copy", k.c), disabled: !p.term.hasSelection() },
      { id: "paste", label: sc("Paste", k.v) },
      { id: "selectAll", label: sc("Select all", k.a) },
      { divider: true },
      { id: "find", label: sc("Find…", k.f), disabled: !p.search },
      { id: "clear", label: sc("Clear", k.k) },
      { divider: true },
      { id: "splitRight", label: sc("Split right", k.r) },
      { id: "splitDown", label: sc("Split down", k.d) },
      ...(multi ? [{ id: "closeTab", label: sc("Close pane", k.w), danger: true }] : []),
      { divider: true },
      { id: "newTab", label: sc("New tab", k.t) },
      { id: "renameTab", label: "Rename tab" },
      { id: multi ? "closeWholeTab" : "closeTab", label: multi ? "Close tab" : sc("Close tab", k.w), danger: true },
    ];
    post({ type: "protoagent:contextmenu:open", x: e.clientX, y: e.clientY, items });
  }

  // ── bus + host messages ───────────────────────────────────────────────────────
  // Adopt a server-side session as a tab (once). `focus` brings it to the front.
  function adopt({ session, name, focus }) {
    if (!session) return;
    for (const p of panes.values()) {
      if (p.sessionId === session) { if (focus) focusPane(p.id); return; }
    }
    newTab({ name: name || undefined, customName: name || null, layout: leaf(session), focus: !!focus || !tabs.length });
  }
  window.addEventListener("message", (e) => {
    const m = e.data || {};
    if (m.type === "protoagent:contextmenu:action" && menuFor) {
      const p = panes.get(menuFor.id) || activePane();
      if (p) run(p, String(m.itemId || "").split(".").pop());
    } else if (m.type === "protoagent:event" && m.topic === "terminal.session_opened" && m.data) {
      adopt(m.data);
    }
  });
  // Tabs opened while no view was loaded (or an event we missed): the gated session list.
  async function adoptPending() {
    if (!kit.apiFetch) return;
    try {
      const r = await kit.apiFetch("/api/plugins/terminal/sessions");
      if (!r.ok) return;
      const { sessions: list } = await r.json();
      for (const x of list || []) if (x.pending_adopt) adopt({ session: x.id, name: x.name, focus: false });
    } catch (e) { /* offline / no host — nothing to adopt */ }
  }

  // Stay mounted while another console view is showing (bridge `background: true`) —
  // otherwise the console unmounts this iframe and every terminal drops. Also hear this
  // plugin's bus topics (terminal.session_opened: a tab the agent opened).
  const stayMounted = () => post({ type: "protoagent:subscribe", patterns: ["terminal.#"], background: true });

  // ── wiring ────────────────────────────────────────────────────────────────────
  $("newtab").onclick = () => newTab();
  new ResizeObserver(() => fitTab(activeTab())).observe($("terms"));
  setInterval(() => { for (const p of panes.values()) send(p, { type: "ping" }); }, 30000);
  // A view coming back from the background: refit + refocus the visible terminal.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    fitTab(activeTab()); const p = activePane(); if (p) p.term.focus();
    if (booted) adoptPending();   // belt-and-braces for a session_opened event we missed
  });
  window.addEventListener("focus", () => { const p = activePane(); if (p && $("find").hidden) p.term.focus(); });

  // Boot once: restore the saved tabs + layouts (reattaching their shells) or open a fresh one.
  async function start() {
    if (booted) return; booted = true;
    applyTheme(); stayMounted();
    const saved = load();
    for (const s of saved) newTab({ ...s, focus: false });
    if (tabs.length) switchTab((tabs[saved.active] || tabs[0]).id);
    await adoptPending();
    if (!tabs.length) newTab();
  }
  kit.initPluginView(() => {
    applyTheme(); stayMounted(); start();
    // The handshake can land after the boot fallback already tried with no bearer — retry
    // any pane that was turned away now that a token is here.
    for (const p of panes.values()) if (p.status === "unauthorized") { p.retry = 0; connect(p); }
    if (booted) adoptPending();   // the first try may have run before the bearer arrived
  });
  setTimeout(start, 1000);
}
