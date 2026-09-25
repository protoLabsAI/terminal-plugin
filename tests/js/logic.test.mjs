// Unit tests for web/logic.js — run with `node --test tests/js/*.test.mjs` (CI does; so does
// tests/test_js.py when node is on PATH).
import { test } from "node:test";
import assert from "node:assert/strict";
import { comboOf, keyAction, buildTheme, contrast, tabLabel, clampFont, hex, mix } from "../../web/logic.js";

const ev = (key, mods = {}) => ({ key, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods });
const MAC = true, LINUX = false;

// ── combos: the console's format ────────────────────────────────────────────────
test("comboOf matches the console's combo format", () => {
  assert.equal(comboOf(ev("k", { metaKey: true, shiftKey: true }), MAC), "mod+shift+k");
  assert.equal(comboOf(ev("k", { ctrlKey: true, shiftKey: true }), LINUX), "mod+shift+k");
  assert.equal(comboOf(ev("Tab", { ctrlKey: true }), MAC), "ctrl+tab");
  assert.equal(comboOf(ev(" ", { metaKey: true }), MAC), "mod+space");
  assert.equal(comboOf(ev("Meta", { metaKey: true }), MAC), "");   // bare modifier
});

// ── keys: mac ───────────────────────────────────────────────────────────────────
test("mac: terminal chords are handled in the frame", () => {
  const act = (key, mods) => (keyAction(ev(key, mods), MAC) || {}).action;
  assert.equal(act("c", { metaKey: true }), "copy");
  assert.equal(act("v", { metaKey: true }), "paste");
  assert.equal(act("k", { metaKey: true }), "clear");
  assert.equal(act("f", { metaKey: true }), "find");
  assert.equal(act("t", { metaKey: true }), "newTab");
  assert.equal(act("w", { metaKey: true }), "closeTab");
  assert.equal(act("}", { metaKey: true, shiftKey: true }), "nextTab");
  assert.equal(act("Tab", { ctrlKey: true }), "nextTab");
  assert.equal(act("Tab", { ctrlKey: true, shiftKey: true }), "prevTab");
  assert.equal(act("=", { metaKey: true }), "zoomIn");
  assert.equal(act("0", { metaKey: true }), "zoomReset");
  assert.deepEqual(keyAction(ev("3", { metaKey: true }), MAC), { action: "selectTab", arg: 2 });
});

test("mac: every other ⌘ chord goes to the console; Ctrl keys stay with the shell", () => {
  assert.deepEqual(keyAction(ev("K", { metaKey: true, shiftKey: true }), MAC), { forward: "mod+shift+k" }); // palette
  assert.deepEqual(keyAction(ev(",", { metaKey: true }), MAC), { forward: "mod+," });                    // settings
  assert.equal(keyAction(ev("c", { ctrlKey: true }), MAC), null);   // ^C → the shell
  assert.equal(keyAction(ev("r", { ctrlKey: true }), MAC), null);   // ^R → the shell
  assert.equal(keyAction(ev("a"), MAC), null);
  assert.equal(keyAction(ev("b", { altKey: true }), MAC), null);    // Option+B → xterm (meta or ∫)
});

// ── keys: linux/windows ─────────────────────────────────────────────────────────
test("linux: Ctrl+<letter> belongs to the shell — terminal chords use Ctrl+Shift", () => {
  for (const k of ["c", "v", "k", "t", "w", "f", "r", "d", "l", "z"]) {
    assert.equal(keyAction(ev(k, { ctrlKey: true }), LINUX), null, "ctrl+" + k);
  }
  const act = (key, mods) => (keyAction(ev(key, mods), LINUX) || {}).action;
  assert.equal(act("C", { ctrlKey: true, shiftKey: true }), "copy");
  assert.equal(act("V", { ctrlKey: true, shiftKey: true }), "paste");
  assert.equal(act("F", { ctrlKey: true, shiftKey: true }), "find");
  assert.equal(act("T", { ctrlKey: true, shiftKey: true }), "newTab");
  assert.equal(act("Tab", { ctrlKey: true }), "nextTab");
  assert.equal(act("PageDown", { ctrlKey: true }), "nextTab");
  assert.deepEqual(keyAction(ev("2", { ctrlKey: true }), LINUX), { action: "selectTab", arg: 1 });
  assert.equal(keyAction(ev("2", { altKey: true }), LINUX), null);   // readline digit-argument
});

test("split panes: ⌘D / ⌘⇧D (mac), Ctrl+Shift+E / O (else); pane focus", () => {
  const act = (mac, key, mods) => (keyAction(ev(key, mods), mac) || {}).action;
  assert.equal(act(MAC, "d", { metaKey: true }), "splitRight");
  assert.equal(act(MAC, "D", { metaKey: true, shiftKey: true }), "splitDown");
  assert.equal(act(MAC, "]", { metaKey: true }), "focusNextPane");
  assert.equal(act(MAC, "ArrowLeft", { metaKey: true, altKey: true }), "focusPrevPane");
  assert.equal(act(LINUX, "E", { ctrlKey: true, shiftKey: true }), "splitRight");
  assert.equal(act(LINUX, "O", { ctrlKey: true, shiftKey: true }), "splitDown");
  assert.equal(keyAction(ev("d", { ctrlKey: true }), LINUX), null);   // ^D (EOF) stays the shell's
});

test("linux: only Ctrl+Shift chords and Ctrl+, are forwarded to the console", () => {
  assert.deepEqual(keyAction(ev("K", { ctrlKey: true, shiftKey: true }), LINUX), { forward: "mod+shift+k" });
  assert.deepEqual(keyAction(ev(",", { ctrlKey: true }), LINUX), { forward: "mod+," });
});

// ── theme: every colour from tokens, legible on light AND dark ──────────────────
const DARK = {
  bg: [10, 10, 12], fg: [237, 237, 237], fgMuted: [154, 154, 165], fgSubtle: [107, 107, 118], bgRaised: [26, 26, 31],
  accent: [155, 135, 242], error: [248, 113, 113], success: [74, 222, 128], warning: [251, 191, 36], info: [96, 165, 250],
};
const LIGHT = {
  bg: [255, 255, 255], fg: [20, 20, 24], fgMuted: [90, 90, 100], fgSubtle: [140, 140, 150], bgRaised: [240, 240, 244],
  accent: [110, 86, 207], error: [220, 38, 38], success: [134, 239, 172], warning: [253, 224, 71], info: [37, 99, 235],
};
const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const ANSI = ["red", "green", "yellow", "blue", "magenta", "cyan"];

for (const [name, tok] of [["dark", DARK], ["light", LIGHT]]) {
  test(`${name} theme: 16 ANSI colours, all hex, all legible on the background`, () => {
    const t = buildTheme(tok);
    for (const c of ["black", "white", "brightBlack", "brightWhite", ...ANSI, ...ANSI.map((a) => "bright" + a[0].toUpperCase() + a.slice(1))]) {
      assert.match(t[c], /^#[0-9a-f]{6}$/, c);
    }
    for (const c of [...ANSI, ...ANSI.map((a) => "bright" + a[0].toUpperCase() + a.slice(1))]) {
      assert.ok(contrast(rgb(t[c]), tok.bg) >= 2.95, `${c} ${t[c]} contrast ${contrast(rgb(t[c]), tok.bg).toFixed(2)}`);
    }
    assert.equal(t.background, hex(tok.bg));
    assert.equal(t.foreground, hex(tok.fg));
  });
}

test("light theme: pale status tokens are deepened until they read as text", () => {
  // LIGHT.success / .warning are badge-pale; raw they'd vanish on white
  assert.ok(contrast(LIGHT.warning, LIGHT.bg) < 2);
  assert.ok(contrast(rgb(buildTheme(LIGHT).yellow), LIGHT.bg) >= 2.95);
});

test("cyan derives from info + success (no token of its own)", () => {
  assert.equal(buildTheme(DARK).cyan, hex(mix(DARK.info, DARK.success, 0.5)));
});

// ── labels + font ───────────────────────────────────────────────────────────────
test("tabLabel: operator name > program title > default, truncated", () => {
  assert.equal(tabLabel({ customName: "builds", title: "vim", name: "Terminal 1" }), "builds");
  assert.equal(tabLabel({ customName: null, title: "vim foo.py", name: "Terminal 1" }), "vim foo.py");
  assert.equal(tabLabel({ customName: null, title: "", name: "Terminal 1" }), "Terminal 1");
  assert.equal(tabLabel({ customName: "x".repeat(40) }, 10), "xxxxxxxxx…");
  // shell titles: drop the user@host prefix every tab shares; keep the cwd's END
  assert.equal(tabLabel({ title: "kj@KJs-MacBook-Pro:~/dev/protoAgent" }), "~/dev/protoAgent");
  assert.equal(tabLabel({ title: "kj@host: /private/tmp/a/very/long/path/to/scratchpad" }, 16), "…path/to/scratchpad".slice(-16).replace(/^./, "…"));
  assert.equal(tabLabel({ title: "vim foo.py" }), "vim foo.py");
});

test("clampFont keeps the Settings range", () => {
  assert.equal(clampFont(99), 32);
  assert.equal(clampFont(2), 8);
  assert.equal(clampFont("14"), 14);
  assert.equal(clampFont("junk", 13), 13);
});
