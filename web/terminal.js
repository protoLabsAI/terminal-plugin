// The terminal view app. Loaded by the page's bootstrap (view.py) AFTER the DS kit and the
// vendored xterm bundles; `boot(ctx)` receives { kit, BASE, CFG, xt } where xt holds the
// xterm constructors. Pure logic (keys, theme maths, labels) lives in logic.js.
//
// Sessions: each tab owns an xterm attached over its own WebSocket to a server-side shell
// by id. Shells OUTLIVE the socket (sessions.py): the view stays mounted while hidden,
// remembers tabs across reloads, and reattaches (replaying missed output) after a drop.
// Only the tab's × ends the shell.

import { keyAction, buildTheme, tabLabel, clampFont } from "./logic.js";

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
    for (const s of sessions.values()) { try { s.term.options.theme = theme; } catch (e) {} }
  }
  // Live re-theme: the kit re-sets the --pl-* vars on :root → rebuild every tab's theme.
  new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["style", "class", "data-theme"] });

  // The canvas/WebGL renderers build a font string that can't resolve var() — resolve the
  // mono stack concretely. An explicit font_family setting wins.
  const monoVar = getComputedStyle(document.documentElement).getPropertyValue("--pl-font-mono").replace(/['"]/g, "").trim();
  const MONO = CFG.fontFamily || ((monoVar ? monoVar + ", " : "") + "Menlo, Monaco, 'Courier New', monospace");

  // ── per-agent persisted UI state (tabs + zoom) ────────────────────────────────
  const STORE = "protoagent.terminal.tabs:" + (BASE || "/");
  const ZOOM = "protoagent.terminal.fontSize:" + (BASE || "/");
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} };
  let fontSize = clampFont(lsGet(ZOOM) || CFG.fontSize, CFG.fontSize);

  const sessions = new Map();   // id → session state (see newSession)
  let activeId = null;
  let counter = 0;

  function save() {
    const all = [...sessions.values()];
    lsSet(STORE, JSON.stringify({
      active: all.findIndex((s) => s.id === activeId),
      tabs: all.map((s) => ({ name: s.name, customName: s.customName || null, session: s.sessionId || null })),
    }));
  }
  function load() { try { return JSON.parse(lsGet(STORE) || "null"); } catch (e) { return null; } }

  // ── status + socket plumbing ──────────────────────────────────────────────────
  // The shell/cwd readout truncates from the LEFT (direction: rtl) so the cwd's tail stays
  // visible; LRM marks keep the path's own punctuation ("/bin/zsh") from being reordered.
  function setMeta(text) { $("shell").textContent = text ? "\u200E" + text + "\u200E" : ""; $("shell").title = text || ""; }
  function setStatus(text, cls) { $("status").textContent = text; $("dot").className = "dot" + (cls ? " " + cls : ""); }
  function setS(s, text, cls) { s.status = text; s.statusCls = cls; if (s.id === activeId) setStatus(text, cls); }
  // The bearer goes in the FIRST frame, never the URL (URLs leak into logs + history).
  const wsUrl = () => (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + BASE + "/plugins/terminal/ws";
  const send = (s, obj) => { if (s.ws && s.ws.readyState === 1) s.ws.send(JSON.stringify(obj)); };

  // Fit only a VISIBLE pane: a hidden one (another tab, or the whole view backgrounded)
  // measures 0×0 and the fit addon would shrink the shell to one row — reflowing every
  // prompt and garbling any TUI running in it.
  function fit(s) {
    if (!s.el.offsetWidth || !s.el.offsetHeight) return;
    try { s.fit.fit(); } catch (e) {}
    if (s.term.cols !== s.sentCols || s.term.rows !== s.sentRows) {
      s.sentCols = s.term.cols; s.sentRows = s.term.rows;
      send(s, { type: "resize", cols: s.term.cols, rows: s.term.rows });
    }
  }

  // ── tabs ──────────────────────────────────────────────────────────────────────
  function renderTabs() {
    const tabs = $("tabs"); tabs.innerHTML = "";
    for (const s of sessions.values()) {
      const b = document.createElement("div");
      b.className = "tab" + (s.id === activeId ? " active" : ""); b.dataset.id = s.id;
      b.title = (s.customName ? "" : (s.title || "")) + (s.title && !s.customName ? " — " : "") + "double-click to rename";
      const dot = document.createElement("span"); dot.className = "tdot " + (s.statusCls || "");
      const lbl = document.createElement("span"); lbl.className = "lbl"; lbl.textContent = tabLabel(s);
      const x = document.createElement("span"); x.className = "x"; x.textContent = "×"; x.title = "Close (ends the shell)";
      b.append(dot, lbl, x); tabs.appendChild(b);
      b.onclick = (e) => { if (e.target === x) closeSession(s.id); else if (!b.querySelector("input")) switchTo(s.id); };
      b.ondblclick = (e) => { if (e.target !== x) rename(s); };
      b.onauxclick = (e) => { if (e.button === 1) closeSession(s.id); };   // middle-click closes
    }
  }
  function refreshTab(s) {
    const el = $("tabs").querySelector('.tab[data-id="' + s.id + '"]');
    if (!el) return renderTabs();
    const lbl = el.querySelector(".lbl"); if (lbl) lbl.textContent = tabLabel(s);
    const dot = el.querySelector(".tdot"); if (dot) dot.className = "tdot " + (s.statusCls || "");
  }

  // Inline rename (a sandboxed iframe cannot rely on a native dialog). Clearing the name
  // hands the label back to the program-set title.
  function rename(s) {
    const el = $("tabs").querySelector('.tab[data-id="' + s.id + '"]'); if (!el) return;
    const lbl = el.querySelector(".lbl");
    const inp = document.createElement("input"); inp.className = "rename"; inp.value = s.customName || tabLabel(s);
    lbl.replaceWith(inp); inp.focus(); inp.select();
    let done = false;
    const finish = (commit) => {
      if (done) return; done = true;
      if (commit) { s.customName = inp.value.trim() || null; save(); }
      renderTabs(); s.term.focus();
    };
    inp.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); };
    inp.onblur = () => finish(true);
    inp.onclick = (e) => e.stopPropagation();
  }

  function switchTo(id) {
    activeId = id;
    for (const s of sessions.values()) s.el.classList.toggle("active", s.id === id);
    // Toggle, don't rebuild: re-rendering the tab bar between the two clicks of a
    // double-click would swap the node out from under it and the rename would never fire.
    const tabEls = $("tabs").querySelectorAll(".tab");
    if (tabEls.length !== sessions.size) renderTabs();
    else tabEls.forEach((el) => el.classList.toggle("active", el.dataset.id === id));
    save();
    const s = sessions.get(id);
    if (s) {
      send(s, { type: "focus" });   // "active" for the agent's terminal_read
      closeFind();
      fit(s); s.term.focus();
      setMeta(s.meta); setStatus(s.status || "…", s.statusCls);
      const el = $("tabs").querySelector('.tab[data-id="' + id + '"]');
      if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }
  const ordered = () => [...sessions.keys()];
  function cycle(delta) {
    const ids = ordered(); if (ids.length < 2) return;
    switchTo(ids[(ids.indexOf(activeId) + delta + ids.length) % ids.length]);
  }

  // ── connection ────────────────────────────────────────────────────────────────
  function connect(s) {
    clearTimeout(s.retryTimer);
    const ws = new WebSocket(wsUrl());
    s.ws = ws; s.detached = false; s.fatal = ""; s.sentCols = 0; s.sentRows = 0;
    setS(s, s.retry ? "reconnecting…" : "connecting…", "");
    ws.onopen = () => {
      const tok = (kit.getToken && kit.getToken()) || "";
      ws.send(JSON.stringify({ type: "auth", token: tok, session: s.sessionId || null, cols: s.term.cols, rows: s.term.rows }));
    };
    ws.onmessage = (e) => {
      let m; try { m = JSON.parse(e.data); } catch (_) { return; }
      if (m.type === "data") s.term.write(m.data);
      else if (m.type === "connected") {
        const lost = s.sessionId && !m.resumed;   // we asked for a shell that is gone
        if (m.resumed || lost) s.term.reset();   // a resume's replay repaints from scratch
        if (lost) s.term.write(dim("[the previous shell ended — this is a new one]"));
        s.sessionId = m.session; s.retry = 0; s.exited = false;
        if (m.name && !s.customName) s.customName = m.name;   // a tab the agent named ("Agent")
        s.origin = m.origin || s.origin;
        save();
        if (s.id === activeId) send(s, { type: "focus" });
        s.meta = (m.shell || "") + "  " + (m.cwd || "");
        if (s.id === activeId) setMeta(s.meta);
        setS(s, m.resumed ? "reattached" : "connected", "ok"); refreshTab(s);
        fit(s);
      }
      else if (m.type === "exit") {
        s.exited = true; s.sessionId = null; save();
        s.term.write(dim("[process exited" + (m.exitCode != null ? " (" + m.exitCode + ")" : "") + (m.error ? ": " + m.error : "") + " — press any key for a new shell]"));
      }
      else if (m.type === "detached") { s.detached = true; s.term.write(dim("[opened in another window — press any key to take it back]")); }
      else if (m.type === "error") { s.fatal = m.message || "error"; s.term.write("\r\n\x1b[31m" + s.fatal + "\x1b[0m\r\n"); }
    };
    ws.onclose = (e) => {
      if (s.ws !== ws || s.closing) return;   // superseded, or the tab was closed
      if (s.exited) { setS(s, "exited", "bad"); return refreshTab(s); }
      if (s.detached) { setS(s, "detached", "bad"); return refreshTab(s); }
      if (e.code === 4001) { setS(s, "unauthorized", "bad"); return refreshTab(s); }
      if (s.fatal) { setS(s, "error", "bad"); return refreshTab(s); }
      // An unexpected drop: the shell is still alive server-side — reattach with backoff.
      const delay = Math.min(10000, 500 * Math.pow(2, s.retry++));
      setS(s, "reconnecting…", ""); refreshTab(s);
      s.retryTimer = setTimeout(() => connect(s), delay);
    };
  }

  // ── renderer ──────────────────────────────────────────────────────────────────
  // WebGL first (fastest; draws block/box glyphs as exact cell shapes with customGlyphs),
  // canvas when WebGL is unavailable or its context is lost (browsers cap live contexts),
  // and xterm's DOM renderer as the last resort.
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

  // ── a session (tab) ───────────────────────────────────────────────────────────
  function newSession(saved) {
    const id = "t" + (++counter);
    const el = document.createElement("div"); el.className = "termpane"; el.dataset.id = id; $("terms").appendChild(el);
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
    term.open(el);
    attachRenderer(term);

    const s = { id, name: (saved && saved.name) || "Terminal " + counter, customName: (saved && saved.customName) || null,
                title: "", sessionId: (saved && saved.session) || null, origin: "operator", term, fit: fitA, search, ws: null, el,
                status: "connecting…", statusCls: "", exited: false, detached: false, closing: false,
                retry: 0, retryTimer: 0, fatal: "", meta: "", sentCols: 0, sentRows: 0 };

    term.onData((d) => {
      if (s.exited) { s.exited = false; s.term.reset(); connect(s); return; }   // any key → a new shell
      if (s.detached) { connect(s); return; }                                  // any key → take it back
      send(s, { type: "input", data: d });
    });
    term.onBinary((d) => send(s, { type: "input", data: d }));
    // Programs name themselves (OSC 0/2: "vim foo.py", "~/dev") — the tab follows unless
    // the operator renamed it.
    term.onTitleChange((t) => { s.title = t; if (!s.customName) refreshTab(s); });
    term.onSelectionChange(() => {
      if (CFG.copyOnSelect && term.hasSelection()) writeClipboard(term.getSelection());
    });
    term.attachCustomKeyEventHandler((e) => handleKey(s, e));
    el.addEventListener("contextmenu", (e) => { e.preventDefault(); openMenu(s, e); });

    sessions.set(id, s);
    watchResults(s);
    connect(s);
    if (!saved) switchTo(id);
    return s;
  }

  function closeSession(id) {
    const s = sessions.get(id); if (!s) return;
    s.closing = true; clearTimeout(s.retryTimer);
    send(s, { type: "close" });   // ends the shell (a bare disconnect would only detach)
    try { if (s.ws) s.ws.close(); } catch (e) {}
    try { s.term.dispose(); } catch (e) {}
    s.el.remove(); sessions.delete(id);
    if (activeId === id) {
      const next = sessions.keys().next().value;
      if (next) switchTo(next); else newSession();  // always keep at least one terminal
    } else { renderTabs(); save(); }
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
  async function paste(s) {
    try { const t = await navigator.clipboard.readText(); if (t) s.term.paste(t); } catch (e) { /* no read permission */ }
    s.term.focus();
  }

  // ── font zoom (per agent, persisted) ──────────────────────────────────────────
  function setFont(n) {
    fontSize = clampFont(n, CFG.fontSize);
    lsSet(ZOOM, fontSize === CFG.fontSize ? null : String(fontSize));
    for (const s of sessions.values()) { s.term.options.fontSize = fontSize; }
    const a = sessions.get(activeId); if (a) fit(a);
  }

  // ── actions (keys + context menu share these) ─────────────────────────────────
  function run(s, action, arg) {
    switch (action) {
      case "copy": if (s.term.hasSelection()) { writeClipboard(s.term.getSelection()); s.term.clearSelection(); } return;
      case "paste": return void paste(s);
      case "selectAll": return s.term.selectAll();
      case "clear": s.term.clearSelection(); s.term.clear(); return s.term.focus();
      case "find": return openFind();
      case "findNext": return findStep(1);
      case "findPrev": return findStep(-1);
      case "newTab": return void newSession();
      case "closeTab": return closeSession(s.id);
      case "renameTab": return rename(s);
      case "nextTab": return cycle(1);
      case "prevTab": return cycle(-1);
      case "selectTab": { const ids = ordered(); const pick = arg >= 8 ? ids[ids.length - 1] : ids[arg]; if (pick) switchTo(pick); return; }
      case "zoomIn": return setFont(fontSize + 1);
      case "zoomOut": return setFont(fontSize - 1);
      case "zoomReset": return setFont(CFG.fontSize);
    }
  }

  // xterm asks this for every key event; false = xterm ignores it. We act on keydown only
  // but swallow every phase of a chord we own, so no stray keypress reaches the shell.
  function handleKey(s, e) {
    if (e.isComposing) return true;
    const hit = keyAction(e, IS_MAC);
    if (!hit) return true;
    if (hit.forward) {
      if (e.type === "keydown") post({ type: "protoagent:keydown", combo: hit.forward, editable: true });
      return false;
    }
    // ⌘C with nothing selected isn't a copy — on mac ⌘ never reaches the shell anyway, but
    // off-mac Ctrl+Shift+C with no selection should also do nothing rather than send ^C.
    if (hit.action === "copy" && !s.term.hasSelection()) return false;
    // Native paste on mac: let the browser fire its paste event into xterm's textarea
    // (works without clipboard-read permission, and keeps bracketed paste intact).
    if (hit.action === "paste" && IS_MAC) return false;
    if (e.type === "keydown") { e.preventDefault(); run(s, hit.action, hit.arg); }
    return false;
  }

  // ── find bar ──────────────────────────────────────────────────────────────────
  const DECOR = () => ({
    matchBackground: theme.selectionInactiveBackground, matchBorder: theme.brightBlack,
    matchOverviewRuler: theme.yellow, activeMatchBackground: theme.selectionBackground,
    activeMatchBorder: theme.cursor, activeMatchColorOverviewRuler: theme.cursor,
  });
  const findOpts = () => ({ caseSensitive: $("findCase").classList.contains("on"), regex: $("findRe").classList.contains("on"), decorations: DECOR(), incremental: false });
  function openFind() {
    const s = sessions.get(activeId); if (!s || !s.search) return;
    $("find").hidden = false;
    const sel = s.term.hasSelection() ? s.term.getSelection() : "";
    if (sel && !sel.includes("\n")) $("findq").value = sel;
    $("findq").focus(); $("findq").select();
    if ($("findq").value) findStep(1);
  }
  function closeFind() {
    if ($("find").hidden) return;
    $("find").hidden = true;
    for (const s of sessions.values()) { try { s.search && s.search.clearDecorations(); } catch (e) {} }
    const s = sessions.get(activeId); if (s) s.term.focus();
  }
  function findStep(dir) {
    const s = sessions.get(activeId); const q = $("findq").value;
    if (!s || !s.search) return;
    if (!q) { s.search.clearDecorations(); $("findn").textContent = ""; return; }
    let found = false;
    try { found = dir < 0 ? s.search.findPrevious(q, findOpts()) : s.search.findNext(q, findOpts()); } catch (e) { found = false; }
    $("find").classList.toggle("miss", !found);
  }
  $("findq").addEventListener("input", () => {
    const s = sessions.get(activeId); const q = $("findq").value;
    if (!s || !s.search) return;
    if (!q) { s.search.clearDecorations(); $("findn").textContent = ""; $("find").classList.remove("miss"); return; }
    let found = false;
    try { found = s.search.findNext(q, { ...findOpts(), incremental: true }); } catch (e) {}
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

  // Result counts: the addon reports them once decorations are on.
  function watchResults(s) {
    if (!s.search || !s.search.onDidChangeResults) return;
    s.search.onDidChangeResults(({ resultIndex, resultCount }) => {
      if (s.id !== activeId) return;
      $("findn").textContent = resultCount ? (resultIndex >= 0 ? resultIndex + 1 : "?") + "/" + resultCount + (resultCount >= 1000 ? "+" : "") : "0";
    });
  }

  // ── context menu (rendered by the console, ADR 0036) ──────────────────────────
  let menuFor = null;
  function openMenu(s, e) {
    if (s.id !== activeId) switchTo(s.id);
    menuFor = s;
    const k = IS_MAC ? { c: "⌘C", v: "⌘V", a: "⌘A", k: "⌘K", f: "⌘F", t: "⌘T", w: "⌘W" }
                     : { c: "Ctrl+Shift+C", v: "Ctrl+Shift+V", a: "Ctrl+Shift+A", k: "", f: "Ctrl+Shift+F", t: "Ctrl+Shift+T", w: "Ctrl+Shift+W" };
    const items = [
      { id: "copy", label: "Copy" + (k.c ? "  " + k.c : ""), disabled: !s.term.hasSelection() },
      { id: "paste", label: "Paste  " + k.v },
      { id: "selectAll", label: "Select all  " + k.a },
      { divider: true },
      { id: "find", label: "Find…  " + k.f, disabled: !s.search },
      { id: "clear", label: "Clear" + (k.k ? "  " + k.k : "") },
      { divider: true },
      { id: "newTab", label: "New tab  " + k.t },
      { id: "renameTab", label: "Rename tab" },
      { id: "closeTab", label: "Close tab  " + k.w, danger: true },
    ];
    if (inFrame) post({ type: "protoagent:contextmenu:open", x: e.clientX, y: e.clientY, items });
  }
  window.addEventListener("message", (e) => {
    const m = e.data || {};
    if (m.type === "protoagent:contextmenu:action" && menuFor) {
      const s = sessions.get(menuFor.id) || sessions.get(activeId);
      if (s) run(s, String(m.itemId || "").split(".").pop());
    }
  });

  // ── wiring ────────────────────────────────────────────────────────────────────
  $("newtab").onclick = () => newSession();
  new ResizeObserver(() => { const s = sessions.get(activeId); if (s) fit(s); }).observe($("terms"));
  setInterval(() => { for (const s of sessions.values()) send(s, { type: "ping" }); }, 30000);
  // A view coming back from the background: refit + refocus the visible terminal.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      const s = sessions.get(activeId); if (s) { fit(s); s.term.focus(); }
      if (booted) adoptPending();   // belt-and-braces for a session_opened event we missed
    }
  });
  window.addEventListener("focus", () => { const s = sessions.get(activeId); if (s && $("find").hidden) s.term.focus(); });

  // Stay mounted while another console view is showing (bridge `background: true`) —
  // otherwise the console unmounts this iframe and every terminal drops.
  // Also hear this plugin's own bus topics — terminal.session_opened is how a tab the
  // agent opened (terminal_run / terminal_open) appears here without a reload.
  const stayMounted = () => post({ type: "protoagent:subscribe", patterns: ["terminal.#"], background: true });

  // Adopt a server-side session as a tab (once). `focus` brings it to the front.
  function adopt({ session, name, focus }) {
    if (!session) return;
    for (const s of sessions.values()) {
      if (s.sessionId === session) { if (focus) switchTo(s.id); return; }
    }
    const s = newSession({ session, customName: name || null, name: name || undefined });
    if (focus || sessions.size === 1) switchTo(s.id); else renderTabs();
    save();
  }
  window.addEventListener("message", (e) => {
    const m = e.data || {};
    if (m.type === "protoagent:event" && m.topic === "terminal.session_opened" && m.data) adopt(m.data);
  });
  // Tabs opened while no view was loaded: ask the (gated) session list once on boot.
  async function adoptPending() {
    if (!kit.apiFetch) return;
    try {
      const r = await kit.apiFetch("/api/plugins/terminal/sessions");
      if (!r.ok) return;
      const { sessions: list } = await r.json();
      for (const x of list || []) if (x.pending_adopt) adopt({ session: x.id, name: x.name, focus: false });
    } catch (e) { /* offline / no host — nothing to adopt */ }
  }

  // Boot once: restore the saved tabs (reattaching their shells) or open a fresh one.
  let booted = false;
  async function start() {
    if (booted) return; booted = true;
    applyTheme(); stayMounted();
    const saved = load();
    if (saved && Array.isArray(saved.tabs) && saved.tabs.length) {
      for (const t of saved.tabs) newSession(t);
      const ids = ordered();
      switchTo(ids[saved.active] || ids[0]);
    }
    await adoptPending();
    if (!sessions.size) newSession();
  }
  kit.initPluginView(() => {
    applyTheme(); stayMounted(); start();
    // The handshake can land after the boot fallback already tried with no bearer — retry
    // any tab that was turned away now that a token is here.
    for (const s of sessions.values()) if (s.status === "unauthorized") { s.retry = 0; connect(s); }
    if (booted) adoptPending();   // the first try may have run before the bearer arrived
  });
  setTimeout(start, 1000);
}
