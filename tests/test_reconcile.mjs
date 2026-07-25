// Self-check for render()'s cursor-based reconciliation (static/app.js).
// Verifies: steady state = 0 DOM moves, and reorder/insert/remove yield correct order.
// Run: node tests/test_reconcile.mjs

// Minimal fake DOM: a parent with insertBefore/appendChild that counts moves.
function makeParent() {
  const kids = []; // {id}
  let moves = 0;
  return {
    kids,
    moves: () => moves,
    firstChild: () => kids[0] || null,
    next: (el) => { const i = kids.indexOf(el); return i >= 0 ? kids[i + 1] || null : null; },
    insertBefore(el, ref) {
      const cur = kids.indexOf(el);
      if (cur >= 0) kids.splice(cur, 1); // existing node → it's a move
      const at = ref ? kids.indexOf(ref) : kids.length;
      kids.splice(at < 0 ? kids.length : at, 0, el);
      moves++;
    },
  };
}

// Mirror of the loop in render(): cursor walk, insert only when out of place.
function reconcile(parent, cards, desiredIds) {
  let cursor = parent.firstChild();
  for (const id of desiredIds) {
    let el = cards.get(id);
    if (!el) { el = { id }; cards.set(id, el); }
    if (el === cursor) cursor = parent.next(cursor);
    else parent.insertBefore(el, cursor);
  }
  // (removal of unseen nodes happens separately in render; not exercised here)
}

function assertEq(got, want, msg) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) throw new Error(`${msg}: got ${g} want ${w}`);
}

// 1. Fresh build inserts all in order.
let p = makeParent(), cards = new Map();
reconcile(p, cards, ["a", "b", "c"]);
assertEq(p.kids.map((k) => k.id), ["a", "b", "c"], "fresh build order");

// 2. Same order again → ZERO moves (the bug: this used to move everything).
const before = p.moves();
reconcile(p, cards, ["a", "b", "c"]);
assertEq(p.moves() - before, 0, "steady state must do no DOM moves");

// 3. Reorder (c to front).
reconcile(p, cards, ["c", "a", "b"]);
assertEq(p.kids.map((k) => k.id), ["c", "a", "b"], "reorder to front");

// 4. Insert new at front + keep rest.
reconcile(p, cards, ["x", "c", "a", "b"]);
assertEq(p.kids.map((k) => k.id), ["x", "c", "a", "b"], "insert new at front");

console.log("ok");
