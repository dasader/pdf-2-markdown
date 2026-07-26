// Self-check for the queue render path in static/app.js. Everything here mirrors
// that file's logic — keep them in step.
//   1-4  cursor reconciliation: steady state = 0 DOM moves, correct order on churn
//   5-6  patchCard field writes: steady state = 0 DOM writes
//   7-8  live-update mode: SSE and polling never run together
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

// ---- patchCard field writes ----
// render() patches EVERY card on every tick, not just changed ones. Admin mode
// polls a full snapshot of ~200 jobs, so unconditional writes meant ~600 DOM
// writes every tick — className (style recalc) and textContent (text node
// replaced even when identical) repainted the whole queue. That is the flicker.

// Minimal fake card that counts writes; getters are free.
function makeCard() {
  let writes = 0;
  const prop = (init) => { let v = init; return { get: () => v, set: (x) => { v = x; writes++; } }; };
  const cls = prop(""), txt = prop(""), wid = prop("");
  return {
    writes: () => writes,
    get className() { return cls.get(); }, set className(v) { cls.set(v); },
    state: { get textContent() { return txt.get(); }, set textContent(v) { txt.set(v); } },
    bar: { style: { get width() { return wid.get(); }, set width(v) { wid.set(v); } } },
  };
}

// Mirror of stateText() and patchCard()'s field writes in static/app.js.
function stateText(j) {
  if (j.status === "done") return "완료";
  if (j.status === "running") return (j.progress | 0) + "%";
  if (j.status === "failed") return "실패";
  const ahead = j.ahead || 0;
  return ahead === 0 ? "곧 시작" : `앞에 ${ahead}개 대기`;
}
function patchFields(el, j) {
  const cls = "job " + j.status;
  if (el.className !== cls) el.className = cls;
  const txt = stateText(j);
  if (el.state.textContent !== txt) el.state.textContent = txt;
  const w = (j.status === "running" ? (j.progress | 0) : j.status === "done" ? 100 : 0) + "%";
  if (el.bar.style.width !== w) el.bar.style.width = w;
}

// 5. Steady state: re-patching an unchanged job writes nothing.
const card = makeCard();
const job = { status: "done", n_tables: 1, n_images: 0 };
patchFields(card, job);
const w0 = card.writes();
if (w0 === 0) throw new Error("first patch must write the initial values");
patchFields(card, job);
patchFields(card, job);
assertEq(card.writes() - w0, 0, "steady state must do no DOM writes");

// 6. Each changed field still writes — and only that field.
const q = makeCard(), qj = { status: "queued", ahead: 2 };
patchFields(q, qj);
let w = q.writes();
patchFields(q, { status: "queued", ahead: 1 });      // label only
assertEq(q.writes() - w, 1, "ahead change writes exactly the label");
w = q.writes();
patchFields(q, { status: "running", progress: 40 }); // class + label + bar
assertEq(q.writes() - w, 3, "status change writes class, label and bar");
w = q.writes();
patchFields(q, { status: "running", progress: 41 }); // label + bar (class unchanged)
assertEq(q.writes() - w, 2, "progress change leaves the class alone");

// ---- live-update mode ----
// SSE and polling must never run together. EventSource can't carry X-Admin-Key,
// so a stray SSE stream in admin mode delivers session-scoped deltas at 0.5s on
// top of the 2s full-snapshot poll — double the render rate, wrong scope.

// Mirror of connectSSE/stopSSE/startPolling and the admin toggle in static/app.js.
function makeLive() {
  const s = { sse: null, poll: null, adminKey: null, timer: null };
  s.connectSSE = () => { if (s.sse || s.adminKey) return; s.sse = "open"; };
  s.sseError = () => { s.sse = null; if (!s.adminKey) s.timer = s.connectSSE; };
  s.fireTimer = () => { const t = s.timer; s.timer = null; if (t) t(); };
  s.enterAdmin = () => { s.adminKey = "secret"; s.sse = null; s.poll = "on"; };
  return s;
}

// 7. A reconnect queued before admin mode took over must not open a stream.
const live = makeLive();
live.connectSSE();
live.sseError();     // server ended the stream (it does this every 5 min) → reconnect armed
live.enterAdmin();   // user types the admin key: polling takes over
live.fireTimer();    // the armed reconnect fires
assertEq([live.sse, live.poll], [null, "on"], "admin mode must not also run SSE");

// 8. Non-admin still reconnects.
const live2 = makeLive();
live2.connectSSE();
live2.sseError();
live2.fireTimer();
assertEq([live2.sse, live2.poll], ["open", null], "non-admin must reconnect SSE");

console.log("ok");
