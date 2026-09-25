// Pure logic for the terminal view — no DOM, no xterm — so it is unit-tested under
// `node --test` (tests/js/). terminal.js wires these into the page.

// ── keys ────────────────────────────────────────────────────────────────────────
// A keyboard event → a normalized combo in the console's format (apps/web keybindings/
// combo.ts): "mod" is the platform primary (⌘ on mac, Ctrl elsewhere), the other platform
// modifier is kept as "ctrl" (mac) / "meta" (else). "" for a bare modifier press.
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead"]);

export function comboOf(e, isMac) {
  if (MODIFIERS.has(e.key)) return "";
  const parts = [];
  if (isMac ? e.metaKey : e.ctrlKey) parts.push("mod");
  if (isMac ? e.ctrlKey : e.metaKey) parts.push(isMac ? "ctrl" : "meta");
  if (e.altKey) parts.push("alt");
  if (e.shiftKey) parts.push("shift");
  parts.push(e.key === " " ? "space" : String(e.key).toLowerCase());
  return parts.join("+");
}

// Terminal chords handled INSIDE the frame. They mirror the console's chat conventions
// (⌘T new tab, ⌘K clear, ⌘1-9 / Ctrl+Tab switch) — those are chat-scoped in the host, so
// they can't collide. Off-mac, Ctrl+<letter> belongs to the shell, so the terminal's own
// chords take Ctrl+Shift (the Linux-terminal convention).
const MAC_KEYS = {
  "mod+c": "copy",
  "mod+v": "paste",
  "mod+a": "selectAll",
  "mod+k": "clear",
  "mod+f": "find",
  "mod+g": "findNext",
  "mod+shift+g": "findPrev",
  "mod+t": "newTab",
  "mod+w": "closeTab",
  "mod+shift+]": "nextTab",
  "mod+shift+[": "prevTab",
  "mod+shift+}": "nextTab", // shift+] reports "}" on US layouts
  "mod+shift+{": "prevTab",
  "mod+=": "zoomIn",
  "mod++": "zoomIn",
  "mod+shift+=": "zoomIn",
  "mod+shift++": "zoomIn",
  "mod+-": "zoomOut",
  "mod+0": "zoomReset",
};
const OTHER_KEYS = {
  "mod+shift+c": "copy",
  "mod+shift+v": "paste",
  "mod+shift+a": "selectAll",
  "mod+shift+f": "find",
  "mod+shift+g": "findNext",
  "mod+shift+t": "newTab",
  "mod+shift+w": "closeTab",
  "mod+pagedown": "nextTab",
  "mod+pageup": "prevTab",
  "mod+=": "zoomIn",
  "mod++": "zoomIn",
  "mod+-": "zoomOut",
  "mod+0": "zoomReset",
};
const SHARED_KEYS = { "ctrl+tab": "nextTab", "ctrl+shift+tab": "prevTab" };

/** What a keydown means to the terminal:
 *   { action, arg? }   — a terminal chord, handled in the frame
 *   { forward: combo } — not ours, but a console chord: hand it to the host
 *   null               — ordinary input: let xterm send it to the shell
 * `ctrl+tab` is written in the host's format, where on mac "ctrl" is the literal key. */
export function keyAction(e, isMac) {
  const combo = comboOf(e, isMac);
  if (!combo) return null;
  const table = isMac ? MAC_KEYS : OTHER_KEYS;
  // "ctrl+tab" in the tables means the physical Ctrl key: on mac that's the "ctrl" segment,
  // elsewhere Ctrl is "mod".
  const physical = isMac ? combo : combo.replace(/^mod\+/, "ctrl+");
  if (SHARED_KEYS[physical]) return { action: SHARED_KEYS[physical] };
  if (table[combo]) return { action: table[combo] };
  // ⌘1-9 / Ctrl+1-9 pick a tab. (Not Alt+digit off-mac: that's readline's digit-argument.)
  const tab = /^mod\+([1-9])$/.exec(combo);
  if (tab) return { action: "selectTab", arg: Number(tab[1]) - 1 };
  // Console chords. On mac every ⌘ combo is fair game (the shell never sees ⌘). Elsewhere
  // only Ctrl+Shift+… and Ctrl+, — plain Ctrl+<key> is the shell's (Ctrl-C, Ctrl-R, …).
  if (isMac ? combo.startsWith("mod+") : combo.startsWith("mod+shift+") || combo === "mod+,") {
    return { forward: combo };
  }
  return null;
}

// ── colour ──────────────────────────────────────────────────────────────────────
// Colours arrive as [r, g, b] (0-255) — terminal.js resolves the --pl-* tokens through a
// canvas pixel, which handles every CSS colour syntax (hex, rgb, oklch, color-mix…).
export const hex = (c) => "#" + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
export const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);

export function luminance([r, g, b]) {
  const lin = (v) => {
    v /= 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

// Nudge `c` toward the foreground until it reads against `bg` (≥ min contrast) — status
// tokens tuned for badges can be too dim as text on the terminal background.
export function legible(c, bg, fg, min = 3) {
  let out = c;
  for (let t = 0.1; contrast(out, bg) < min && t <= 1; t += 0.1) out = mix(c, fg, t);
  return out;
}

/** The xterm theme from the console's tokens. Every ANSI colour derives from a token, so a
 *  theme family (light or dark) carries all 16 — nothing is a hardcoded hex. `tok` holds
 *  [r,g,b] for: bg, fg, fgMuted, fgSubtle, bgRaised, accent, error, success, warning, info.
 *  Cyan has no token of its own: it's the midpoint of info (blue) and success (green). */
export function buildTheme(tok) {
  const { bg, fg } = tok;
  const dark = luminance(bg) < 0.5;
  // Brights move AWAY from the background: lighter on dark themes, deeper on light ones.
  const brighten = (c) => mix(c, dark ? [255, 255, 255] : [0, 0, 0], 0.28);
  const base = {
    red: tok.error,
    green: tok.success,
    yellow: tok.warning,
    blue: tok.info,
    magenta: tok.accent,
    cyan: mix(tok.info, tok.success, 0.5),
  };
  const theme = {
    background: hex(bg),
    foreground: hex(fg),
    cursor: hex(tok.accent),
    cursorAccent: hex(bg),
    selectionBackground: hex(mix(bg, tok.accent, 0.35)),
    selectionInactiveBackground: hex(mix(bg, tok.accent, 0.18)),
    black: hex(dark ? tok.bgRaised : tok.fg),
    white: hex(dark ? tok.fgMuted : tok.bgRaised),
    brightBlack: hex(tok.fgSubtle),
    brightWhite: hex(dark ? fg : mix(fg, [0, 0, 0], 0.3)),
  };
  for (const [name, c] of Object.entries(base)) {
    const color = legible(c, bg, fg);
    theme[name] = hex(color);
    theme["bright" + name[0].toUpperCase() + name.slice(1)] = hex(legible(brighten(color), bg, fg));
  }
  return theme;
}

// ── tabs ────────────────────────────────────────────────────────────────────────
/** A tab's label: the operator's name wins; else the title the shell/program set (OSC 0/2
 *  — "vim foo.py", "~/dev"); else the default "Terminal N". Shells usually title
 *  themselves "user@host: /long/cwd" — the user@host part is the same on every tab, so
 *  it's dropped, and a long title keeps its END (the cwd's leaf) rather than its start. */
export function tabLabel({ customName, title, name }, max = 28) {
  if (customName) return clip(customName.trim(), max, false);
  const t = String(title || "").trim().replace(/^[^\s@:]+@[^\s:]+:\s*/, "");
  if (t) return clip(t, max, true);
  return clip(String(name || "").trim(), max, false);
}
const clip = (s, max, keepEnd) =>
  s.length <= max ? s : keepEnd ? "…" + s.slice(s.length - (max - 1)) : s.slice(0, max - 1) + "…";

/** Clamp a font size to the range Settings allows. */
export const clampFont = (n, fallback = 13) => {
  const v = Math.round(Number(n));
  return Number.isFinite(v) ? Math.max(8, Math.min(32, v)) : fallback;
};
