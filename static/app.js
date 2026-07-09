const $ = (s) => document.querySelector(s);
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

const dropEl = $("#drop");
const fileInput = $("#file");
const optImg = $("#opt-img");
const optCsv = $("#opt-csv");
const queueEl = $("#queue");
const beatEl = $("#beat");
const downallEl = $("#downall");
const adminBtn = $("#admin");
const modalEl = $("#modal");
const modalFn = $("#modal-fn");
const mdEl = $("#md");

const state = new Map(); // id -> job
let busy = false;
let adminKey = localStorage.getItem("pdf2md-admin-key") || null;
let sse = null;
let pollTimer = null;

// ---- helpers ----

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function apiFetch(url, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (adminKey) headers["X-Admin-Key"] = adminKey;
  return fetch(url, Object.assign({}, opts, { headers }));
}

function stateText(j) {
  if (j.status === "done") return "완료";
  if (j.status === "running") return (j.progress | 0) + "%";
  if (j.status === "failed") return "실패";
  const ahead = j.ahead || 0;
  return ahead === 0 ? "곧 시작" : `앞에 ${ahead}개 대기`;
}

// ---- render ----

function render() {
  const jobs = [...state.values()].sort((a, b) => b.created_at - a.created_at);

  const myRunning = jobs.some((j) => j.status === "running");
  const showBeat = busy && !myRunning;
  beatEl.classList.toggle("show", showBeat);
  beatEl.classList.toggle("hide", !showBeat);

  const myDone = jobs.filter((j) => j.status === "done").length;
  downallEl.classList.toggle("hide", myDone < 2);

  queueEl.innerHTML = jobs
    .map(
      (j, k) => `
      <div class="job ${j.status}" style="animation-delay:${reduce ? 0 : k * 45}ms" data-id="${j.id}">
        <span class="node"></span>
        <div class="job-row">
          <span class="name">${escapeHtml(j.filename)}</span>
          <span class="state">${stateText(j)}</span>
        </div>
        <div class="bar"><i style="width:${j.status === "running" ? (j.progress | 0) : 0}%"></i></div>
        ${j.status === "failed" && j.error ? `<div class="err">${escapeHtml(j.error)}</div>` : ""}
        ${
          j.status === "done"
            ? `<div class="done-meta">
                <span class="chip">표 ${j.n_tables || 0} · 이미지 ${j.n_images || 0}</span>
                <button class="act" data-preview="${j.id}" type="button">미리보기</button>
                <button class="act ghost" data-copy="${j.id}" type="button">마크다운 복사</button>
                <a class="act ghost" href="/api/jobs/${j.id}/download">ZIP 내려받기</a>
              </div>`
            : ""
        }
      </div>`
    )
    .join("");

  queueEl.querySelectorAll("[data-preview]").forEach((b) => {
    b.onclick = () => openPreview(b.dataset.preview);
  });
  queueEl.querySelectorAll("[data-copy]").forEach((b) => {
    b.onclick = () => copyMd(b.dataset.copy, b);
  });
}

// ---- job state sync ----

function applyFull(data) {
  state.clear();
  (data.jobs || []).forEach((j) => state.set(j.id, j));
  busy = !!data.busy;
  render();
}

function applyDelta(data) {
  const settled = [];
  (data.jobs || []).forEach((j) => {
    const prev = state.get(j.id);
    if (prev && prev.status !== "done" && j.status === "done") settled.push(j.id);
    state.set(j.id, j);
  });
  busy = !!data.busy;
  render();
  settled.forEach((id) => {
    const node = queueEl.querySelector(`.job[data-id="${id}"] .node`);
    if (node) {
      node.classList.add("settle");
      setTimeout(() => node.classList.remove("settle"), 320);
    }
  });
}

async function refresh() {
  const res = await apiFetch("/api/jobs");
  applyFull(await res.json());
}

// ---- live updates: SSE normally, poll while admin (EventSource can't carry X-Admin-Key) ----

function connectSSE() {
  if (sse) return;
  sse = new EventSource("/api/events");
  sse.onmessage = (e) => applyDelta(JSON.parse(e.data));
  sse.onerror = () => {
    if (sse) { sse.close(); sse = null; }
    if (!adminKey) setTimeout(connectSSE, 2000);
  };
}
function stopSSE() {
  if (sse) { sse.close(); sse = null; }
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refresh, 2000);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ---- upload ----

$("#pick").onclick = () => fileInput.click();
fileInput.onchange = () => {
  upload([...fileInput.files]);
  fileInput.value = "";
};

["dragover", "dragenter"].forEach((e) =>
  dropEl.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropEl.classList.add("over");
  })
);
["dragleave", "drop"].forEach((e) =>
  dropEl.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropEl.classList.remove("over");
  })
);
dropEl.addEventListener("drop", (ev) => upload([...ev.dataTransfer.files]));

async function upload(files) {
  files = files.filter((f) => f.name.toLowerCase().endsWith(".pdf"));
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  fd.append("include_images", optImg.checked ? "true" : "false");
  fd.append("include_tables_csv", optCsv.checked ? "true" : "false");
  await apiFetch("/api/jobs", { method: "POST", body: fd });
  await refresh();
}

// ---- preview / copy ----

async function openPreview(id) {
  const j = state.get(id);
  modalFn.textContent = j ? j.filename : "";
  mdEl.textContent = "불러오는 중…";
  modalEl.classList.add("open");
  try {
    const md = await (await apiFetch(`/api/jobs/${id}/preview`)).text();
    mdEl.innerHTML = marked.parse(md);
    mdEl.querySelectorAll("table").forEach((t) => {
      if (!t.parentElement.classList.contains("tw")) {
        const wrap = document.createElement("div");
        wrap.className = "tw";
        t.parentNode.insertBefore(wrap, t);
        wrap.appendChild(t);
      }
    });
  } catch (e) {
    mdEl.textContent = "미리보기를 불러오지 못했습니다.";
  }
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (e) {
      /* fall through to legacy path */
    }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand("copy");
  } catch (e) {
    /* ponytail: best-effort clipboard fallback, silent no-op if it fails */
  }
  document.body.removeChild(ta);
}

async function copyMd(id, btn) {
  try {
    const md = await (await apiFetch(`/api/jobs/${id}/preview`)).text();
    await copyText(md);
    const orig = btn.textContent;
    btn.textContent = "복사됨";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = orig;
      btn.classList.remove("copied");
    }, 1400);
  } catch (e) {
    /* silent — preview fetch failed */
  }
}

// ---- modal chrome ----

function closeModal() {
  modalEl.classList.remove("open");
}
$("#close").onclick = closeModal;
modalEl.onclick = (e) => {
  if (e.target === modalEl) closeModal();
};
addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ---- download all ----

downallEl.onclick = () => {
  location.href = "/api/download-all";
};

// ---- admin toggle ----

adminBtn.onclick = () => {
  if (adminKey) {
    adminKey = null;
    localStorage.removeItem("pdf2md-admin-key");
    adminBtn.textContent = "전체 보기";
    stopPolling();
    connectSSE();
  } else {
    const key = prompt("관리자 키를 입력하세요");
    if (!key) return;
    adminKey = key;
    localStorage.setItem("pdf2md-admin-key", key);
    adminBtn.textContent = "내 작업만";
    stopSSE();
    startPolling();
  }
  refresh();
};

// ---- boot ----

if (adminKey) {
  adminBtn.textContent = "내 작업만";
  startPolling();
} else {
  connectSSE();
}
refresh();
