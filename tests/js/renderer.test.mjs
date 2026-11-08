// The deferred WebGL upgrade (web/logic.js createUpgradeQueue). Stress test of protoAgent
// v0.231.0: the view's first mount froze the console ~670 ms, because WebGL's context +
// shader init ran synchronously inside the mount. The queue must never upgrade inside the
// call that queued the pane, must do ONE pane per scheduled turn, and must wait for a pane
// to be visible (a hidden tab's panes don't pay WebGL until shown).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createUpgradeQueue } from "../../web/logic.js";

function harness({ visible = () => true } = {}) {
  const turns = [], done = [];
  const q = createUpgradeQueue({
    schedule: (fn) => turns.push(fn),
    ready: (p) => visible(p),
    alive: (p) => !p.closing,
    upgrade: (p) => done.push(p.id),
  });
  const step = () => { const fn = turns.shift(); if (fn) fn(); };
  return { q, turns, done, step };
}

test("adding a pane never upgrades it synchronously", () => {
  const h = harness();
  h.q.add({ id: "a" });
  assert.deepEqual(h.done, []);
  assert.equal(h.turns.length, 1);
  h.step();
  assert.deepEqual(h.done, ["a"]);
});

test("one pane per scheduled turn, in order, with one turn pending at a time", () => {
  const h = harness();
  for (const id of ["a", "b", "c"]) h.q.add({ id });
  assert.equal(h.turns.length, 1);   // not one turn per add
  h.step(); assert.deepEqual(h.done, ["a"]);
  h.step(); assert.deepEqual(h.done, ["a", "b"]);
  h.step(); assert.deepEqual(h.done, ["a", "b", "c"]);
  assert.equal(h.turns.length, 0);
  assert.equal(h.q.size, 0);
});

test("a hidden pane waits; kick() resumes it once it is shown", () => {
  const shown = new Set(["a"]);
  const h = harness({ visible: (p) => shown.has(p.id) });
  const b = { id: "b" };
  h.q.add({ id: "a" }); h.q.add(b);
  h.step(); assert.deepEqual(h.done, ["a"]);
  assert.equal(h.turns.length, 0);    // nothing visible left → no idle spinning
  h.q.kick(); assert.equal(h.turns.length, 0);
  shown.add("b"); h.q.kick();
  assert.equal(h.turns.length, 1);
  h.step(); assert.deepEqual(h.done, ["a", "b"]);
});

test("a pane closed before its turn is dropped, not upgraded", () => {
  const h = harness();
  const a = { id: "a" }, b = { id: "b" };
  h.q.add(a); h.q.add(b);
  a.closing = true;
  h.step(); h.step();
  assert.deepEqual(h.done, ["b"]);
  assert.equal(h.q.size, 0);
});

test("an upgrade that throws still lets the rest proceed", () => {
  const turns = [], done = [];
  const q = createUpgradeQueue({
    schedule: (fn) => turns.push(fn), ready: () => true,
    upgrade: (p) => { if (p.id === "a") throw new Error("no webgl"); done.push(p.id); },
  });
  q.add({ id: "a" }); q.add({ id: "b" });
  assert.throws(() => turns.shift()());
  turns.shift()();
  assert.deepEqual(done, ["b"]);
});
