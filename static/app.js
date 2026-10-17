// Review Analyzer frontend. Plain JavaScript, no build step, no third-party code.
// All user-supplied text is inserted with textContent (never innerHTML) to avoid XSS.
"use strict";

const SENTIMENTS = ["positive", "neutral", "negative"];
const LABEL = { positive: "Positive", neutral: "Neutral", negative: "Negative" };
const POLL_MS = 1000;

const $ = (id) => document.getElementById(id);
const state = { status: null, file: null, jobId: null, result: null, filter: "all", query: "" };

// ---------- tiny DOM helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

const pct = (n, total) => (total ? Math.round((n / total) * 100) : 0);
const fmtBytes = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.ceil(b / 1024)} KB`);

function show(id, visible) { $(id).hidden = !visible; }

function showError(message) {
  $("error").textContent = message;
  show("error", Boolean(message));
}

async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const msg = body?.detail?.message
      || (Array.isArray(body?.detail) ? "Please choose a CSV file first." : null)
      || `Request failed (HTTP ${res.status}). Please try again.`;
    throw new Error(msg);
  }
  return body;
}

// ---------- status & limits ----------

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch {
    $("limits").textContent = "Couldn't reach the server.";
    return;
  }
  const s = state.status;
  $("f-rows").textContent = s.max_rows;
  $("limits").textContent =
    `Up to ${s.max_rows} reviews and ${fmtBytes(s.max_upload_bytes)} per file · ` +
    `${s.uploads_remaining_for_you} analyses left for you this hour · ` +
    `${s.calls_remaining_today} of ${s.daily_call_limit} AI calls left today`;
  $("disabled-reason").textContent = s.reason || "";
  show("disabled-reason", !s.can_analyze);
  updateButtons();
}

function updateButtons() {
  const can = Boolean(state.status?.can_analyze) && !state.jobId;
  $("analyze-btn").disabled = !can || !state.file;
  $("sample-btn").disabled = !can;
}

// ---------- file selection ----------

function selectFile(file) {
  showError("");
  state.file = null;
  $("dropzone").classList.remove("has-file");
  if (!file) return updateButtons();
  const max = state.status?.max_upload_bytes ?? 2 * 1048576;
  if (!/\.(csv|tsv|txt)$/i.test(file.name)) {
    showError("Please choose a .csv file. In Excel use File › Save As › CSV UTF-8.");
  } else if (file.size > max) {
    showError(`That file is ${fmtBytes(file.size)}. The limit is ${fmtBytes(max)}.`);
  } else if (file.size === 0) {
    showError("That file is empty.");
  } else {
    state.file = file;
    $("dropzone").classList.add("has-file");
    $("dz-main").textContent = file.name;
    $("dz-hint").textContent = `${fmtBytes(file.size)} · ready to analyze`;
  }
  updateButtons();
}

function setupDropzone() {
  const dz = $("dropzone");
  $("file-input").addEventListener("change", (e) => selectFile(e.target.files[0]));
  for (const ev of ["dragenter", "dragover"]) {
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); });
  }
  dz.addEventListener("drop", (e) => selectFile(e.dataTransfer.files[0]));
}

// ---------- running an analysis ----------

async function startAnalysis() {
  if (!state.file) return;
  showError("");
  const form = new FormData();
  form.append("file", state.file);
  $("analyze-btn").disabled = true;
  try {
    const { job_id } = await api("/api/analyze", { method: "POST", body: form });
    state.jobId = job_id;
    history.replaceState(null, "", `#job=${job_id}`);
    updateButtons();
    show("results", false);
    show("progress-card", true);
    setProgress(0, 1, "Uploading…");
    poll();
  } catch (err) {
    showError(err.message);
    refreshStatus();
  }
}

async function useSample() {
  showError("");
  try {
    const res = await fetch("/api/sample.csv");
    const blob = await res.blob();
    selectFile(new File([blob], "coffee_shop_reviews.csv", { type: "text/csv" }));
    await startAnalysis();
  } catch (err) {
    showError(err.message);
  }
}

function setProgress(done, total, message) {
  // Show a sliver of progress immediately so the bar never looks stuck at zero.
  const value = Math.max(4, pct(done, total));
  $("progress-fill").style.width = `${value}%`;
  $("progressbar").setAttribute("aria-valuenow", String(value));
  $("progress-msg").textContent = message;
}

async function poll() {
  let job;
  try {
    job = await api(`/api/jobs/${state.jobId}`);
  } catch (err) {
    return finishWithError(err.message);
  }
  setProgress(job.progress.done, job.progress.total, job.progress.message);
  if (job.status === "running") return setTimeout(poll, POLL_MS);
  if (job.status === "failed") return finishWithError(job.error);
  show("progress-card", false);
  renderResults(job.result, job.id);
  state.jobId = null;
  refreshStatus();
}

function finishWithError(message) {
  state.jobId = null;
  history.replaceState(null, "", location.pathname);
  show("progress-card", false);
  showError(message);
  refreshStatus();
}

// ---------- results ----------

function renderResults(result, jobId) {
  state.result = result;
  const n = result.analyses.length;
  $("results-sub").textContent =
    `${result.filename} · ${n} reviews · ${result.api_calls_used} AI calls`;
  $("download-btn").href = `/api/jobs/${jobId}/report.xlsx`;
  show("fake-notice", result.provider === "fake");
  show("results", true); // Visible before drawing, so charts can measure their width.

  renderTiles(result);
  renderSentimentChart(result);
  renderTopicChart(result);
  renderRatingChart(result);
  renderInsights($("complaints"), result.insights.top_complaints, "No recurring complaints found.");
  renderInsights($("praise"), result.insights.top_praise, "No recurring praise found.");
  renderActions(result.insights.suggested_actions);
  renderTable();
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderTiles(result) {
  const c = result.sentiment_counts;
  const n = result.analyses.length;
  const ratings = result.reviews.map((r) => r.rating).filter((r) => r != null);
  const tiles = [
    ["Reviews analyzed", String(n), result.filename],
    ["Positive", `${pct(c.positive, n)}%`, `${c.positive} reviews`],
    ["Negative", `${pct(c.negative, n)}%`, `${c.negative} reviews`],
  ];
  if (ratings.length) {
    const avg = ratings.reduce((a, b) => a + b, 0) / ratings.length;
    tiles.push(["Average rating", avg.toFixed(1), `from ${ratings.length} rated reviews`]);
  } else {
    tiles.push(["Topics found", String(result.topic_stats.length), "after grouping"]);
  }
  $("tiles").replaceChildren(...tiles.map(([label, value, sub]) =>
    el("div", { class: "tile" },
      el("div", { class: "tile-label", text: label }),
      el("div", { class: "tile-value", text: value }),
      el("div", { class: "tile-sub", text: sub }))));
}

// Tooltip shared by both charts.
function attachTooltip(target, title, counts) {
  const tip = $("tooltip");
  target.addEventListener("mousemove", (e) => {
    const rows = SENTIMENTS.filter((s) => counts[s] != null).map((s) =>
      el("div", { class: `tt-row ${s}` }, el("span", { text: LABEL[s] }), el("b", { text: String(counts[s]) })));
    tip.replaceChildren(el("strong", { text: title }), ...rows);
    tip.hidden = false;
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = `${x}px`;
    tip.style.top = `${e.clientY + 14}px`;
  });
  target.addEventListener("mouseleave", () => { tip.hidden = true; });
}

function legend() {
  return el("div", { class: "legend" }, ...SENTIMENTS.map((s) => el("span", { class: s, text: LABEL[s] })));
}

// A path with only the right-hand corners rounded: the bar's "data end".
function roundedRight(x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}` +
         `Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
}

let clipSeq = 0;

/** Draw stacked segments (2px surface gap between them) clipped to a bar shape. */
function stackedBar(parent, x, y, width, height, counts, total, clipShape) {
  const id = `clip${++clipSeq}`;
  const clip = svg("clipPath", { id });
  clip.append(clipShape);
  parent.append(clip);
  const group = svg("g", { "clip-path": `url(#${id})`, class: "seg-group" });
  let cursor = x;
  const present = SENTIMENTS.filter((s) => counts[s] > 0);
  present.forEach((s, i) => {
    const w = (counts[s] / total) * width;
    const gap = i < present.length - 1 ? 2 : 0;
    group.append(svg("rect", { x: cursor, y, width: Math.max(0, w - gap), height, class: `seg fill-${s}` }));
    cursor += w;
  });
  parent.append(group);
}

function renderSentimentChart(result) {
  const box = $("sentiment-chart");
  const c = result.sentiment_counts;
  const n = result.analyses.length;
  const W = Math.max(260, box.clientWidth || 400);
  const H = 70;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": SENTIMENTS.map((s) => `${LABEL[s]} ${pct(c[s], n)}%`).join(", ") });
  stackedBar(root, 0, 30, W, 24, c, n, svg("rect", { x: 0, y: 30, width: W, height: 24, rx: 4 }));

  // Direct labels above each segment, only where they fit.
  let cursor = 0;
  for (const s of SENTIMENTS) {
    const w = (c[s] / n) * W;
    if (c[s] > 0 && w > 70) {
      root.append(svg("text", { x: cursor + 2, y: 20, class: "label-strong" }, `${pct(c[s], n)}%`));
      root.append(svg("text", { x: cursor + 38, y: 20 }, LABEL[s].toLowerCase()));
    }
    cursor += w;
  }
  const hit = svg("rect", { x: 0, y: 26, width: W, height: 32, class: "hit" });
  root.append(hit);
  attachTooltip(hit, `${n} reviews`, c);

  const counts = el("div", { class: "legend" }, ...SENTIMENTS.map((s) =>
    el("span", { class: s, text: `${LABEL[s]}: ${c[s]} (${pct(c[s], n)}%)` })));
  box.replaceChildren(root, counts);
}

function niceStep(max) {
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  return [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag;
}

function renderTopicChart(result) {
  const rows = result.topic_stats.map((t) => ({ ...t, label: t.topic, title: `${t.topic} · ${t.total} reviews` }));
  renderStackedRows($("topic-chart"), rows, "Reviews per topic");
}

// Does the AI's sentiment agree with the star rating? One row per star value.
function renderRatingChart(result) {
  const box = $("rating-chart");
  const byId = new Map(result.reviews.map((r) => [r.id, r]));
  const rows = new Map();
  for (const a of result.analyses) {
    const rating = byId.get(a.id).rating;
    if (rating == null) continue;
    const stars = Math.round(rating);
    if (!rows.has(stars)) rows.set(stars, { positive: 0, neutral: 0, negative: 0, total: 0 });
    const row = rows.get(stars);
    row[a.sentiment] += 1;
    row.total += 1;
  }
  show("rating-block", rows.size > 0);
  if (!rows.size) return box.replaceChildren();
  const ordered = [...rows.entries()].sort((a, b) => b[0] - a[0]).map(([stars, c]) =>
    ({ ...c, label: `${stars}★`, title: `${stars}-star reviews · ${c.total}` }));
  renderStackedRows(box, ordered, "Sentiment by star rating", { labelWidth: 44, legend: false });
}

/** Horizontal stacked bars, one per row, with a value at each bar's tip. */
function renderStackedRows(box, stats, ariaLabel, { labelWidth = null, legend: withLegend = true } = {}) {
  const W = Math.max(260, box.clientWidth || 560);
  const labelW = labelWidth ?? Math.min(150, W * 0.34);
  const rowH = 34, barH = 20, top = 4, axisH = 22;
  const plotW = W - labelW - 36; // Room for the total at the bar tip.
  const max = Math.max(1, ...stats.map((t) => t.total));
  const step = niceStep(max);
  const axisMax = Math.ceil(max / step) * step;
  const H = top + stats.length * rowH + axisH;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${ariaLabel}: ${stats.map((t) => `${t.label} ${t.total}`).join(", ")}` });
  const scale = (v) => (v / axisMax) * plotW;

  // Recessive vertical gridlines and tick labels.
  for (let v = 0; v <= axisMax; v += step) {
    const x = labelW + scale(v);
    root.append(svg("line", { x1: x, x2: x, y1: top, y2: H - axisH, class: v === 0 ? "baseline" : "grid" }));
    root.append(svg("text", { x, y: H - 6, "text-anchor": "middle" }, String(v)));
  }

  stats.forEach((t, i) => {
    const y = top + i * rowH + (rowH - barH) / 2;
    const row = svg("g", { class: "row" });
    const name = t.label.length > 22 ? `${t.label.slice(0, 21)}…` : t.label;
    row.append(svg("text", { x: labelW - 10, y: y + barH / 2 + 4, "text-anchor": "end", class: "label-strong" }, name));
    const w = scale(t.total);
    stackedBar(row, labelW, y, w, barH, t, t.total, svg("path", { d: roundedRight(labelW, y, w, barH, 4) }));
    row.append(svg("text", { x: labelW + w + 6, y: y + barH / 2 + 4, class: "value" }, String(t.total)));
    const hit = svg("rect", { x: 0, y: top + i * rowH, width: W, height: rowH, class: "hit" });
    row.append(hit);
    attachTooltip(hit, t.title, t);
    root.append(row);
  });
  box.replaceChildren(root, ...(withLegend ? [legend()] : []));
}

function renderInsights(list, items, emptyText) {
  if (!items.length) return list.replaceChildren(el("li", { class: "empty", text: emptyText }));
  list.replaceChildren(...items.map((item) =>
    el("li", {},
      el("strong", { text: item.title }),
      el("p", { text: item.detail }),
      el("span", { class: "count", text: `${item.mentions} review${item.mentions === 1 ? "" : "s"}` }))));
}

function renderActions(actions) {
  $("actions").replaceChildren(...actions.map((a) =>
    el("li", {}, el("strong", { text: a.title }), el("p", { text: a.detail }))));
}

function renderTable() {
  const r = state.result;
  if (!r) return;
  const byId = new Map(r.reviews.map((rev) => [rev.id, rev]));
  const q = state.query.trim().toLowerCase();
  const rows = r.analyses.filter((a) => {
    if (state.filter !== "all" && a.sentiment !== state.filter) return false;
    if (!q) return true;
    return `${a.topic} ${a.summary} ${byId.get(a.id).text}`.toLowerCase().includes(q);
  });
  $("reviews-body").replaceChildren(...rows.map((a) => {
    const rev = byId.get(a.id);
    return el("tr", {},
      el("td", { text: String(a.id) }),
      el("td", {}, el("span", { class: `pill pill-${a.sentiment}`, text: LABEL[a.sentiment] })),
      el("td", { text: a.topic }),
      el("td", { class: "summary" },
        el("details", {}, el("summary", { text: a.summary }), el("p", { text: rev.text }))),
      el("td", { text: rev.rating != null ? `${rev.rating}★` : "–" }));
  }));
  $("table-count").textContent = `Showing ${rows.length} of ${r.analyses.length} reviews. Click a summary to read the full review.`;
}

function setupTable() {
  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === chip));
    state.filter = chip.dataset.filter;
    renderTable();
  }));
  $("search").addEventListener("input", (e) => { state.query = e.target.value; renderTable(); });
}

// Restore a finished job after a page refresh (#job=<id>).
async function restoreFromHash() {
  const match = location.hash.match(/^#job=([\w-]+)$/);
  if (!match) return;
  state.jobId = match[1];
  show("progress-card", true);
  poll();
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.result && !$("results").hidden) {
      renderSentimentChart(state.result);
      renderTopicChart(state.result);
      renderRatingChart(state.result);
    }
  }, 150);
});

document.addEventListener("DOMContentLoaded", () => {
  setupDropzone();
  setupTable();
  $("analyze-btn").addEventListener("click", startAnalysis);
  $("sample-btn").addEventListener("click", useSample);
  $("restart-btn").addEventListener("click", () => {
    show("results", false);
    history.replaceState(null, "", location.pathname);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  refreshStatus();
  restoreFromHash();
});
