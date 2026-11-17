import { createVoice, acknowledgement } from "./voice.js";

const $ = (s, el = document) => el.querySelector(s);
const messages = $("#messages"), form = $("#askForm"), input = $("#question");
const orb = $("#orb"), voiceState = $("#voiceState"), interimEl = $("#interim");

const store = {   // per-viewer conveniences only; the app works without storage
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let config = { voice: { provider: "browser" }, initiatives: [] };
let history = [];          // [{role, content}] for follow-up questions
let current = null;        // AbortController of the in-flight question
let voice = null, live = false, spokenFromVoice = false;

// ---------------------------------------------------------------- API helpers
function headers(extra = {}) {
  const key = store.get("accessKey");
  return { "Content-Type": "application/json", ...(key ? { "X-Access-Key": key } : {}), ...extra };
}
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: headers(opts.headers) });
  if (r.status === 401) { promptKey(); throw new Error("Access key required"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
  return r.json();
}
function promptKey() {
  const k = prompt("This demo is protected. Enter the access key:");
  if (k) store.set("accessKey", k.trim());
}

// ---------------------------------------------------------------- markdown (escaped, minimal)
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[((?:S|C)\d+)\]/g, '<button type="button" class="cite" data-n="$1" aria-label="Source $1">$1</button>');
}
function renderMarkdown(md) {
  const lines = String(md || "").replace(/\r/g, "").split("\n"), out = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*\|/.test(l)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const body = rows.filter(r => !/^\s*\|?\s*:?-{3,}/.test(r));
      out.push("<table><thead><tr>" + cells(body[0]).map(c => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>"
        + body.slice(1).map(r => "<tr>" + cells(r).map(c => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
    } else if (/^\s*([-*•]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d/.test(l), items = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
      out.push(`<${ordered ? "ol" : "ul"}>` + items.map(t => `<li>${inline(t)}</li>`).join("") + `</${ordered ? "ol" : "ul"}>`);
    } else if (l.trim()) {
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(\||[-*•]\s|\d+[.)]\s)/.test(lines[i])) para.push(lines[i++]);
      out.push(`<p>${inline(para.join(" "))}</p>`);
    } else i++;
  }
  return out.join("");
}
const rag = s => s ? `<span class="rag ${esc(s)}">${esc(s)}</span>` : "";
function rowsTable(cols, rows, max = 12) {
  if (!cols?.length) return "";
  return `<table class="data small"><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${
    rows.slice(0, max).map(r => `<tr>${r.map(v => `<td>${esc(v ?? "not available")}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    + (rows.length > max ? `<p class="muted">${rows.length - max} more rows</p>` : "");
}

// ---------------------------------------------------------------- tabs
function showView(name) {
  document.querySelectorAll(".tab").forEach(t => t.setAttribute("aria-selected", String(t.dataset.view === name)));
  document.querySelectorAll(".view").forEach(v => { v.hidden = v.id !== `view-${name}`; });
  store.set("view", name);
  ({ scorecard: loadScorecard, quality: loadChangeLog, report: loadReport, charters: loadCharters }[name] || (() => {}))();
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => showView(t.dataset.view)));

// ---------------------------------------------------------------- chat rendering
function addMessage(role, html) {
  $("#emptyState")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (role === "user") el.textContent = html; else el.innerHTML = html;
  messages.append(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

const STEP_LABEL = { sql: "Reading the governed scorecard", charter: "Searching the initiative charters",
                     "charter-sections": "Attaching charter risks for those initiatives" };
const ROUTE_LABEL = { numeric: "Scorecard question", narrative: "Charter question", mixed: "Scorecard + charters",
                      pii_refusal: "Personal data request", not_in_data: "Not in the supplied data", clarify: "Needs clarification",
                      greeting: "Greeting" };
function progressTracker(el) {
  const steps = new Map();
  const box = document.createElement("div"); box.className = "progress"; el.replaceChildren(box);
  const step = (key, text, done = false) => {
    let s = steps.get(key);
    if (!s) { s = document.createElement("div"); s.className = "step"; box.append(s); steps.set(key, s); }
    s.textContent = text; s.classList.toggle("done", done);
  };
  step("plan", "Routing the question…");
  return ev => {
    if (ev.type === "plan") {
      step("plan", `Routed: ${ROUTE_LABEL[ev.route] || ev.route}${ev.initiatives?.length ? " · " + ev.initiatives.join(", ") : ""}`, true);
      ev.agents.forEach(a => step(a, STEP_LABEL[a] + "…"));
    } else if (ev.type === "agent_done") step(ev.agent, `${STEP_LABEL[ev.agent] || ev.agent} (${ev.evidence} source${ev.evidence === 1 ? "" : "s"})`, true);
    else if (ev.type === "agent_error") step(ev.agent, `${STEP_LABEL[ev.agent] || ev.agent}: failed`, true);
    else if (ev.type === "draft") step("draft" + ev.revision, ev.revision ? `Revising (${ev.revision})…` : "Writing the answer…");
    else if (ev.type === "guard") step("guard" + steps.size, `Number check: ${ev.numbers_verified}/${ev.numbers_checked} figures match the scorecard`, true);
    else if (ev.type === "verdict") step("verdict" + steps.size, ev.skipped ? "Review: all figures verified in code" :
      `Review: grounded ${ev.groundedness}/5, relevant ${ev.relevance}/5, complete ${ev.completeness}/5 → ${ev.action}`, true);
    messages.scrollTop = messages.scrollHeight;
  };
}

function charterUrl(file) { return file ? `/files/charters/${encodeURIComponent(file)}` : ""; }

function renderAnswer(el, r) {
  const parts = [];
  parts.push(`<div class="answer ${r.type}">${renderMarkdown(r.answer_markdown || "")}</div>`);
  if (r.type === "clarify" && r.clarification_options?.length)
    parts.push(`<div class="suggestions left">${r.clarification_options.map(o => `<button type="button" class="chip clarify">${esc(o)}</button>`).join("")}</div>`);

  const g = r.guardrails || {};
  const notes = [...(g.notes || [])];
  if (g.removed_sentences?.length) notes.push(`${g.removed_sentences.length} statement(s) removed because they could not be verified against the scorecard.`);
  if (notes.length) parts.push(`<div class="guard-note" role="note"><b>Guardrails</b><ul>${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>`);

  if (r.citations?.length) {
    parts.push(`<div class="sources">${r.citations.map(c => c.kind === "sql"
      ? `<details class="source" data-n="${c.label}"><summary><span class="n">[${c.label}]</span><span><b>Governed scorecard</b> <span class="where">${esc(c.title)}</span></span></summary>
          <pre class="sql">${esc(c.query)}</pre>${rowsTable(c.columns, c.rows)}</details>`
      : `<details class="source" data-n="${c.label}"><summary><span class="n">[${c.label}]</span><span><b>${esc(c.source_file)}</b> <span class="where">${esc(c.section)}${c.page ? " · p. " + c.page : ""}</span>${c.injection_flag ? ' <span class="pill warn">instruction found: treated as data</span>' : ""}</span></summary>
          <blockquote>${esc(c.excerpt)}</blockquote><a href="${charterUrl(c.source_file)}" target="_blank" rel="noopener">Open charter ↗</a></details>`).join("")}</div>`);
  }
  if (r.closest_matches?.length)
    parts.push(`<div class="sources"><div class="muted">Closest charter passages (they do not answer the question):</div>${r.closest_matches.map(m =>
      `<details class="source"><summary><b>${esc(m.source_file)}</b>&nbsp;<span class="where">${esc(m.section)}</span></summary><blockquote>${esc(m.excerpt)}</blockquote></details>`).join("")}</div>`);

  const conf = r.confidence, t = r.trace?.timings_ms?.total;
  parts.push(`<div class="meta-row">
    ${r.route ? `<span class="pill">${esc(ROUTE_LABEL[r.route] || r.route)}</span>` : ""}
    ${g.numbers_checked ? `<span class="badge ${g.passed ? "high" : "low"}" title="Every figure is compared with the governed scorecard values in code">✓ ${g.numbers_verified}/${g.numbers_checked} figures verified</span>` : ""}
    ${conf ? `<span class="badge ${conf.label}" title="${esc(conf.reason || "")}">confidence: ${conf.label}</span>` : ""}
    ${t ? `<span class="pill">${(t / 1000).toFixed(1)} s</span>` : ""}
    ${r.trace?.revisions ? `<span class="pill">revised ×${r.trace.revisions}</span>` : ""}
    <span class="fb"><button type="button" data-rate="1" aria-label="Helpful" aria-pressed="false">👍</button><button type="button" data-rate="-1" aria-label="Not helpful" aria-pressed="false">👎</button></span>
  </div>
  <details class="trace"><summary>How this answer was produced</summary><pre>${esc(JSON.stringify(r.trace, null, 2))}</pre></details>`);
  el.innerHTML = parts.join("");

  el.querySelectorAll(".chip.clarify").forEach(b => b.addEventListener("click", () => ask(b.textContent)));
  el.querySelectorAll(".cite").forEach(b => b.addEventListener("click", () => {
    const card = el.querySelector(`.source[data-n="${b.dataset.n}"]`);
    if (!card) return;
    card.open = true; card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    card.classList.add("flash"); setTimeout(() => card.classList.remove("flash"), 1200);
  }));
  el.querySelectorAll(".fb button").forEach(b => b.addEventListener("click", async () => {
    const rating = Number(b.dataset.rate);
    const comment = rating < 0 ? (prompt("What was wrong? (optional, helps the assistant learn)") || "") : "";
    el.querySelectorAll(".fb button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    try { await api("/api/feedback", { method: "POST", body: JSON.stringify({ interaction_id: r.interaction_id, rating, comment }) }); }
    catch (e) { console.warn(e); }
  }));
}

// ---------------------------------------------------------------- asking
async function ask(question, { fromVoice = false } = {}) {
  question = question.trim();
  if (!question) return;
  showView("ask");
  current?.abort();
  voice?.interrupt();
  const ctrl = new AbortController(); current = ctrl;
  spokenFromVoice = fromVoice;

  addMessage("user", question);
  const botEl = addMessage("bot", "");
  const onEvent = progressTracker(botEl);
  setVoiceState("thinking");
  try {
    const resp = await fetch("/api/ask/stream", { method: "POST", headers: headers(), signal: ctrl.signal,
      body: JSON.stringify({ question, history: history.slice(-6) }) });
    if (resp.status === 401) { promptKey(); throw new Error("Access key required. Please ask again."); }
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).error || `HTTP ${resp.status}`);
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "", answer = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = chunk.split("\n").find(x => x.startsWith("data: "));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.type === "answer") answer = ev.response;
        else if (ev.type === "error") throw new Error(ev.message);
        else {
          onEvent(ev);
          if (ev.type === "plan" && ev.agents?.length && shouldSpeak()) voice?.say(acknowledgement(ev.agents));
        }
      }
    }
    if (!answer) throw new Error("No answer received");
    renderAnswer(botEl, answer);
    history.push({ role: "user", content: question },
                 { role: "assistant", content: (answer.answer_markdown || "").replace(/\[(?:S|C)?\d+\]/g, "").slice(0, 600) });
    if (shouldSpeak()) voice?.say(answer.spoken_summary || "");
  } catch (e) {
    if (e.name === "AbortError") { botEl.innerHTML = `<span class="muted">Cancelled.</span>`; return; }
    botEl.innerHTML = `<span class="badge low">Error</span> ${esc(e.message)}`;
  } finally {
    if (current === ctrl) { current = null; setVoiceState(live ? (voice?.speaking ? "speaking" : "listening") : "off"); }
  }
}
const shouldSpeak = () => $("#speakToggle").checked && voice && (live || spokenFromVoice);

// ---------------------------------------------------------------- scorecard
async function loadScorecard() {
  try {
    const { rows, summary: s } = await api("/api/scorecard");
    $("#scMeta").textContent = `As of ${s.as_of} · calculated in code (${s.calc_version}) from the cleaned data files`;
    $("#scTiles").innerHTML = `
      <div class="kpi"><b>${s.green} · ${s.amber} · ${s.red}</b><span>${rag("Green")} ${rag("Amber")} ${rag("Red")}</span></div>
      <div class="kpi"><b>${esc(s.total_realised_benefit_display)}</b><span>realised benefit to date</span></div>
      <div class="kpi"><b>${esc(s.total_cost_display)}</b><span>total cost to date</span></div>
      <div class="kpi"><b>${esc(s.portfolio_roi_display)}</b><span title="${esc(s.definition_note)}">portfolio ROI (aggregate)</span></div>
      <div class="kpi"><b>${esc(s.portfolio_adoption_display)}</b><span>latest-week adoption, all licences</span></div>`;
    const cols = [["initiative_id", "Initiative"], ["name", "Name"], ["status", "Status"], ["adoption_display", "Adoption"],
      ["wau_change_display", "WAU change"], ["roi_display", "ROI"], ["avg_monthly_benefit_display", "Avg monthly benefit"],
      ["payback_display", "Payback"], ["run_rate_vs_plan_display", "Run-rate vs plan"]];
    $("#scTable").innerHTML = `<thead><tr>${cols.map(c => `<th>${c[1]}</th>`).join("")}</tr></thead><tbody>${rows.map(r =>
      `<tr tabindex="0" data-id="${esc(r.initiative_id)}" title="${esc(r.status_reason)}">${cols.map(([k]) =>
        `<td>${k === "status" ? rag(r[k]) : esc(r[k] ?? "not available")}</td>`).join("")}</tr>`).join("")}</tbody>`;
    $("#scTable").querySelectorAll("tbody tr").forEach(tr => {
      const open = () => showProvenance(tr.dataset.id);
      tr.addEventListener("click", open);
      tr.addEventListener("keydown", e => { if (e.key === "Enter") open(); });
    });
  } catch (e) { $("#scMeta").textContent = e.message; }
}

async function showProvenance(id) {
  const d = await api(`/api/scorecard/${id}`), r = d.row, i = d.initiative;
  $("#provTitle").innerHTML = `${esc(id)} ${esc(r.name)} ${rag(r.status)}`;
  $("#provBody").innerHTML = `
    <p><b>Status rule:</b> ${esc(r.status_reason)}</p>
    ${r.data_notes ? `<p class="muted">${esc(r.data_notes)}</p>` : ""}
    <p class="muted">Sponsor: ${esc(i.sponsor_name)} (${esc(i.sponsor_role)}) · Product owner: ${esc(i.owner_name)} (${esc(i.owner_role)})</p>
    <table class="data small"><thead><tr><th>Metric</th><th>Value</th><th>Formula</th><th>Inputs</th></tr></thead><tbody>${
      d.provenance.map(p => `<tr><td>${esc(p.metric.replaceAll("_", " "))}</td><td><b>${esc(p.value_display)}</b></td><td>${esc(p.formula)}</td><td>${esc(p.inputs)}</td></tr>`).join("")}</tbody></table>
    <h3>Data corrections affecting this initiative</h3>
    ${d.changes.length ? `<ul>${d.changes.map(c => `<li><b>${esc(c.id)}</b> ${esc(c.file)} (${esc(c.kind.replaceAll("_", " "))}): ${esc(c.before)} → ${esc(c.after)}<br><span class="muted">${esc(c.effect)}</span></li>`).join("")}</ul>` : '<p class="muted">None.</p>'}`;
  $("#provDialog").showModal();
}

// ---------------------------------------------------------------- change log
async function loadChangeLog() {
  try {
    const rows = await api("/api/changelog");
    $("#clTable").innerHTML = `<thead><tr><th>Id</th><th>File</th><th>Initiative</th><th>Issue</th><th>Before → after</th><th>Why</th><th>Effect on the scorecard</th></tr></thead><tbody>${
      rows.map(c => `<tr><td>${esc(c.id)}</td><td>${esc(c.file)}</td><td>${esc(c.initiative)}</td><td>${esc(c.kind.replaceAll("_", " "))}</td>
        <td><code>${esc(c.before)}</code> → <code>${esc(c.after)}</code></td><td>${esc(c.reason)}</td><td>${esc(c.effect)}</td></tr>`).join("")}</tbody>`;
  } catch (e) { $("#clTable").innerHTML = `<caption>${esc(e.message)}</caption>`; }
}

// ---------------------------------------------------------------- weekly report
function showReport(r) {
  $("#reportBody").innerHTML = r.html;
  $("#reportLinks").innerHTML = Object.entries(r.files || {}).map(([k, v]) => `<a href="${esc(v)}" target="_blank" rel="noopener" ${k === "pdf" ? "download" : ""}>${k.toUpperCase()}</a>`).join(" ");
}
async function loadReport() {
  try { showReport(await api("/api/report/weekly")); }
  catch { $("#reportBody").innerHTML = '<p class="muted">No report yet. Generate one (about 15–40 s).</p>'; }
}
$("#genReport").addEventListener("click", async e => {
  const b = e.currentTarget; b.disabled = true; b.textContent = "Generating…";
  $("#reportBody").innerHTML = '<p class="muted">Reading the scorecard, retrieving charter passages and writing verified notes…</p>';
  try { showReport(await api("/api/report/weekly", { method: "POST" })); }
  catch (err) { $("#reportBody").innerHTML = `<p class="badge low">${esc(err.message)}</p>`; }
  finally { b.disabled = false; b.textContent = "Generate this week's report"; }
});

// ---------------------------------------------------------------- charters
async function loadCharters() {
  $("#uploadInit").innerHTML = (config.initiatives || []).map(i => `<option value="${esc(i.initiative_id)}">${esc(i.initiative_id)} ${esc(i.name)}</option>`).join("");
  try {
    const list = await api("/api/charters");
    $("#chTable").innerHTML = `<thead><tr><th>Initiative</th><th>Charter</th><th>Format</th><th>Sections</th></tr></thead><tbody>${list.map(c =>
      `<tr><td>${esc(c.initiative_id)}</td><td><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.file)}</a>${c.source === "upload" ? ' <span class="pill">uploaded</span>' : ""}${c.flagged_passages?.length ? ' <span class="pill warn">instruction found</span>' : ""}</td>
       <td>${esc(c.format)}</td><td>${Array.isArray(c.sections) ? esc(c.sections.join(" · ")) : esc(c.sections)}</td></tr>`).join("")}</tbody>`;
  } catch (e) { $("#chTable").innerHTML = `<caption>${esc(e.message)}</caption>`; }
}
$("#uploadForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = $("#uploadFile").files[0], status = $("#uploadStatus");
  if (!f) return;
  const fd = new FormData(); fd.append("file", f); fd.append("initiative_id", $("#uploadInit").value);
  status.textContent = "Uploading, scanning with Prompt Shields and indexing…";
  const key = store.get("accessKey");
  try {
    const r = await fetch("/api/charters", { method: "POST", body: fd, headers: key ? { "X-Access-Key": key } : {} });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    status.textContent = `Indexed “${j.title}”: ${j.sections} sections → ${j.chunks} passages` +
      (j.flagged_passages.length ? `; ${j.flagged_passages.length} passage(s) contain instructions (treated as data).` : ".");
    loadCharters();
  } catch (err) { status.textContent = "Failed: " + err.message; }
});

// ---------------------------------------------------------------- voice wiring
function setVoiceState(s) {
  orb.dataset.state = s;
  orb.setAttribute("aria-pressed", String(live));
  voiceState.textContent = { off: "Voice off", listening: "Listening…", thinking: "Thinking…", speaking: "Speaking…" }[s] || s;
}
async function ensureVoice() {
  if (voice) return voice;
  voice = await createVoice(config, () => api("/api/speech/token"), {
    onInterim: t => { interimEl.textContent = t; },
    onFinal: t => { interimEl.textContent = ""; ask(t, { fromVoice: true }); },
    onBargeIn: () => { current?.abort(); setVoiceState("listening"); },
    onSpeaking: v => { if (live && !current) setVoiceState(v ? "speaking" : "listening"); },
    onError: e => { console.warn(e); voiceState.textContent = "Voice error: " + e.message; },
  });
  $("#voiceEngine").textContent = "voice: " + voice.name;
  voice.headphones = $("#headphonesToggle").checked;
  return voice;
}
$("#headphonesToggle").checked = store.get("headphones") === "1";
$("#headphonesToggle").addEventListener("change", e => {
  store.set("headphones", e.target.checked ? "1" : "0");
  if (voice) voice.headphones = e.target.checked;
});
function interruptSpeech() {
  if (!voice?.speaking) return false;
  voice.interrupt();
  setVoiceState(live ? "listening" : "off");
  return true;
}
document.addEventListener("keydown", e => { if (e.key === "Escape") interruptSpeech(); });
orb.addEventListener("click", async () => {
  if (interruptSpeech()) return;
  try {
    await ensureVoice();
    if (live) { live = false; await voice.stop(); voice.interrupt(); setVoiceState("off"); interimEl.textContent = ""; }
    else { await voice.start(); live = true; setVoiceState("listening"); }
  } catch (e) { voiceState.textContent = e.message; }
});

// ---------------------------------------------------------------- composer, dialogs
form.addEventListener("submit", e => { e.preventDefault(); const q = input.value; input.value = ""; autosize(); ask(q); });
input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; };
input.addEventListener("input", autosize);
document.querySelectorAll(".suggestions .chip").forEach(b => b.addEventListener("click", () => ask(b.textContent)));
$("#newChatBtn").addEventListener("click", () => { current?.abort(); voice?.interrupt(); history = []; store.set("view", "ask"); location.reload(); });

$("#metricsBtn").addEventListener("click", async () => {
  $("#metricsDialog").showModal();
  const body = $("#metricsBody");
  try {
    const m = await api("/api/metrics");
    const s = m.avg_final_scores || {};
    body.innerHTML = `<div class="kpis">
      <div class="kpi"><b>${m.interactions}</b><span>questions answered</span></div>
      <div class="kpi"><b>${m.first_draft_pass_rate == null ? "–" : Math.round(m.first_draft_pass_rate * 100) + "%"}</b><span>first drafts passing review</span></div>
      <div class="kpi"><b>${s.groundedness ?? "–"}</b><span>avg groundedness /5</span></div>
      <div class="kpi"><b>${m.feedback.up} / ${m.feedback.down}</b><span>👍 / 👎</span></div></div>
      <h3>Lessons learned from reviews and feedback</h3>
      ${m.lessons.length ? `<table class="data small"><tbody>${m.lessons.map(l => `<tr><td>${esc(l.lesson)}</td><td class="muted">${l.source}</td><td class="muted">used ${l.uses}×</td></tr>`).join("")}</tbody></table>` : '<p class="muted">None yet.</p>'}`;
  } catch (e) { body.textContent = e.message; }
});

// ---------------------------------------------------------------- boot
(async () => {
  try { config = await api("/api/config"); } catch (e) { console.warn(e); }
  $("#voiceEngine").textContent = "voice: " + (config.voice?.provider === "azure" ? "Azure Speech" : "browser");
  if (!config.uploads) $("#uploadForm").hidden = true;
  setVoiceState("off");
  const v = store.get("view");
  if (v && v !== "ask" && document.getElementById(`view-${v}`)) showView(v);
})();
