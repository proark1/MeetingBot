/* ── MeetingBot Dashboard ──────────────────────────────────────────────────── */

const API = "/api/v1";

// ── Utilities ──────────────────────────────────────────────────────────────

async function apiFetch(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

function esc(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day:"2-digit", month:"short", year:"numeric" }) +
    " " + d.toLocaleTimeString("en-GB", { hour:"2-digit", minute:"2-digit" });
}

function fmtDuration(start, end) {
  if (!start || !end) return "—";
  const secs = Math.round((new Date(end) - new Date(start)) / 1000);
  const m = Math.floor(secs / 60), s = secs % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function fmtTs(secs) {
  const m = String(Math.floor(secs / 60)).padStart(2, "0");
  const s = String(Math.floor(secs % 60)).padStart(2, "0");
  return `${m}:${s}`;
}

const PLATFORM_ICONS = {
  zoom:"🔵", google_meet:"🟢", microsoft_teams:"🟣",
  webex:"🔷", whereby:"🟠", bluejeans:"🔵", gotomeeting:"🟤", unknown:"🤖",
};
const platformIcon = (p) => PLATFORM_ICONS[p] || "🤖";

const LIFECYCLE_STEPS = [
  { key: "ready",       label: "Ready",       icon: "○" },
  { key: "joining",     label: "Joining",     icon: "→" },
  { key: "in_call",     label: "In Call",     icon: "▶" },
  { key: "call_ended",  label: "Ending",      icon: "■" },
  { key: "done",        label: "Done",        icon: "✓" },
];
const STEP_ORDER = LIFECYCLE_STEPS.map((s) => s.key);

function statusBadge(status) {
  return `<span class="badge badge-${esc(status)}">${esc(status.replace(/_/g, " "))}</span>`;
}

// ── Toast ──────────────────────────────────────────────────────────────────

function showToast(msg, type = "info") {
  const icons = { success: "✅", error: "❌", info: "ℹ️" };
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span class="toast-icon">${icons[type] || "ℹ️"}</span><span class="toast-text">${esc(msg)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("toast-out");
    toast.addEventListener("animationend", () => toast.remove());
  }, 3500);
}

// ── Routing ────────────────────────────────────────────────────────────────

function showPage(id) {
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  document.getElementById("page-" + id).classList.add("active");
  document.querySelectorAll(".nav-item[data-page]").forEach((n) => {
    n.classList.toggle("active", n.dataset.page === id);
  });
}

document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
  el.addEventListener("click", (e) => {
    if (el.getAttribute("target") === "_blank") return;
    e.preventDefault();
    const page = el.dataset.page;
    if (page === "bots")     { showPage("bots"); loadBots(); }
    if (page === "webhooks") { showPage("webhooks"); loadWebhooks(); }
    if (page === "debug")    { showPage("debug"); loadDebugFiles(); }
  });
});

// ── Modals ─────────────────────────────────────────────────────────────────

function openModal(id) {
  document.getElementById(id).classList.remove("hidden");
}
function closeModal(id) {
  document.getElementById(id).classList.add("hidden");
  // Clear form fields
  document.querySelectorAll(`#${id} .input`).forEach((inp) => {
    if (inp.id === "new-bot-name") inp.value = "MeetingBot";
    else if (inp.type !== "checkbox") inp.value = "";
  });
  document.querySelectorAll(`#${id} .field-error`).forEach((e) => {
    e.classList.add("hidden"); e.textContent = "";
  });
  document.querySelectorAll(`#${id} .input.error`).forEach((e) => {
    e.classList.remove("error");
  });
}

document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => closeModal(btn.dataset.close));
});
document.querySelectorAll(".modal-backdrop").forEach((bd) => {
  bd.addEventListener("click", (e) => { if (e.target === bd) closeModal(bd.id); });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.querySelectorAll(".modal-backdrop:not(.hidden)").forEach((m) => closeModal(m.id));
  }
});

function showFieldError(inputId, errorId, msg) {
  const inp = document.getElementById(inputId);
  const err = document.getElementById(errorId);
  if (inp) inp.classList.add("error");
  if (err) { err.textContent = msg; err.classList.remove("hidden"); }
}

// ── WebSocket ──────────────────────────────────────────────────────────────

let _wsRetryDelay = 1000;
let _ws = null;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  _ws = new WebSocket(`${proto}//${location.host}/api/v1/ws`);

  _ws.onopen = () => {
    _wsRetryDelay = 1000;
    setWsStatus(true);
    // Keep-alive
    setInterval(() => _ws && _ws.readyState === WebSocket.OPEN && _ws.send("ping"), 25000);
  };

  _ws.onmessage = (e) => {
    try {
      const { event, data } = JSON.parse(e.data);
      if (event && data) handleServerEvent(event, data);
    } catch (_) {}
  };

  _ws.onclose = () => {
    setWsStatus(false);
    setTimeout(connectWS, _wsRetryDelay);
    _wsRetryDelay = Math.min(_wsRetryDelay * 2, 30000);
  };

  _ws.onerror = () => _ws.close();
}

function setWsStatus(connected) {
  const el = document.getElementById("ws-indicator");
  const label = el.querySelector(".ws-label");
  el.className = `ws-indicator ${connected ? "connected" : "disconnected"}`;
  label.textContent = connected ? "Live" : "Reconnecting…";
}

function handleServerEvent(event, data) {
  // Update stats bar on any bot event
  if (event.startsWith("bot.")) {
    loadStats();

    // If on the bots list page, refresh the affected row
    if (document.getElementById("page-bots").classList.contains("active")) {
      updateBotRow(data.bot_id, data);
    }

    // If the detail page is showing the same bot, refresh it
    const detailPage = document.getElementById("page-bot-detail");
    if (detailPage.classList.contains("active") && _currentBotId === data.bot_id) {
      refreshBotDetail(data.bot_id);
    }
  }
}

// ── Stats ──────────────────────────────────────────────────────────────────

async function loadStats() {
  try {
    const s = await apiFetch("GET", "/bot/stats");
    document.getElementById("stat-total").textContent = s.total;
    document.getElementById("stat-active").textContent = s.active;
    document.getElementById("stat-done").textContent = s.done;
    document.getElementById("stat-error").textContent = s.error;
  } catch (_) {}
}

// ── Bots list ──────────────────────────────────────────────────────────────

let _currentFilter = "";
let _bots = [];

async function loadBots(filter = _currentFilter) {
  _currentFilter = filter;
  const listEl = document.getElementById("bot-list");
  if (!_bots.length) listEl.innerHTML = '<div class="loading">Loading…</div>';

  try {
    const qs = filter ? `?status=${filter}` : "";
    const data = await apiFetch("GET", `/bot${qs}`);
    _bots = data.results;
    renderBotList(_bots);
  } catch (e) {
    listEl.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }

  await loadStats();
}

function renderBotList(bots) {
  const listEl = document.getElementById("bot-list");
  if (!bots.length) {
    listEl.innerHTML = `
      <div class="empty">
        No reports yet${_currentFilter ? ` with status "${_currentFilter}"` : ""}.
        <div class="empty-action">
          <button class="btn btn-primary btn-sm" onclick="openModal('modal-new-bot')">+ Send your first bot</button>
        </div>
      </div>`;
    return;
  }

  listEl.innerHTML = bots.map((b) => {
    const duration = fmtDuration(b.started_at, b.ended_at);
    const participants = (b.participants || []).length;
    const transcriptLen = (b.transcript || []).length;
    const summary = b.analysis?.summary || "";
    const sentiment = b.analysis?.sentiment || "";

    const stats = [
      b.started_at ? `🕐 ${fmtDate(b.started_at)}` : `📅 ${fmtDate(b.created_at)}`,
      duration !== "—" ? `⏱ ${duration}` : "",
      participants ? `👥 ${participants} participant${participants !== 1 ? "s" : ""}` : "",
      transcriptLen ? `💬 ${transcriptLen} entries` : "",
    ].filter(Boolean).join(" &nbsp;·&nbsp; ");

    const sentimentChip = sentiment
      ? `<span class="sentiment-badge sentiment-${esc(sentiment)}">${esc(sentiment)}</span>`
      : "";

    const demoChip = b.is_demo_transcript
      ? `<span class="badge badge-demo" title="No real audio was captured — this transcript was AI-generated">demo</span>`
      : "";

    const summaryEl = summary
      ? `<div class="report-summary">${esc(summary.length > 160 ? summary.slice(0, 160) + "…" : summary)}</div>`
      : "";

    return `
    <div class="report-card" data-id="${esc(b.id)}">
      <div class="report-header">
        <div class="report-platform">
          <span class="report-platform-icon">${platformIcon(b.meeting_platform)}</span>
          <span class="report-platform-name">${esc(b.meeting_platform.replace(/_/g, " "))}</span>
        </div>
        <div class="report-header-right">
          <span data-badge-id="${esc(b.id)}">${statusBadge(b.status)}</span>
          ${sentimentChip}
          ${demoChip}
          <button class="btn btn-danger btn-sm" data-delete-bot="${esc(b.id)}" title="Delete">🗑</button>
        </div>
      </div>
      <div class="report-url">${esc(b.meeting_url)}</div>
      <div class="report-stats">${stats}</div>
      ${summaryEl}
    </div>`;
  }).join("");

  listEl.querySelectorAll(".report-card").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-delete-bot]")) return;
      showBotDetail(card.dataset.id);
    });
  });

  listEl.querySelectorAll("[data-delete-bot]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this report and all its data?")) return;
      btn.disabled = true;
      try {
        await apiFetch("DELETE", `/bot/${btn.dataset.deleteBot}`);
        showToast("Report deleted", "success");
        loadBots();
      } catch (err) {
        showToast(err.message, "error");
        btn.disabled = false;
      }
    });
  });
}

function updateBotRow(botId, data) {
  // Update just the status badge on the list row without full refresh
  const badgeEl = document.querySelector(`[data-badge-id="${botId}"]`);
  if (badgeEl && data.status) badgeEl.innerHTML = statusBadge(data.status);
  // Update _bots cache
  const idx = _bots.findIndex((b) => b.id === botId);
  if (idx >= 0) _bots[idx] = { ..._bots[idx], ...data };
}

// ── Filter buttons ─────────────────────────────────────────────────────────

document.querySelectorAll(".filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    loadBots(btn.dataset.status || "");
  });
});

document.getElementById("btn-refresh").addEventListener("click", () => loadBots());

// ── Create Bot ─────────────────────────────────────────────────────────────

document.getElementById("btn-new-bot").addEventListener("click", () => openModal("modal-new-bot"));

async function submitCreateBot() {
  const urlInput = document.getElementById("new-bot-url");
  const nameInput = document.getElementById("new-bot-name");
  const url = urlInput.value.trim();
  const name = nameInput.value.trim() || "MeetingBot";

  // Client-side validation
  if (!url) {
    showFieldError("new-bot-url", "new-bot-url-error", "Meeting URL is required");
    return;
  }
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    showFieldError("new-bot-url", "new-bot-url-error", "URL must start with http:// or https://");
    return;
  }

  const btn = document.getElementById("btn-create-bot");
  btn.disabled = true;
  btn.textContent = "Creating…";

  try {
    const bot = await apiFetch("POST", "/bot", { meeting_url: url, bot_name: name });
    closeModal("modal-new-bot");
    showToast(`Bot created — joining now`, "success");
    showBotDetail(bot.id);
    loadBots();
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Create Bot";
  }
}

document.getElementById("btn-create-bot").addEventListener("click", submitCreateBot);
document.getElementById("modal-new-bot").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.target.matches("textarea")) submitCreateBot();
});

// ── Bot Detail ─────────────────────────────────────────────────────────────

let _currentBotId = null;

async function showBotDetail(botId) {
  _currentBotId = botId;
  showPage("bot-detail");
  renderBotDetailLoading();
  await refreshBotDetail(botId);
}

function renderBotDetailLoading() {
  document.getElementById("bot-detail-badges").innerHTML = "";
  document.getElementById("lifecycle-steps").innerHTML =
    LIFECYCLE_STEPS.map((s) => `
      <div class="lifecycle-step" data-step="${s.key}">
        <div class="step-dot">${s.icon}</div>
        <div class="step-label">${s.label}</div>
      </div>`).join("");
  document.getElementById("bot-detail-content").innerHTML = '<div class="loading">Loading…</div>';
}

async function refreshBotDetail(botId) {
  if (_currentBotId !== botId) return; // navigated away
  try {
    const bot = await apiFetch("GET", `/bot/${botId}`);
    if (_currentBotId !== botId) return;
    renderBotDetail(bot);
  } catch (e) {
    if (_currentBotId !== botId) return;
    document.getElementById("bot-detail-content").innerHTML =
      `<div class="empty">Error loading bot: ${esc(e.message)}</div>`;
  }
}

function renderBotDetail(bot) {
  // Badges in header
  const badgesEl = document.getElementById("bot-detail-badges");
  const demoBadge = bot.is_demo_transcript
    ? `<span class="badge badge-demo" title="No real audio was captured — this transcript was AI-generated">demo transcript</span>`
    : "";
  badgesEl.innerHTML = `
    <span style="font-size:1.25rem">${platformIcon(bot.meeting_platform)}</span>
    <strong style="font-size:1rem">${esc(bot.bot_name)}</strong>
    ${statusBadge(bot.status)}
    ${demoBadge}`;

  // Lifecycle steps
  renderLifecycleSteps(bot.status);

  // Meta grid
  const duration = fmtDuration(bot.started_at, bot.ended_at);
  const contentEl = document.getElementById("bot-detail-content");

  const errorBanner = bot.error_message
    ? `<div class="error-banner">⚠ ${esc(bot.error_message)}</div>`
    : "";

  contentEl.innerHTML = `
    ${errorBanner}
    <div class="detail-meta">
      <div class="meta-item"><div class="meta-label">Platform</div><div class="meta-value">${esc(bot.meeting_platform)}</div></div>
      <div class="meta-item"><div class="meta-label">Started</div><div class="meta-value">${fmtDate(bot.started_at)}</div></div>
      <div class="meta-item"><div class="meta-label">Duration</div><div class="meta-value">${duration}</div></div>
      <div class="meta-item"><div class="meta-label">Created</div><div class="meta-value">${fmtDate(bot.created_at)}</div></div>
      <div class="meta-item"><div class="meta-label">Participants</div><div class="meta-value">${(bot.participants || []).length || "—"}</div></div>
      <div class="meta-item"><div class="meta-label">Transcript</div><div class="meta-value">${(bot.transcript || []).length} entries</div></div>
      <div class="meta-item"><div class="meta-label">Meeting URL</div><div class="meta-value" style="font-family:var(--font-mono);font-size:0.75rem;color:var(--text-muted)">${esc(bot.meeting_url.slice(0, 40))}${bot.meeting_url.length > 40 ? "…" : ""}</div></div>
    </div>

    <!-- Participants section -->
    ${(bot.participants || []).length ? `
    <div class="section-card">
      <div class="section-header"><h3>Participants</h3></div>
      <div class="pill-list" style="padding:0.25rem 0 0.5rem">
        ${(bot.participants || []).map((p) => `<span class="pill">${esc(p)}</span>`).join("")}
      </div>
    </div>` : ""}

    <!-- Transcript section -->
    <div class="section-card">
      <div class="section-header">
        <h3>Transcript <span style="color:var(--text-muted);font-size:0.8rem;font-weight:400">(${(bot.transcript||[]).length} entries)</span></h3>
        <div style="display:flex;gap:0.5rem">
          ${(bot.transcript||[]).length ? `<button class="btn btn-icon" id="btn-copy-transcript" title="Copy transcript">📋 Copy</button>` : ""}
          ${(bot.transcript||[]).length && bot.analysis ? `<button class="btn btn-icon" id="btn-export-json" title="Download JSON">⬇ Export</button>` : ""}
          ${(bot.transcript||[]).length ? `<button class="btn btn-sm btn-primary" id="btn-reanalyse">✨ Analyse with Claude</button>` : ""}
        </div>
      </div>
      ${renderTranscript(bot.transcript)}
    </div>

    <!-- Analysis section -->
    <div class="section-card" id="analysis-section">
      <div class="section-header">
        <h3>AI Analysis</h3>
        ${bot.analysis ? `<span class="sentiment-badge sentiment-${esc(bot.analysis.sentiment || 'neutral')}">${esc(bot.analysis.sentiment || 'neutral')}</span>` : ""}
      </div>
      ${renderAnalysis(bot.analysis)}
    </div>`;

  // Wire up buttons
  const copyBtn = document.getElementById("btn-copy-transcript");
  if (copyBtn) copyBtn.addEventListener("click", () => copyTranscript(bot.transcript));

  const exportBtn = document.getElementById("btn-export-json");
  if (exportBtn) exportBtn.addEventListener("click", () => exportJson(bot));

  const analyseBtn = document.getElementById("btn-reanalyse");
  if (analyseBtn) analyseBtn.addEventListener("click", () => reanalyse(bot.id));
}

function renderLifecycleSteps(status) {
  const stepsEl = document.getElementById("lifecycle-steps");
  const currentIdx = status === "error"
    ? STEP_ORDER.indexOf("call_ended") // show error at last reached step
    : STEP_ORDER.indexOf(status);

  stepsEl.innerHTML = LIFECYCLE_STEPS.map((s, i) => {
    let cls = "";
    if (status === "error" && i === currentIdx) cls = "error";
    else if (i < currentIdx) cls = "done";
    else if (i === currentIdx) cls = "active";

    const icon = cls === "done" ? "✓" : s.icon;
    return `
      <div class="lifecycle-step ${cls}" data-step="${s.key}">
        <div class="step-dot">${icon}</div>
        <div class="step-label">${s.label}</div>
      </div>`;
  }).join("");
}

function renderTranscript(transcript) {
  if (!transcript || !transcript.length) {
    return '<div class="empty" style="padding:1.5rem">No transcript yet — bot is still running</div>';
  }
  return `<div class="transcript-list">` +
    transcript.map((e) => `
      <div class="transcript-entry">
        <span class="t-speaker">${esc(e.speaker)}</span>
        <span class="t-ts">${fmtTs(e.timestamp)}</span>
        <span class="t-text">${esc(e.text)}</span>
      </div>`).join("") +
    `</div>`;
}

function renderAnalysis(a) {
  if (!a) {
    return '<div class="empty" style="padding:1.5rem">No analysis yet — appears automatically after the meeting ends</div>';
  }

  const actionItems = (a.action_items || []).length
    ? (a.action_items || []).map((item) => `
        <div class="action-row">
          <span style="color:var(--accent)">☑</span>
          <div class="action-task">${esc(item.task)}</div>
          ${item.assignee ? `<div class="action-assignee">@${esc(item.assignee)}</div>` : ""}
        </div>`).join("")
    : '<div style="color:var(--text-muted);font-size:0.875rem">None identified</div>';

  const bullets = (items) => !items?.length
    ? '<li style="color:var(--text-muted)">—</li>'
    : items.map((t) => `<li>${esc(t)}</li>`).join("");

  return `
    ${a.summary ? `<div class="analysis-summary">${esc(a.summary)}</div>` : ""}

    ${(a.topics||[]).length ? `
    <div class="analysis-section">
      <div class="analysis-section-title">Topics</div>
      <div class="pill-list">${(a.topics||[]).map((t) => `<span class="pill">${esc(t)}</span>`).join("")}</div>
    </div>` : ""}

    <div class="analysis-section">
      <div class="analysis-section-title">Key Points</div>
      <ul class="bullet-list">${bullets(a.key_points)}</ul>
    </div>

    <div class="analysis-section">
      <div class="analysis-section-title">Action Items</div>
      ${actionItems}
    </div>

    ${(a.decisions||[]).length ? `
    <div class="analysis-section">
      <div class="analysis-section-title">Decisions</div>
      <ul class="bullet-list">${bullets(a.decisions)}</ul>
    </div>` : ""}

    ${(a.next_steps||[]).length ? `
    <div class="analysis-section">
      <div class="analysis-section-title">Next Steps</div>
      <ul class="bullet-list">${bullets(a.next_steps)}</ul>
    </div>` : ""}`;
}

async function reanalyse(botId) {
  const btn = document.getElementById("btn-reanalyse");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Analysing…";
  try {
    const analysis = await apiFetch("POST", `/bot/${botId}/analyze`);
    // Patch in-place
    const section = document.getElementById("analysis-section");
    if (section) {
      section.innerHTML = `
        <div class="section-header">
          <h3>AI Analysis</h3>
          <span class="sentiment-badge sentiment-${esc(analysis.sentiment || 'neutral')}">${esc(analysis.sentiment || 'neutral')}</span>
        </div>
        ${renderAnalysis(analysis)}`;
    }
    showToast("Analysis complete!", "success");
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "✨ Analyse with Claude"; }
  }
}

function copyTranscript(transcript) {
  const text = (transcript || [])
    .map((e) => `[${fmtTs(e.timestamp)}] ${e.speaker}: ${e.text}`)
    .join("\n");
  navigator.clipboard.writeText(text).then(
    () => showToast("Transcript copied to clipboard", "success"),
    () => showToast("Copy failed — try selecting manually", "error"),
  );
}

function exportJson(bot) {
  const data = {
    id: bot.id,
    bot_name: bot.bot_name,
    meeting_url: bot.meeting_url,
    meeting_platform: bot.meeting_platform,
    started_at: bot.started_at,
    ended_at: bot.ended_at,
    participants: bot.participants || [],
    transcript: bot.transcript,
    analysis: bot.analysis,
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `meetingbot-${bot.id.slice(0, 8)}.json`;
  a.click();
  URL.revokeObjectURL(url);
  showToast("Exported as JSON", "success");
}

// ── Back button ────────────────────────────────────────────────────────────

document.getElementById("btn-back-bots").addEventListener("click", () => {
  _currentBotId = null;
  showPage("bots");
  loadBots();
});

// ── Webhooks ───────────────────────────────────────────────────────────────

// Toggle "all events" checkbox vs individual
document.getElementById("wh-ev-all").addEventListener("change", (e) => {
  const list = document.getElementById("wh-event-list");
  list.classList.toggle("disabled", e.target.checked);
  if (e.target.checked) {
    list.querySelectorAll("input[type=checkbox]").forEach((cb) => (cb.checked = false));
  }
});

async function loadWebhooks() {
  const listEl = document.getElementById("webhook-list");
  listEl.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const webhooks = await apiFetch("GET", "/webhook");
    if (!webhooks.length) {
      listEl.innerHTML = `
        <div class="empty">
          No webhooks registered yet.
          <div class="empty-action">
            <button class="btn btn-primary btn-sm" onclick="openModal('modal-new-webhook')">+ Add Webhook</button>
          </div>
        </div>`;
      return;
    }
    listEl.innerHTML = webhooks.map((wh) => `
      <div class="webhook-row">
        <div class="webhook-url">${esc(wh.url)}</div>
        <div class="webhook-events">
          ${wh.events.map((e) => `<span class="chip chip-code">${esc(e)}</span>`).join("")}
        </div>
        <div style="white-space:nowrap;font-size:0.75rem;color:var(--text-muted)">
          ${wh.delivery_attempts} sent
          ${wh.last_delivery_status ? `· ${wh.last_delivery_status}` : ""}
        </div>
        <button class="btn btn-danger btn-sm" data-del-wh="${esc(wh.id)}" title="Delete webhook">🗑</button>
      </div>`).join("");

    listEl.querySelectorAll("[data-del-wh]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this webhook?")) return;
        btn.disabled = true;
        try {
          await apiFetch("DELETE", `/webhook/${btn.dataset.delWh}`);
          showToast("Webhook deleted", "success");
          loadWebhooks();
        } catch (e) {
          showToast(e.message, "error");
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    listEl.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

document.getElementById("btn-new-webhook").addEventListener("click", () =>
  openModal("modal-new-webhook")
);

async function submitCreateWebhook() {
  const urlInput = document.getElementById("new-wh-url");
  const url = urlInput.value.trim();

  if (!url) {
    showFieldError("new-wh-url", "new-wh-url-error", "URL is required");
    return;
  }
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    showFieldError("new-wh-url", "new-wh-url-error", "Must start with http:// or https://");
    return;
  }

  const secret = document.getElementById("new-wh-secret").value.trim() || null;
  const allEvt = document.getElementById("wh-ev-all").checked;
  let events = ["*"];
  if (!allEvt) {
    events = Array.from(
      document.querySelectorAll("#wh-event-list input[type=checkbox]:checked")
    ).map((cb) => cb.value);
    if (!events.length) events = ["*"];
  }

  const btn = document.getElementById("btn-create-webhook");
  btn.disabled = true;
  btn.textContent = "Registering…";

  try {
    await apiFetch("POST", "/webhook", { url, events, secret });
    closeModal("modal-new-webhook");
    showToast("Webhook registered", "success");
    loadWebhooks();
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Register";
  }
}

document.getElementById("btn-create-webhook").addEventListener("click", submitCreateWebhook);
document.getElementById("modal-new-webhook").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.target.matches("select")) submitCreateWebhook();
});

// ── Debug / Screenshots ────────────────────────────────────────────────────

async function loadDebugFiles() {
  const listEl = document.getElementById("debug-file-list");
  listEl.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const data = await apiFetch("GET", "/debug/screenshots");
    const files = data.files || [];
    if (!files.length) {
      listEl.innerHTML = `<div class="empty">No screenshots yet — they appear here when a bot fails to join a meeting.</div>`;
      return;
    }
    listEl.innerHTML = `<div class="debug-grid">${files.map((f) => debugFileCard(f)).join("")}</div>`;
  } catch (e) {
    listEl.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

function debugFileCard(f) {
  const ts = new Date(f.modified * 1000).toLocaleString("en-GB");
  const sizeKb = (f.size / 1024).toFixed(1);
  const url = `/api/v1/debug/screenshots/${encodeURIComponent(f.name)}`;
  const isImg = f.type === "png";
  const thumb = isImg
    ? `<a href="${url}" target="_blank"><img class="debug-thumb" src="${url}" alt="${esc(f.name)}" loading="lazy" /></a>`
    : `<a class="debug-html-link" href="${url}" target="_blank">📄 View HTML dump</a>`;
  return `
    <div class="debug-card">
      ${thumb}
      <div class="debug-card-info">
        <div class="debug-card-name" title="${esc(f.name)}">${esc(f.name)}</div>
        <div class="debug-card-meta">${ts} · ${sizeKb} KB</div>
        <a class="btn btn-ghost btn-sm" href="${url}" target="_blank" download="${esc(f.name)}">⬇ Download</a>
      </div>
    </div>`;
}

document.getElementById("btn-refresh-debug").addEventListener("click", () => loadDebugFiles());

// ── Init ───────────────────────────────────────────────────────────────────

connectWS();
loadBots();
