// Unit tests for web/layout.js (split-pane trees).
import { test } from "node:test";
import assert from "node:assert/strict";
import { leaf, panesOf, split, remove, neighbor, resize, mapPanes, validate } from "../../web/layout.js";

const close = (a, b) => Math.abs(a - b) < 1e-9;

test("split a single pane right, then down inside the right half", () => {
  let t = leaf("a");
  t = split(t, "a", "b", "row");
  assert.deepEqual(t, { dir: "row", children: [leaf("a"), leaf("b")], sizes: [0.5, 0.5] });
  t = split(t, "b", "c", "col");
  assert.deepEqual(panesOf(t), ["a", "b", "c"]);
  assert.equal(t.children[1].dir, "col");
});

test("splitting along the parent's direction adds a sibling, not a nested split", () => {
  let t = split(leaf("a"), "a", "b", "row");
  t = split(t, "b", "c", "row");
  assert.equal(t.dir, "row");
  assert.deepEqual(panesOf(t), ["a", "b", "c"]);
  assert.ok(close(t.sizes[0], 0.5) && close(t.sizes[1], 0.25) && close(t.sizes[2], 0.25));
});

test("split never mutates the input tree", () => {
  const t = split(leaf("a"), "a", "b", "row");
  const snapshot = JSON.stringify(t);
  split(t, "a", "c", "col");
  remove(t, "a");
  assert.equal(JSON.stringify(t), snapshot);
});

test("split of an unknown pane is a no-op", () => {
  const t = split(leaf("a"), "a", "b", "row");
  assert.equal(split(t, "zzz", "c", "row"), t);
});

test("remove collapses a split left with one child and renormalizes sizes", () => {
  let t = split(split(leaf("a"), "a", "b", "row"), "b", "c", "row"); // a | b | c
  t = remove(t, "b");
  assert.deepEqual(panesOf(t), ["a", "c"]);
  assert.ok(close(t.sizes.reduce((x, y) => x + y, 0), 1));
  t = remove(t, "c");
  assert.deepEqual(t, leaf("a"));
  assert.equal(remove(t, "a"), null);
});

test("remove inside a nested split collapses only that level", () => {
  let t = split(leaf("a"), "a", "b", "row");
  t = split(t, "b", "c", "col"); // a | (b / c)
  t = remove(t, "c");
  assert.deepEqual(t, { dir: "row", children: [leaf("a"), leaf("b")], sizes: [0.5, 0.5] });
});

test("neighbor walks reading order and wraps", () => {
  const t = split(split(leaf("a"), "a", "b", "row"), "b", "c", "col");
  assert.equal(neighbor(t, "a", 1), "b");
  assert.equal(neighbor(t, "c", 1), "a");
  assert.equal(neighbor(t, "a", -1), "c");
  assert.equal(neighbor(leaf("a"), "a", 1), "a");
});

test("resize moves one divider, clamped to the minimum", () => {
  let t = split(leaf("a"), "a", "b", "row");
  t = resize(t, [], 0, 0.7);
  assert.ok(close(t.sizes[0], 0.7) && close(t.sizes[1], 0.3));
  t = resize(t, [], 0, 0.99);
  assert.ok(close(t.sizes[1], 0.08));
  // nested: resize the col split inside the right child
  let n = split(split(leaf("a"), "a", "b", "row"), "b", "c", "col");
  n = resize(n, [1], 0, 0.25);
  assert.ok(close(n.children[1].sizes[0], 0.25));
});

test("mapPanes swaps ids for persistence and repairs dropped panes", () => {
  const t = split(split(leaf("p1"), "p1", "p2", "row"), "p2", "p3", "col");
  const saved = mapPanes(t, (id) => ({ p1: "s1", p2: "s2", p3: null })[id]);
  assert.deepEqual(saved, { dir: "row", children: [leaf("s1"), leaf("s2")], sizes: [0.5, 0.5] });
  assert.equal(mapPanes(leaf("x"), () => null), null);
});

test("validate accepts good trees and rejects junk from storage", () => {
  const good = { dir: "col", children: [{ pane: "s1" }, { pane: "s2" }], sizes: [3, 1] };
  const v = validate(good);
  assert.ok(close(v.sizes[0], 0.75));
  assert.deepEqual(validate({ pane: "s1" }), leaf("s1"));
  for (const bad of [null, 42, {}, { dir: "diag", children: [{ pane: 1 }, { pane: 2 }] }, { dir: "row", children: [{ pane: "x" }] }, { pane: {} }]) {
    assert.equal(validate(bad), null, JSON.stringify(bad));
  }
  // bad sizes are replaced by an even split
  assert.deepEqual(validate({ dir: "row", children: [{ pane: "a" }, { pane: "b" }], sizes: [0, "x"] }).sizes, [0.5, 0.5]);
});
