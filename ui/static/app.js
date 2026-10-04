/* AI Worker front end. Plain ES2020, no build step.
 *
 * One EventSource per run; every server event maps to one small render function.
 * The rule throughout: the server's word for a run's status is authoritative, and
 * anything the UI shows about a value is shown with the source that produced it. */

const $ = (id) => document.getElementById(id);

const FAULTS = [
  ["session_expiry", "Session expires mid-run"],
  ["transient_500_on_submit", "AP returns 500 on submit"],
  ["ui_rename", "AP relabels its controls"],
  ["slow_page", "Pages load very slowly"],
  ["flaky_render", "Invoice table renders late"],
  ["silent_save_failure", "Save looks fine, writes nothing"],
];

const state = {
  runId: null,
  source: null,
  questions: new Map(),   // id -> pending question, so a re-render can restore the modal
  startedAt: null,
  tick: null,
  shotTimer: null,
};

// ── helpers ────────────────────────────────────────────────────────────
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const shorten = (s, n = 160) => {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n) + "…" : t;
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* keep status */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, ms = 2600) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

// ── status pill ─────────────────────────────────────────────────────────
const OK = new Set(["verified", "completed"]);
const BAD = new Set(["failed", "error", "budget_exhausted"]);
const WARN = new Set(["partial", "blocked", "cancelled", "interrupted"]);

function setStatus(status) {
  const pill = $("status-pill");
  pill.textContent = status.replace(/_/g, " ");
  pill.className = "pill " + (
    status === "running" ? "pill--running"
      : status === "waiting" ? "pill--waiting"
        : OK.has(status) ? "pill--ok"
          : WARN.has(status) ? "pill--warn"
            : BAD.has(status) ? "pill--bad"
              : "pill--idle");
}

function startClock() {
  state.startedAt = Date.now();
  clearInterval(state.tick);
  state.tick = setInterval(() => {
    if (!state.startedAt) return;
    const seconds = Math.round((Date.now() - state.startedAt) / 1000);
    $("elapsed").textContent = `${seconds}s`;
  }, 1000);
}

function stopClock() {
  clearInterval(state.tick);
  state.tick = null;
  if (state.startedAt) {
    $("elapsed").textContent = `${Math.round((Date.now() - state.startedAt) / 1000)}s`;
  }
}

// ── feed ────────────────────────────────────────────────────────────────
function addEntry({ tool, text, kind = "", step = null, args = null }) {
  const feed = $("feed");
  feed.querySelector("p.empty")?.remove();
  const entry = document.createElement("div");
  entry.className = `entry ${kind}`.trim();
  const label = step === null ? "" : String(step).padStart(2, "0");
  entry.innerHTML =
    `<span class="entry__n">${esc(label)}</span>` +
    `<span><span class="entry__tool">${esc(tool)}</span>` +
    (args ? `<span class="entry__args"> ${esc(shorten(args, 110))}</span>` : "") +
    (text ? `<span class="entry__sum">${esc(text)}</span>` : "") +
    `</span>`;
  feed.append(entry);
  feed.scrollTop = feed.scrollHeight;
}

function clearFeed() {
  $("feed").innerHTML = '<p class="empty">Waiting for the first step.</p>';
}

// ── plan + memory ───────────────────────────────────────────────────────
function renderPlan(plan) {
  const list = $("plan");
  const steps = plan?.steps || [];
  list.innerHTML = steps.length
    ? steps.map((s) =>
        `<li data-status="${esc(s.status)}"><span class="desc">${esc(s.description)}` +
        (s.note ? `<span class="note">${esc(s.note)}</span>` : "") +
        `</span></li>`).join("")
    : '<li class="empty">No plan yet.</li>';

  // Guarded: this is called with null before a run starts, and an exception here
  // used to abort boot() before the examples were ever fetched.
  $("criteria").innerHTML = (plan?.criteria || []).map((c) =>
    `<li data-cid="${esc(c.id)}"><span class="mark">○</span>${esc(c.description)}` +
    `<span class="detail" data-for="${esc(c.id)}"></span></li>`).join("");
}

function markCriterion(criterionId, passed, detail) {
  if (!criterionId) return;
  const row = document.querySelector(`#criteria li[data-cid="${CSS.escape(criterionId)}"]`);
  if (!row) return;
  row.className = passed ? "pass" : "fail";
  const mark = row.querySelector(".mark");
  if (mark) mark.textContent = passed ? "✓" : "✕";
  const slot = row.querySelector(".detail");
  if (slot && detail) slot.textContent = shorten(detail, 300);
}

// VerificationReport serialises its checks under "criteria" and each result
// under "id". Accept both spellings so a schema tweak cannot silently leave the
// panel showing four unmarked circles.
function markAllCriteria(report) {
  (report?.criteria || report?.results || []).forEach((r) =>
    markCriterion(r.criterion_id || r.id, r.passed, r.evidence || r.detail || r.actual));
}

function renderFacts(facts) {
  const body = $("facts");
  if (!facts || !facts.length) {
    body.innerHTML = '<tr><td colspan="3" class="empty">Nothing remembered yet.</td></tr>';
    return;
  }
  body.innerHTML = facts.map((f) => {
    const conflict = (f.conflicts || []).length
      ? `<div class="conflict">conflicts with ${f.conflicts.length} other source(s)</div>` : "";
    return `<tr><td>${esc(f.key)}</td><td>${esc(shorten(f.value, 60))}` +
           `${conflict}<div class="src">step ${esc(f.step)}${f.source ? " · " + esc(shorten(f.source, 40)) : ""}</div></td></tr>`;
  }).join("");
}

// ── human in the loop ───────────────────────────────────────────────────
function showQuestion(question) {
  state.questions.set(question.id, question);
  const isApproval = question.kind === "approval";
  $("q-kind").textContent = isApproval ? "approval required" : "question";
  $("q-title").textContent = question.question;
  $("q-just").textContent = question.justification || question.context || "";
  $("q-fp").textContent = question.fingerprint ? `fingerprint ${question.fingerprint}` : "";
  $("q-approve").textContent = isApproval ? "Approve" : "Send";
  $("q-deny").hidden = !isApproval;
  $("q-input").value = "";
  // An approval has exactly two answers. Showing a free-text box on one invites
  // someone to type a justification into a field nothing will read.
  $("q-input").hidden = isApproval;
  $("q-input").previousElementSibling.hidden = isApproval;

  const options = $("q-options");
  options.innerHTML = "";
  (question.options || []).forEach((option) => {
    const button = document.createElement("button");
    button.textContent = option;
    button.onclick = () => { $("q-input").value = option; $("q-input").focus(); };
    options.append(button);
  });

  $("scrim").hidden = false;
  setStatus("waiting");
  $("q-input").focus();
}

function closeQuestion() {
  $("scrim").hidden = true;
  if (state.runId) refreshStatus();
}

async function sendAnswer(answer) {
  const id = state.currentQuestionId;
  if (!id) return;
  try {
    await api(`/api/runs/${state.runId}/answer`, {
      method: "POST",
      body: JSON.stringify({ question_id: id, answer }),
    });
    state.questions.delete(id);
    state.currentQuestionId = null;
    closeQuestion();
  } catch (error) {
    // 409 means the run already moved on — usually a double click.
    toast(error.message);
    closeQuestion();
  }
}

// ── report ──────────────────────────────────────────────────────────────
function renderReport(outcome, confirmation) {
  const panel = $("report-panel");
  const report = outcome?.verification;
  panel.hidden = false;

  const good = OK.has(outcome?.status);
  const summary = report?.summary || outcome?.error || "";
  let html =
    `<div class="verdict"><span class="verdict__status ${good ? "note-good" : "note-bad"}">` +
    `${esc(outcome?.status || "unknown")}</span>` +
    `<span class="verdict__summary">${esc(summary)}</span></div>`;

  const kv = [];
  const add = (k, v) => { if (v !== undefined && v !== null && v !== "") kv.push(`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`); };
  const totalTokens = Object.values(outcome?.tokens || {}).reduce((a, b) => a + (Number(b) || 0), 0);
  add("steps", outcome?.steps?.length);
  add("elapsed", `${outcome?.elapsed_seconds ?? "?"}s`);
  add("tokens", totalTokens ? totalTokens.toLocaleString() : "");
  add("repairs", outcome?.repairs);
  add("error", outcome?.error);
  if (kv.length) html += `<dl class="kv">${kv.join("")}</dl>`;

  markAllCriteria(report);

  if (confirmation) {
    const seen = confirmation.visible
      ? '<span class="note-good">confirmed in a fresh browser</span>'
      : `<span class="note-bad">not visible</span>`;
    const detail = confirmation.detail ? `<dt>how</dt><dd>${esc(shorten(confirmation.detail, 160))}</dd>` : "";
    html += `<dl class="kv"><dt>second look</dt><dd>${esc(confirmation.invoice_number || "")} ${seen}</dd>${detail}</dl>`;
  }

  $("report").innerHTML = html;
  loadTrace();
}

async function loadTrace() {
  if (!state.runId || $("trace").hidden === false) return;
  try {
    const data = await api(`/api/runs/${state.runId}/trace`);
    $("trace").textContent = data.events
      .map((e) => JSON.stringify(e))
      .join("\n");
  } catch { /* trace is a nicety, not a requirement */ }
}

// ── screenshots ─────────────────────────────────────────────────────────
function pollScreenshot() {
  clearInterval(state.shotTimer);
  state.shotTimer = setInterval(refreshScreenshot, 2500);
}

async function refreshScreenshot() {
  if (!state.runId) return;
  try {
    const response = await fetch(`/api/runs/${state.runId}/screenshot`);
    if (response.status === 204) return;   // nothing captured yet
    if (!response.ok) return;
    const blob = await response.blob();
    if (!blob.size) return;
    const img = $("shot");
    const previous = img.src;
    img.src = URL.createObjectURL(blob);
    img.hidden = false;
    $("shot-empty").hidden = true;
    if (previous.startsWith("blob:")) URL.revokeObjectURL(previous);
  } catch { /* no screenshot yet */ }
}

// ── event stream ────────────────────────────────────────────────────────
function connect(runId) {
  state.source?.close();
  const source = new EventSource(`/api/runs/${runId}/events`);
  state.source = source;

  const on = (name, fn) => source.addEventListener(name, (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    fn(payload.data || {}, payload);
  });

  on("run.started", () => { setStatus("running"); startClock(); });
  on("thought", (data) => addEntry({ tool: "thinking", text: shorten(data.text, 220), kind: "entry--thought" }));
  on("step", (data) => addEntry({
    tool: data.tool, step: data.index, args: data.args ? JSON.stringify(data.args) : null,
    text: data.summary, kind: data.ok ? "" : "entry--bad",
  }));
  on("state", (data) => {
    if (data.plan) renderPlan(data.plan);
    if (data.facts) renderFacts(data.facts);
  });
  on("recovery", (data) => addEntry({
    tool: `recovery: ${data.strategy}`,
    text: data.detail || "recovered", kind: "entry--recovery",
  }));
  on("failure", (data) => addEntry({ tool: `failure: ${data.kind}`, text: data.message, kind: "entry--bad" }));
  on("question", (data) => {
    state.currentQuestionId = data.id;
    showQuestion(data);
  });
  on("question.resolved", () => setStatus("running"));
  on("verify.started", (data) => addEntry({
    tool: "verify", text: `re-checking ${data.criteria} criteria independently`, kind: "entry--verify",
  }));
  on("verify.finished", (data) => {
    addEntry({ tool: "verifier", text: data.summary, kind: "entry--verify" });
    markAllCriteria(data);
  });
  on("verify.repair", (data) => addEntry({
    tool: "repair", text: data.detail, kind: "entry--recovery",
  }));
  on("llm.error", (data) => addEntry({ tool: "llm error", text: data.error, kind: "entry--bad" }));
  on("run.error", (data) => { setStatus("error"); toast(data.error); });
  on("confirm", () => { /* folded into the report once the run finishes */ });
  on("run.state", (data) => { setStatus(data.status); });
  on("run.finished", async () => {
    try {
      const summary = await api(`/api/runs/${state.runId}`);
      stopClock();
      setStatus(summary.status);
      renderReport(summary.outcome, summary.confirmation);
      // One last grab of the screenshot, then stop polling.
      await refreshScreenshot();
      clearInterval(state.shotTimer);
      $("start").disabled = false;
      $("cancel").disabled = true;
    } catch { /* the stream already told us it finished */ }
  });
  source.onerror = () => { /* EventSource retries on its own */ };
}

async function refreshStatus() {
  if (!state.runId) return;
  try { setStatus((await api(`/api/runs/${state.runId}`)).status); } catch { /* gone */ }
}

// ── run control ─────────────────────────────────────────────────────────
async function startRun() {
  const goal = $("goal").value.trim();
  if (goal.length < 3) { toast("Describe the task first."); return; }

  $("start").disabled = true;
  $("cancel").disabled = false;
  clearFeed();
  renderPlan(null);
  renderFacts(null);
  $("report-panel").hidden = true;
  $("criteria").innerHTML = "";
  $("shot").hidden = true;
  $("shot-empty").hidden = false;
  state.questions.clear();
  state.currentQuestionId = null;
  setStatus("running");
  startClock();

  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ goal, headless: true }),
    });
    state.runId = run.run_id;
    $("run-id").textContent = run.run_id;
    connect(run.run_id);
    pollScreenshot();
  } catch (error) {
    setStatus("error");
    toast(`Could not start: ${error.message}`);
    $("start").disabled = false;
    $("cancel").disabled = true;
  }
}

async function cancelRun() {
  if (!state.runId) return;
  $("cancel").disabled = true;
  try { await api(`/api/runs/${state.runId}/cancel`, { method: "POST" }); }
  catch (error) { toast(error.message); }
}

// ── faults ──────────────────────────────────────────────────────────────
async function setFault(name, enabled, box) {
  try {
    const result = await api("/api/faults", {
      method: "POST",
      body: JSON.stringify(enabled ? { name, enabled: true } : { reset: true }),
    });
    // The env apps answer 200 even when they ignore the request, so trust
    // `armed` rather than the status code.
    const armed = Object.values(result || {}).some((r) => (r.armed || []).includes(name));
    if (enabled && !armed) {
      box.checked = false;
      toast(`Could not arm ${name}.`);
    } else {
      toast(enabled ? `Armed: ${name}` : `Disarmed: ${name}`);
    }
  } catch (error) {
    box.checked = !enabled;
    toast(`Fault control failed: ${error.message}`);
  }
}

function renderFaults() {
  const host = $("faults");
  host.innerHTML = "";
  FAULTS.forEach(([name, label]) => {
    const wrap = document.createElement("label");
    wrap.className = "fault";
    wrap.innerHTML = `<input type="checkbox"><span>${esc(label)} <code>${esc(name)}</code></span>`;
    const box = wrap.querySelector("input");
    box.onchange = () => setFault(name, box.checked, box);
    host.append(wrap);
  });
}

// ── boot ────────────────────────────────────────────────────────────────
async function boot() {
  $("start").onclick = startRun;
  $("cancel").onclick = cancelRun;
  $("q-approve").onclick = () => sendAnswer($("q-approve").textContent === "Approve" ? "approve" : $("q-input").value);
  $("q-deny").onclick = () => sendAnswer("deny");
  $("q-input").onkeydown = (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) $("q-approve").click();
  };
  // ⌘/Ctrl+Enter submits from anywhere while the modal is open.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey)) return;
    if ($("scrim").hidden || !state.currentQuestionId) return;
    event.preventDefault();
    $("q-approve").click();
  });
  $("faults-reset").onclick = async () => {
    try {
      await api("/api/faults", { method: "POST", body: JSON.stringify({ reset: true }) });
      document.querySelectorAll("#faults input").forEach((b) => { b.checked = false; });
      toast("All faults disarmed.");
    } catch (error) { toast(error.message); }
  };
  $("trace-toggle").onclick = async () => {
    const pre = $("trace");
    pre.hidden = !pre.hidden;
    if (!pre.hidden && !pre.textContent) await loadTrace();
    $("trace-toggle").textContent = pre.hidden ? "Show raw trace" : "Hide raw trace";
  };
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("scrim").hidden && state.currentQuestionId) {
      // Escape must not silently approve anything; it sends an empty answer.
      sendAnswer("");
    }
  });

  renderFaults();
  renderPlan(null);
  renderFacts(null);

  try {
    const config = await api("/api/config");
    const list = $("examples");
    list.innerHTML = "";
    (config.examples || []).forEach((example) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.textContent = example;
      button.onclick = () => { $("goal").value = example; $("goal").focus(); };
      item.append(button);
      list.append(item);
    });
    if (config.examples?.length) $("goal").value = config.examples[0];
  } catch { /* the app still works without examples */ }
}

boot();