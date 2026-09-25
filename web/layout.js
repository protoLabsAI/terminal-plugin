// Split-pane layout trees — pure (no DOM), unit-tested under node --test.
//
// A tab's layout is a tree:
//   leaf  = { pane: "<paneId>" }
//   split = { dir: "row" | "col", children: [node, …], sizes: [fraction, …] }
// "row" lays children left→right (a vertical divider — ⌘D "split right"); "col" stacks
// them top→bottom (⌘⇧D "split down"). `sizes` are fractions summing to 1. Every operation
// returns a NEW tree (the old one is never mutated).

export const leaf = (pane) => ({ pane });

/** The pane ids in reading order (left→right, top→bottom). */
export function panesOf(node) {
  if (!node) return [];
  if (node.pane) return [node.pane];
  return node.children.flatMap(panesOf);
}

const normalize = (sizes) => {
  const total = sizes.reduce((a, b) => a + b, 0) || 1;
  return sizes.map((s) => s / total);
};

/** Split `target`'s pane in `dir`, putting `fresh` after it. Splitting in the direction of
 *  the parent split adds a sibling (sharing the target's space) instead of nesting — so
 *  three ⌘D presses give three columns, not a staircase. */
export function split(node, target, fresh, dir) {
  const out = splitIn(node, target, fresh, dir);
  return out || node;
}

function splitIn(node, target, fresh, dir) {
  if (node.pane) {
    if (node.pane !== target) return null;
    return { dir, children: [leaf(target), leaf(fresh)], sizes: [0.5, 0.5] };
  }
  for (let i = 0; i < node.children.length; i++) {
    const child = node.children[i];
    if (child.pane === target && node.dir === dir) {
      const children = node.children.slice();
      const sizes = node.sizes.slice();
      const half = sizes[i] / 2;
      children.splice(i + 1, 0, leaf(fresh));
      sizes.splice(i, 1, half, half);
      return { ...node, children, sizes };
    }
    const got = splitIn(child, target, fresh, dir);
    if (got) {
      const children = node.children.slice();
      children[i] = got;
      return { ...node, children };
    }
  }
  return null;
}

/** Remove `target`'s pane. A split left with one child collapses into that child; the
 *  freed space goes to the neighbours proportionally. Returns null when the tree empties. */
export function remove(node, target) {
  if (!node) return null;
  if (node.pane) return node.pane === target ? null : node;
  const children = [];
  const sizes = [];
  node.children.forEach((c, i) => {
    const next = remove(c, target);
    if (next) {
      children.push(next);
      sizes.push(node.sizes[i]);
    }
  });
  if (!children.length) return null;
  if (children.length === 1) return children[0];
  return { ...node, children, sizes: normalize(sizes) };
}

/** The pane `delta` steps from `target` in reading order, wrapping. */
export function neighbor(node, target, delta) {
  const ids = panesOf(node);
  const i = ids.indexOf(target);
  if (i < 0 || ids.length < 2) return target;
  return ids[(i + delta + ids.length) % ids.length];
}

/** Move the divider between children `i` and `i+1` of the split at `path` (child indexes
 *  from the root) so child `i` gets `frac` of the pair's combined share. Each side keeps at
 *  least `min` of the pair. */
export function resize(node, path, i, frac, min = 0.08) {
  if (!path.length) {
    if (node.pane || i < 0 || i + 1 >= node.children.length) return node;
    const pair = node.sizes[i] + node.sizes[i + 1];
    const f = Math.max(min, Math.min(1 - min, frac));
    const sizes = node.sizes.slice();
    sizes[i] = pair * f;
    sizes[i + 1] = pair * (1 - f);
    return { ...node, sizes };
  }
  const [head, ...rest] = path;
  if (node.pane || !node.children[head]) return node;
  const children = node.children.slice();
  children[head] = resize(children[head], rest, i, frac, min);
  return { ...node, children };
}

/** Swap pane ids for something persistable (a session id) and back. `map` returns the
 *  new value for a pane id, or null to drop that pane (the tree is repaired). */
export function mapPanes(node, map) {
  if (!node) return null;
  if (node.pane != null) {
    const v = map(node.pane);
    return v == null ? null : leaf(v);
  }
  let out = { ...node, children: node.children.map((c) => mapPanes(c, map)) };
  const keep = out.children.map((c, i) => [c, out.sizes[i]]).filter(([c]) => c);
  if (!keep.length) return null;
  if (keep.length === 1) return keep[0][0];
  out = { ...out, children: keep.map(([c]) => c), sizes: normalize(keep.map(([, s]) => s)) };
  return out;
}

/** A tree from untrusted storage, or null when it isn't a valid one. */
export function validate(node, depth = 0) {
  if (!node || typeof node !== "object" || depth > 8) return null;
  if ("pane" in node) return node.pane == null || typeof node.pane === "object" ? null : leaf(node.pane);
  if ((node.dir !== "row" && node.dir !== "col") || !Array.isArray(node.children) || node.children.length < 2) return null;
  const children = node.children.map((c) => validate(c, depth + 1));
  if (children.some((c) => !c)) return null;
  const sizes = Array.isArray(node.sizes) && node.sizes.length === children.length && node.sizes.every((s) => s > 0)
    ? normalize(node.sizes) : children.map(() => 1 / children.length);
  return { dir: node.dir, children, sizes };
}
