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
    if (page === "bots")      { showPage("bots"); loadBots(); }
    if (page === "webhooks")  { showPage("webhooks"); loadWebhooks(); }
    if (page === "debug")     { showPage("debug"); loadDebugFiles(); }
    if (page === "search")       { showPage("search"); }
    if (page === "action-items") { showPage("action-items"); loadActionItems(); }
    if (page === "templates")    { showPage("templates"); loadTemplates(); }
    if (page === "analytics")    { showPage("analytics"); loadAnalytics(); }
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
let _wsKeepaliveTimer = null;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  _ws = new WebSocket(`${proto}//${location.host}/api/v1/ws`);

  _ws.onopen = () => {
    _wsRetryDelay = 1000;
    setWsStatus(true);
    // Keep-alive — clear any previous timer first to avoid accumulation on reconnect
    if (_wsKeepaliveTimer) clearInterval(_wsKeepaliveTimer);
    _wsKeepaliveTimer = setInterval(
      () => _ws && _ws.readyState === WebSocket.OPEN && _ws.send("ping"), 25000
    );
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
  // Live transcript entry — append in-place without a full page reload
  if (event === "bot.live_transcript") {
    const detailPage = document.getElementById("page-bot-detail");
    if (detailPage.classList.contains("active") && _currentBotId === data.bot_id) {
      _appendLiveTranscriptEntry(data.entry);
    }
    return;
  }

  // Update stats bar on any other bot event
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

function _appendLiveTranscriptEntry(entry) {
  const body = document.getElementById("transcript-body");
  if (!body) return;

  // If showing the "no transcript yet" empty state, replace it with a list
  let list = body.querySelector(".transcript-list");
  if (!list) {
    body.innerHTML = '<div class="transcript-list"></div>';
    list = body.querySelector(".transcript-list");
  }

  // Append the new entry
  const div = document.createElement("div");
  div.className = "transcript-entry";
  div.dataset.ts = entry.timestamp || 0;
  div.innerHTML =
    `<span class="t-speaker">${esc(entry.speaker)}</span>` +
    `<span class="t-ts">${fmtTs(entry.timestamp)}</span>` +
    `<span class="t-text">${esc(entry.text)}</span>`;
  list.appendChild(div);

  // Update the transcript count heading
  const count = list.querySelectorAll(".transcript-entry").length;
  const heading = document.querySelector("#page-bot-detail h3");
  if (heading && heading.textContent.includes("Transcript")) {
    const span = heading.querySelector("span");
    if (span) span.textContent = `(${count} entries)`;
  }

  // Scroll the new entry into view
  div.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

const PAGE_SIZE = 10;
let _currentFilter = "";
let _currentSearch = "";
let _currentPage   = 0;
let _totalBots     = 0;
let _loadBotsSeq   = 0;  // incremented on each call; stale responses are discarded

// Debounce helper
function _debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function loadBots(filter = _currentFilter, search = _currentSearch, page = _currentPage) {
  _currentFilter = filter;
  _currentSearch = search;
  _currentPage   = page;
  const seq = ++_loadBotsSeq;

  const listEl = document.getElementById("bot-list");
  listEl.innerHTML = '<div class="loading">Loading…</div>';

  try {
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE });
    if (filter) params.set("status", filter);
    if (search) params.set("search", search);
    const data = await apiFetch("GET", `/bot?${params}`);
    if (seq !== _loadBotsSeq) return;  // a newer call superseded this one — discard
    _totalBots = data.count;
    renderBotList(data.results);
    renderPagination();
    _startLiveTimers();
  } catch (e) {
    if (seq !== _loadBotsSeq) return;
    listEl.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }

  await loadStats();
}

function renderPagination() {
  const el = document.getElementById("bot-pagination");
  if (!el) return;
  const totalPages = Math.ceil(_totalBots / PAGE_SIZE);
  if (totalPages <= 1) { el.innerHTML = ""; return; }
  const start = _currentPage * PAGE_SIZE + 1;
  const end   = Math.min((_currentPage + 1) * PAGE_SIZE, _totalBots);
  // Buttons use data-page-dir; clicks are handled via event delegation (no per-render listeners)
  el.innerHTML = `
    <div class="pagination">
      <button class="btn btn-ghost btn-sm" data-page-dir="prev" ${_currentPage === 0 ? "disabled" : ""}>← Prev</button>
      <span class="pagination-info">${start}–${end} of ${_totalBots}</span>
      <button class="btn btn-ghost btn-sm" data-page-dir="next" ${end >= _totalBots ? "disabled" : ""}>Next →</button>
    </div>`;
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

    // Live timer for active calls
    const durationDisplay = b.status === "in_call" && b.started_at
      ? `<span class="live-timer" data-live-start="${b.started_at}">…</span>`
      : (duration !== "—" ? duration : "");

    const stats = [
      b.started_at ? `🕐 ${fmtDate(b.started_at)}` : `📅 ${fmtDate(b.created_at)}`,
      durationDisplay ? `⏱ ${durationDisplay}` : "",
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

    const errorEl = b.status === "error" && b.error_message
      ? `<div class="report-error">⚠ ${esc(b.error_message.length > 120 ? b.error_message.slice(0, 120) + "…" : b.error_message)}</div>`
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
          ${b.status === "in_call"
            ? `<button class="btn btn-end-meeting btn-sm" data-delete-bot="${esc(b.id)}" data-in-call="1" title="End meeting">⏹ End</button>`
            : `<button class="btn btn-danger btn-sm" data-delete-bot="${esc(b.id)}" title="Delete">🗑</button>`
          }
        </div>
      </div>
      <div class="report-url">${esc(b.meeting_url)}</div>
      <div class="report-stats">${stats}</div>
      ${summaryEl}
      ${errorEl}
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
      const isInCall = !!btn.dataset.inCall;
      const msg = isInCall
        ? "Remove the bot from the meeting? It will leave the call and generate the transcript + summary."
        : "Delete this report and all its data?";
      if (!confirm(msg)) return;
      btn.disabled = true;
      try {
        await apiFetch("DELETE", `/bot/${btn.dataset.deleteBot}`);
        showToast(isInCall ? "Bot leaving meeting — summary in progress…" : "Report deleted", "success");
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
}

// Live timer for in-call bots — single interval, updates all [data-live-start] spans
let _liveTimerInterval = null;
function _startLiveTimers() {
  if (_liveTimerInterval) return;
  _liveTimerInterval = setInterval(() => {
    const spans = document.querySelectorAll("[data-live-start]");
    if (!spans.length) return;
    spans.forEach((el) => {
      const secs = Math.floor((Date.now() - new Date(el.dataset.liveStart)) / 1000);
      el.textContent = _fmtSecs(secs) + " ●";
    });
  }, 1000);
}
function _fmtSecs(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${ss}s` : `${ss}s`;
}

async function cancelBot(botId, isInCall) {
  const msg = isInCall
    ? "End the meeting? The bot will leave the call and generate the transcript + summary."
    : "Cancel this bot?";
  if (!confirm(msg)) return;
  try {
    await apiFetch("DELETE", `/bot/${botId}`);
    if (isInCall) {
      showToast("Bot leaving meeting — transcript processing…", "info");
      // Navigate to bot detail so user can see transcript arriving
      showBotDetail(botId);
    } else {
      showToast("Bot cancelled", "info");
      await refreshBotDetail(botId);
    }
    loadBots();
  } catch (e) {
    showToast(e.message, "error");
  }
}

// ── Filter buttons ─────────────────────────────────────────────────────────

document.querySelectorAll(".filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    loadBots(btn.dataset.status || "", _currentSearch, 0);
  });
});

document.getElementById("btn-refresh").addEventListener("click", () =>
  loadBots(_currentFilter, _currentSearch, 0));

// Pagination — single delegated listener on the static container (no per-render binding)
document.getElementById("bot-pagination").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-page-dir]");
  if (!btn || btn.disabled) return;
  const dir = btn.dataset.pageDir;
  if (dir === "prev" && _currentPage > 0)
    loadBots(_currentFilter, _currentSearch, _currentPage - 1);
  else if (dir === "next")
    loadBots(_currentFilter, _currentSearch, _currentPage + 1);
});

// Search input — debounced so we don't fire on every keystroke
const _searchInput = document.getElementById("bot-search");
if (_searchInput) {
  _searchInput.addEventListener("input", _debounce((e) => {
    loadBots(_currentFilter, e.target.value.trim(), 0);
  }, 300));
}

// ── Create Bot ─────────────────────────────────────────────────────────────

// Respond-on-mention toggle — show/hide the response-mode and TTS-provider rows.
// Listeners are attached once; subsequent calls (from re-opening the modal) only
// reset the visual state back to defaults.
let _mentionToggleInited = false;
function _initMentionToggle() {
  const toggle    = document.getElementById("new-bot-respond-mention");
  const modeRow   = document.getElementById("response-mode-row");
  const ttsRow    = document.getElementById("tts-provider-row");
  const ttsHint   = document.getElementById("tts-hint");
  if (!toggle || !modeRow) return;

  const updateTtsRow = () => {
    if (!ttsRow) return;
    const mode = document.querySelector('input[name="mention_response_mode"]:checked')?.value || "text";
    ttsRow.style.display = (toggle.checked && mode !== "text") ? "" : "none";
  };

  const updateModeRow = () => {
    modeRow.style.display = toggle.checked ? "" : "none";
    updateTtsRow();
  };

  if (!_mentionToggleInited) {
    _mentionToggleInited = true;

    toggle.addEventListener("change", updateModeRow);

    // Listen to 'change' on the radio inputs rather than 'click' on the label
    // wrappers.  The browser fires exactly one 'change' event when the user
    // selects a radio (via its label or directly), with no bubbling surprises.
    const modePills = modeRow.querySelectorAll(".mode-pill");
    modeRow.querySelectorAll('input[name="mention_response_mode"]').forEach(radio => {
      radio.addEventListener("change", () => {
        modePills.forEach(p => p.classList.remove("mode-pill-active"));
        const active = modeRow.querySelector(`.mode-pill[data-value="${radio.value}"]`);
        if (active) active.classList.add("mode-pill-active");
        updateTtsRow();
      });
    });

    // TTS provider pills — same pattern
    if (ttsRow) {
      const ttsPills = ttsRow.querySelectorAll(".mode-pill");
      ttsRow.querySelectorAll('input[name="tts_provider"]').forEach(radio => {
        radio.addEventListener("change", () => {
          ttsPills.forEach(p => p.classList.remove("mode-pill-active"));
          const active = ttsRow.querySelector(`.mode-pill[data-value="${radio.value}"]`);
          if (active) active.classList.add("mode-pill-active");
          if (ttsHint) {
            ttsHint.textContent = radio.value === "gemini"
              ? "Uses your configured Gemini API key. More natural voice."
              : "Fast, free, no extra key required.";
          }
        });
      });
    }
  }

  // Reset pills to defaults every time the modal opens
  const modePills = modeRow.querySelectorAll(".mode-pill");
  modePills.forEach(p => p.classList.remove("mode-pill-active"));
  const textPill = modeRow.querySelector('[data-value="text"]');
  if (textPill) { textPill.classList.add("mode-pill-active"); textPill.querySelector("input").checked = true; }
  if (ttsRow) {
    ttsRow.querySelectorAll(".mode-pill").forEach(p => p.classList.remove("mode-pill-active"));
    const edgePill = ttsRow.querySelector('[data-value="edge"]');
    if (edgePill) { edgePill.classList.add("mode-pill-active"); edgePill.querySelector("input").checked = true; }
    if (ttsHint) ttsHint.textContent = "Fast, free, no extra key required.";
  }
  updateModeRow();
}

// Analysis mode picker — highlight selected card + show/hide AI options
function _initModePicker() {
  const picker = document.getElementById("analysis-mode-picker");
  if (!picker) return;
  picker.querySelectorAll(".mode-card").forEach(card => {
    card.addEventListener("click", () => {
      picker.querySelectorAll(".mode-card").forEach(c => c.classList.remove("mode-card-active"));
      card.classList.add("mode-card-active");
      card.querySelector("input[type=radio]").checked = true;
      const aiSection = document.getElementById("ai-options-section");
      if (aiSection) {
        aiSection.classList.toggle("hidden", card.dataset.value === "transcript_only");
      }
    });
  });
}

document.getElementById("btn-new-bot").addEventListener("click", async () => {
  // Reset mode picker to "full" each time the modal opens
  const picker = document.getElementById("analysis-mode-picker");
  if (picker) {
    picker.querySelectorAll(".mode-card").forEach(c => c.classList.remove("mode-card-active"));
    const fullCard = picker.querySelector('[data-value="full"]');
    if (fullCard) {
      fullCard.classList.add("mode-card-active");
      fullCard.querySelector("input[type=radio]").checked = true;
    }
  }
  const aiSection = document.getElementById("ai-options-section");
  if (aiSection) aiSection.classList.remove("hidden");

  _initModePicker();
  _initMentionToggle();
  // Reset live transcription to off each time the modal opens
  const liveTranscCheck = document.getElementById("new-bot-live-transcription");
  if (liveTranscCheck) liveTranscCheck.checked = false;
  openModal("modal-new-bot");
  // Populate template dropdown
  try {
    const tmpls = await apiFetch("GET", "/templates");
    const sel = document.getElementById("new-bot-template");
    if (sel) {
      sel.innerHTML = '<option value="">Default analysis</option>' +
        tmpls.map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
    }
  } catch (_) {}
});

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

  const joinAtInput = document.getElementById("new-bot-join-at");
  const joinAtVal = joinAtInput ? joinAtInput.value : "";
  const emailInput = document.getElementById("new-bot-email");
  const notifyEmail = emailInput ? emailInput.value.trim() : "";
  const templateId = (document.getElementById("new-bot-template")?.value || "").trim();
  const vocabRaw = (document.getElementById("new-bot-vocab")?.value || "").trim();
  const vocabulary = vocabRaw ? vocabRaw.split(",").map(s => s.trim()).filter(Boolean) : null;
  const analysisMode = document.querySelector('input[name="analysis_mode"]:checked')?.value || "full";
  const respondOnMention = document.getElementById("new-bot-respond-mention")?.checked ?? true;
  const mentionResponseMode = document.querySelector('input[name="mention_response_mode"]:checked')?.value || "text";
  const ttsProvider = document.querySelector('input[name="tts_provider"]:checked')?.value || "edge";
  const liveTranscription = document.getElementById("new-bot-live-transcription")?.checked ?? false;
  const body = {
    meeting_url: url,
    bot_name: name,
    analysis_mode: analysisMode,
    respond_on_mention: respondOnMention,
    mention_response_mode: mentionResponseMode,
    tts_provider: ttsProvider,
    live_transcription: liveTranscription,
  };
  if (joinAtVal) body.join_at = new Date(joinAtVal).toISOString();
  if (notifyEmail) body.notify_email = notifyEmail;
  if (analysisMode === "full") {
    if (templateId) body.template_id = templateId;
    if (vocabulary) body.vocabulary = vocabulary;
  }

  try {
    const bot = await apiFetch("POST", "/bot", body);
    closeModal("modal-new-bot");
    const modeLabel = analysisMode === "transcript_only" ? "transcript only" : "full AI analysis";
    if (joinAtVal) {
      showToast(`Bot scheduled for ${new Date(joinAtVal).toLocaleString()} · ${modeLabel}`, "success");
    } else {
      showToast(`Bot deployed · ${modeLabel}`, "success");
    }
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

  if (["scheduled","joining","in_call","call_ended"].includes(bot.status)) {
    const isInCall = bot.status === "in_call";
    const btnLabel = isInCall ? "⏹ End Meeting" : "✕ Cancel";
    badgesEl.insertAdjacentHTML("beforeend",
      `<button class="btn ${isInCall ? "btn-end-meeting" : "btn-danger"} btn-sm" id="btn-cancel-bot">${btnLabel}</button>`);
    document.getElementById("btn-cancel-bot").addEventListener("click", () => cancelBot(bot.id, isInCall));
  }

  // Share link button
  if (bot.share_token) {
    badgesEl.insertAdjacentHTML("beforeend",
      `<button class="btn btn-ghost btn-sm" id="btn-share-bot" title="Copy public share link">🔗 Share</button>`);
    document.getElementById("btn-share-bot").addEventListener("click", () => {
      const url = `${location.origin}/share/${bot.share_token}`;
      navigator.clipboard.writeText(url).then(
        () => showToast("Share link copied!", "success"),
        () => showToast("Copy failed", "error"),
      );
    });
  }

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
      <div class="meta-item"><div class="meta-label">Analysis Mode</div><div class="meta-value">${bot.analysis_mode === "transcript_only" ? '<span style="color:var(--text-muted);font-weight:500">Transcript Only</span>' : '<span style="color:var(--accent);font-weight:600">✦ Full AI</span>'}</div></div>
      <div class="meta-item"><div class="meta-label">Meeting URL</div><div class="meta-value" style="font-family:var(--font-mono);font-size:0.75rem;color:var(--text-muted)">${esc(bot.meeting_url.slice(0, 40))}${bot.meeting_url.length > 40 ? "…" : ""}</div></div>
    </div>

    <!-- Participants + Speaker Stats -->
    ${(bot.participants || []).length || (bot.speaker_stats || []).length ? `
    <div class="section-card">
      <div class="section-header"><h3>Participants</h3></div>
      <div class="pill-list" style="padding:0.25rem 0 0.5rem">
        ${(bot.participants || []).map((p) => `<span class="pill">${esc(p)}</span>`).join("")}
      </div>
      ${(bot.speaker_stats || []).length ? `
      <div class="speaker-stats">
        ${(bot.speaker_stats || []).map((s) => `
          <div class="speaker-stat-row">
            <div class="speaker-stat-name">${esc(s.name)}</div>
            <div class="speaker-stat-bar-wrap">
              <div class="speaker-stat-bar" style="width:${s.talk_pct}%"></div>
            </div>
            <div class="speaker-stat-pct">${s.talk_pct}%</div>
            <div class="speaker-stat-time">${_fmtSecs(s.talk_time_s)}</div>
          </div>`).join("")}
      </div>` : ""}
    </div>` : ""}

    <!-- Smart Chapters -->
    ${(bot.chapters || []).length ? `
    <div class="section-card">
      <div class="section-header"><h3>Chapters</h3></div>
      <div class="chapters-list">
        ${(bot.chapters || []).map((c, i) => `
          <div class="chapter-item" data-ts="${c.start_time || 0}">
            <div class="chapter-num">${i + 1}</div>
            <div class="chapter-body">
              <div class="chapter-title">${esc(c.title)}</div>
              <div class="chapter-meta">${fmtTs(c.start_time || 0)} · ${esc(c.summary || "")}</div>
            </div>
          </div>`).join("")}
      </div>
    </div>` : ""}

    <!-- Recording download -->
    ${bot.recording_path ? `
    <div class="section-card">
      <div class="section-header"><h3>Recording</h3></div>
      <a class="btn btn-ghost" href="/api/v1/bot/${esc(bot.id)}/recording" download>⬇ Download Audio (WAV)</a>
    </div>` : ""}

    <!-- Transcript section -->
    <div class="section-card">
      <div class="section-header">
        <h3>Transcript <span style="color:var(--text-muted);font-size:0.8rem;font-weight:400">(${(bot.transcript||[]).length} entries)</span></h3>
        <div style="display:flex;gap:0.5rem;flex-wrap:wrap;align-items:center">
          ${(bot.transcript||[]).length ? `<button class="btn btn-icon" id="btn-copy-transcript" title="Copy transcript">📋 Copy</button>` : ""}
          ${(bot.transcript||[]).length ? `<button class="btn btn-icon" id="btn-export-csv" title="Download CSV">⬇ CSV</button>` : ""}
          ${(bot.transcript||[]).length ? `<button class="btn btn-icon" id="btn-export-md" title="Download Markdown">⬇ MD</button>` : ""}
          ${(bot.transcript||[]).length && bot.analysis ? `<button class="btn btn-icon" id="btn-export-json" title="Download JSON">⬇ JSON</button>` : ""}
          ${(bot.transcript||[]).length ? `<button class="btn btn-sm btn-primary" id="btn-reanalyse">✨ Analyse with Claude</button>` : ""}
        </div>
      </div>
      ${(bot.transcript||[]).length ? `
      <div class="transcript-toolbar">
        <select class="input btn-sm" id="transcript-speaker-filter" style="width:auto">
          <option value="">All speakers</option>
          ${[...new Set((bot.transcript||[]).map(e => e.speaker))].map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}
        </select>
        <input class="input btn-sm" id="transcript-search" type="search" placeholder="Search…" style="width:160px">
      </div>` : ""}
      <div id="transcript-body">${renderTranscript(bot.transcript)}</div>
    </div>

    <!-- Analysis section -->
    ${bot.analysis_mode === "transcript_only"
      ? `<div class="section-card" id="analysis-section">
           <div class="section-header">
             <h3>AI Analysis</h3>
             <span class="badge badge-ready" style="text-transform:none;font-size:0.75rem">Transcript-only mode</span>
           </div>
           <div class="empty" style="padding:1.25rem 0">
             This bot was configured to deliver transcript only — AI analysis was skipped.
             <div class="empty-action">
               <button class="btn btn-primary btn-sm" id="btn-run-analysis">Run AI Analysis Now</button>
             </div>
           </div>
         </div>`
      : `<div class="section-card" id="analysis-section">
           <div class="section-header">
             <h3>AI Analysis</h3>
             ${bot.analysis ? `<span class="sentiment-badge sentiment-${esc(bot.analysis.sentiment || 'neutral')}">${esc(bot.analysis.sentiment || 'neutral')}</span>` : ""}
           </div>
           ${renderAnalysis(bot.analysis)}
         </div>`
    }

    <!-- Ask Anything -->
    ${bot.transcript?.length ? `
    <div class="section-card" id="ask-section">
      <div class="section-header"><h3>Ask About This Meeting</h3></div>
      <div class="ask-row">
        <input class="input" id="ask-input" placeholder="e.g. What were the main decisions?" style="flex:1" />
        <button class="btn btn-primary" id="btn-ask">Ask</button>
      </div>
      <div id="ask-answer" class="ask-answer hidden"></div>
    </div>` : ""}

    <!-- Highlights -->
    <div class="section-card" id="highlights-section">
      <div class="section-header"><h3>Highlights</h3></div>
      <div id="highlights-list"><div class="empty" style="padding:0.5rem 0">No highlights yet — click the bookmark icon on a transcript entry</div></div>
    </div>`;

  // Wire up transcript toolbar
  const copyBtn = document.getElementById("btn-copy-transcript");
  if (copyBtn) copyBtn.addEventListener("click", () => copyTranscript(bot.transcript));

  const exportCsvBtn = document.getElementById("btn-export-csv");
  if (exportCsvBtn) exportCsvBtn.addEventListener("click", () => exportTranscriptCsv(bot));

  const exportMdBtn = document.getElementById("btn-export-md");
  if (exportMdBtn) exportMdBtn.addEventListener("click", () => exportTranscriptMd(bot));

  const exportBtn = document.getElementById("btn-export-json");
  if (exportBtn) exportBtn.addEventListener("click", () => exportJson(bot));

  const analyseBtn = document.getElementById("btn-reanalyse");
  if (analyseBtn) analyseBtn.addEventListener("click", () => reanalyse(bot.id));

  // "Run AI Analysis Now" button shown when bot was created in transcript_only mode
  const runAnalysisBtn = document.getElementById("btn-run-analysis");
  if (runAnalysisBtn) runAnalysisBtn.addEventListener("click", () => reanalyse(bot.id));

  // Transcript filter + search
  function _filterTranscript() {
    const speaker = document.getElementById("transcript-speaker-filter")?.value || "";
    const q = (document.getElementById("transcript-search")?.value || "").toLowerCase();
    const filtered = (bot.transcript || []).filter((e) =>
      (!speaker || e.speaker === speaker) &&
      (!q || e.text.toLowerCase().includes(q) || e.speaker.toLowerCase().includes(q))
    );
    const body = document.getElementById("transcript-body");
    if (body) body.innerHTML = renderTranscript(filtered);
  }
  document.getElementById("transcript-speaker-filter")?.addEventListener("change", _filterTranscript);
  document.getElementById("transcript-search")?.addEventListener("input", _filterTranscript);

  // Chapters: click to jump to timestamp in transcript
  document.querySelectorAll(".chapter-item[data-ts]").forEach((el) => {
    el.addEventListener("click", () => {
      const ts = parseFloat(el.dataset.ts);
      const tFilter = document.getElementById("transcript-search");
      if (tFilter) { tFilter.value = ""; _filterTranscript(); }
      // Scroll to nearest transcript entry
      setTimeout(() => {
        const entries = document.querySelectorAll(".transcript-entry");
        let closest = null, minDiff = Infinity;
        entries.forEach((row) => {
          const rowTs = parseFloat(row.dataset.ts || "0");
          const diff = Math.abs(rowTs - ts);
          if (diff < minDiff) { minDiff = diff; closest = row; }
        });
        closest?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 50);
    });
  });

  // Ask Anything
  const askBtn = document.getElementById("btn-ask");
  if (askBtn) {
    askBtn.addEventListener("click", async () => {
      const q = document.getElementById("ask-input")?.value.trim();
      if (!q) return;
      askBtn.disabled = true;
      askBtn.textContent = "Thinking…";
      const answerEl = document.getElementById("ask-answer");
      if (answerEl) { answerEl.classList.add("hidden"); answerEl.textContent = ""; }
      try {
        const res = await apiFetch("POST", `/bot/${bot.id}/ask`, { question: q });
        if (answerEl) {
          answerEl.textContent = res.answer;
          answerEl.classList.remove("hidden");
        }
      } catch (e) {
        showToast(e.message, "error");
      } finally {
        askBtn.disabled = false;
        askBtn.textContent = "Ask";
      }
    });
    document.getElementById("ask-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") askBtn.click();
    });
  }

  // Highlights: add bookmark icon to transcript entries and load existing
  _loadHighlights(bot.id);
  _wireHighlightButtons(bot.id);
}

async function _loadHighlights(botId) {
  const listEl = document.getElementById("highlights-list");
  if (!listEl) return;
  try {
    const highlights = await apiFetch("GET", `/bot/${botId}/highlight`);
    if (!highlights.length) {
      listEl.innerHTML = '<div class="empty" style="padding:0.5rem 0">No highlights yet — click the 🔖 icon on a transcript entry</div>';
      return;
    }
    listEl.innerHTML = highlights.map((h) => `
      <div class="highlight-row" data-hid="${esc(h.id)}">
        <div class="highlight-ts">${fmtTs(h.timestamp)}</div>
        <div class="highlight-body">
          <div class="highlight-speaker">${esc(h.speaker)}</div>
          <div class="highlight-text">${esc(h.text_snippet)}</div>
          ${h.comment ? `<div class="highlight-comment">${esc(h.comment)}</div>` : ""}
        </div>
        <button class="btn btn-ghost btn-sm" data-del-hl="${esc(h.id)}" title="Remove highlight">✕</button>
      </div>`).join("");
    listEl.querySelectorAll("[data-del-hl]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await apiFetch("DELETE", `/bot/highlight/${btn.dataset.delHl}`);
        _loadHighlights(botId);
      });
    });
  } catch (_) {}
}

function _wireHighlightButtons(botId) {
  // Add bookmark button to each transcript entry
  document.querySelectorAll(".transcript-entry").forEach((row) => {
    if (row.querySelector(".hl-btn")) return;
    const hlBtn = document.createElement("button");
    hlBtn.className = "btn btn-icon hl-btn";
    hlBtn.title = "Bookmark";
    hlBtn.textContent = "🔖";
    hlBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const speaker = row.querySelector(".t-speaker")?.textContent || "";
      const text = row.querySelector(".t-text")?.textContent || "";
      const tsStr = row.querySelector(".t-ts")?.textContent || "00:00";
      const [mm, ss] = tsStr.split(":").map(Number);
      const timestamp = (mm || 0) * 60 + (ss || 0);
      const comment = prompt("Add a comment (optional):", "") ?? null;
      try {
        await apiFetch("POST", `/bot/${botId}/highlight`, { timestamp, text_snippet: text, speaker, comment: comment || null });
        showToast("Highlight saved", "success");
        _loadHighlights(botId);
      } catch (err) {
        showToast(err.message, "error");
      }
    });
    row.appendChild(hlBtn);
  });
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
      <div class="transcript-entry" data-ts="${e.timestamp || 0}">
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

function _downloadBlob(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function exportTranscriptCsv(bot) {
  const rows = [["speaker", "timestamp", "text"]];
  (bot.transcript || []).forEach((e) => rows.push([e.speaker, fmtTs(e.timestamp), e.text]));
  const csv = rows.map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
  _downloadBlob(csv, `transcript-${bot.id.slice(0, 8)}.csv`, "text/csv");
  showToast("Exported as CSV", "success");
}

function exportTranscriptMd(bot) {
  const lines = [`# Meeting Transcript\n`, `**Bot:** ${bot.bot_name}  `, `**URL:** ${bot.meeting_url}  `, `**Date:** ${fmtDate(bot.started_at || bot.created_at)}\n`];
  (bot.transcript || []).forEach((e) => lines.push(`**${e.speaker}** (${fmtTs(e.timestamp)}): ${e.text}\n`));
  _downloadBlob(lines.join("\n"), `transcript-${bot.id.slice(0, 8)}.md`, "text/markdown");
  showToast("Exported as Markdown", "success");
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
        <button class="btn btn-ghost btn-sm" data-test-wh="${esc(wh.id)}" title="Send test delivery">Test</button>
        <button class="btn btn-danger btn-sm" data-del-wh="${esc(wh.id)}" title="Delete webhook">🗑</button>
      </div>`).join("");

    listEl.querySelectorAll("[data-test-wh]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "Sending…";
        try {
          const res = await apiFetch("POST", `/webhook/${btn.dataset.testWh}/test`);
          showToast(`Test delivered — HTTP ${res.status_code}`, res.status_code < 400 ? "success" : "error");
        } catch (e) {
          showToast(`Test failed: ${e.message}`, "error");
        } finally {
          btn.disabled = false;
          btn.textContent = "Test";
        }
      });
    });

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

// ── Search ─────────────────────────────────────────────────────────────────

async function runSearch() {
  const q = document.getElementById("search-input")?.value.trim();
  const resultsEl = document.getElementById("search-results");
  if (!q || q.length < 2) {
    if (resultsEl) resultsEl.innerHTML = '<div class="empty">Enter at least 2 characters to search</div>';
    return;
  }
  if (resultsEl) resultsEl.innerHTML = '<div class="loading">Searching…</div>';
  try {
    const data = await apiFetch("GET", `/search?q=${encodeURIComponent(q)}`);
    if (!data.results.length) {
      resultsEl.innerHTML = `<div class="empty">No results for <em>${esc(q)}</em></div>`;
      return;
    }
    resultsEl.innerHTML = `<div class="search-count">${data.total} meeting${data.total !== 1 ? "s" : ""} matched</div>` +
      data.results.map((r) => `
        <div class="search-result-card" data-id="${esc(r.bot_id)}">
          <div class="search-result-header">
            <span>${platformIcon(r.meeting_platform)}</span>
            <strong>${esc(r.bot_name)}</strong>
            <span class="search-result-date">${r.started_at ? fmtDate(r.started_at) : "—"}</span>
            <span class="search-result-count">${r.match_count} match${r.match_count !== 1 ? "es" : ""}</span>
          </div>
          <div class="search-result-url">${esc(r.meeting_url)}</div>
          <div class="search-snippets">
            ${r.snippets.map((s) => `
              <div class="search-snippet">
                <span class="t-ts">${fmtTs(s.timestamp)}</span>
                <span class="t-speaker">${esc(s.speaker)}</span>
                <span class="t-text">${esc(s.text).replace(
                  new RegExp(esc(q), "gi"),
                  (m) => `<mark>${m}</mark>`
                )}</span>
              </div>`).join("")}
          </div>
        </div>`).join("");

    resultsEl.querySelectorAll(".search-result-card").forEach((card) => {
      card.addEventListener("click", () => showBotDetail(card.dataset.id));
    });
  } catch (e) {
    resultsEl.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

document.getElementById("btn-search")?.addEventListener("click", runSearch);
document.getElementById("search-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});

// ── Analytics ──────────────────────────────────────────────────────────────

async function loadAnalytics() {
  const el = document.getElementById("analytics-content");
  if (!el) return;
  el.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const d = await apiFetch("GET", "/analytics");
    el.innerHTML = `
      <!-- Summary cards -->
      <div class="analytics-cards">
        <div class="stat-card">
          <div class="stat-value">${d.total_meetings}</div>
          <div class="stat-label">Total Meetings</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${d.avg_duration_fmt}</div>
          <div class="stat-label">Avg Duration</div>
        </div>
        <div class="stat-card positive">
          <div class="stat-value">${d.sentiment_distribution.positive}</div>
          <div class="stat-label">Positive</div>
        </div>
        <div class="stat-card negative">
          <div class="stat-value">${d.sentiment_distribution.negative}</div>
          <div class="stat-label">Negative</div>
        </div>
      </div>

      <div class="analytics-grid">
        <!-- Meetings per day chart -->
        <div class="card analytics-chart-card">
          <h3>Meetings (last 30 days)</h3>
          ${_renderBarChart(d.meetings_per_day.map((r) => ({ label: r.date.slice(5), value: r.count })))}
        </div>

        <!-- Top topics -->
        <div class="card">
          <h3>Top Topics</h3>
          ${d.top_topics.length
            ? d.top_topics.map((t) => `
              <div class="analytics-topic-row">
                <span class="analytics-topic-label">${esc(t.topic)}</span>
                <div class="speaker-stat-bar-wrap" style="flex:1">
                  <div class="speaker-stat-bar" style="width:${Math.round(t.count / d.top_topics[0].count * 100)}%"></div>
                </div>
                <span class="analytics-topic-count">${t.count}</span>
              </div>`).join("")
            : '<div class="empty">No topics yet</div>'}
        </div>

        <!-- Platform breakdown -->
        <div class="card">
          <h3>By Platform</h3>
          ${Object.entries(d.platform_breakdown).length
            ? Object.entries(d.platform_breakdown).sort((a,b)=>b[1]-a[1]).map(([p, c]) => `
              <div class="analytics-topic-row">
                <span class="analytics-topic-label">${platformIcon(p)} ${esc(p.replace(/_/g," "))}</span>
                <div class="speaker-stat-bar-wrap" style="flex:1">
                  <div class="speaker-stat-bar" style="width:${Math.round(c / d.total_meetings * 100)}%"></div>
                </div>
                <span class="analytics-topic-count">${c}</span>
              </div>`).join("")
            : '<div class="empty">No data yet</div>'}
        </div>

        <!-- Top participants -->
        <div class="card">
          <h3>Top Participants</h3>
          ${d.top_participants.length
            ? d.top_participants.map((p) => `
              <div class="analytics-topic-row">
                <span class="analytics-topic-label">${esc(p.name)}</span>
                <div class="speaker-stat-bar-wrap" style="flex:1">
                  <div class="speaker-stat-bar" style="width:${Math.round(p.meetings / d.top_participants[0].meetings * 100)}%"></div>
                </div>
                <span class="analytics-topic-count">${p.meetings}</span>
              </div>`).join("")
            : '<div class="empty">No data yet</div>'}
        </div>
      </div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty">Error loading analytics: ${esc(e.message)}</div>`;
  }
}

function _renderBarChart(items) {
  if (!items.length) return '<div class="empty">No data</div>';
  const max = Math.max(...items.map((i) => i.value), 1);
  return `<div class="bar-chart">
    ${items.map((i) => `
      <div class="bar-chart-col">
        <div class="bar-chart-bar" style="height:${Math.round(i.value / max * 100)}%" title="${i.value}">
          ${i.value > 0 ? `<span class="bar-chart-val">${i.value}</span>` : ""}
        </div>
        <div class="bar-chart-label">${esc(i.label)}</div>
      </div>`).join("")}
  </div>`;
}

document.getElementById("btn-refresh-analytics")?.addEventListener("click", loadAnalytics);

// ── Action Items ─────────────────────────────────────────────────────────

async function loadActionItems() {
  const listEl = document.getElementById("action-items-list");
  const statsEl = document.getElementById("ai-stats");
  listEl.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const doneFilter = document.getElementById("ai-filter-done")?.value ?? "";
    const assignee = document.getElementById("ai-filter-assignee")?.value.trim() ?? "";
    const params = new URLSearchParams({ limit: 200 });
    if (doneFilter !== "") params.set("done", doneFilter);
    if (assignee) params.set("assignee", assignee);
    const items = await apiFetch("GET", `/action-items?${params}`);

    // Stats
    const stats = await apiFetch("GET", "/action-items/stats");
    if (statsEl) statsEl.innerHTML = `
      <div class="stat-card"><div class="stat-value">${stats.total}</div><div class="stat-label">Total</div></div>
      <div class="stat-card"><div class="stat-value">${stats.pending}</div><div class="stat-label">Pending</div></div>
      <div class="stat-card"><div class="stat-value">${stats.done}</div><div class="stat-label">Done</div></div>`;

    if (!items.length) {
      listEl.innerHTML = '<div class="empty-state">No action items found.</div>';
      return;
    }

    listEl.innerHTML = `<table class="ai-table">
      <thead><tr><th></th><th>Task</th><th>Assignee</th><th>Due Date</th><th>Meeting</th><th>Date</th></tr></thead>
      <tbody>${items.map(item => `
        <tr class="ai-row${item.done ? " ai-done" : ""}" data-ai-id="${esc(item.id)}">
          <td><input type="checkbox" class="ai-checkbox" ${item.done ? "checked" : ""}></td>
          <td class="ai-task">${esc(item.task)}</td>
          <td><span class="ai-assignee">${esc(item.assignee || "—")}</span></td>
          <td>${esc(item.due_date || "—")}</td>
          <td><span class="chip chip-sm">${esc(item.meeting_platform || "")}</span> <small>${esc(item.bot_name || "")}</small></td>
          <td><small>${fmtDate(item.created_at)}</small></td>
        </tr>`).join("")}
      </tbody>
    </table>`;

    // Wire checkboxes
    listEl.querySelectorAll(".ai-checkbox").forEach(cb => {
      cb.addEventListener("change", async () => {
        const row = cb.closest("[data-ai-id]");
        const id = row.dataset.aiId;
        try {
          await apiFetch("PATCH", `/action-items/${id}`, { done: cb.checked });
          row.classList.toggle("ai-done", cb.checked);
        } catch (e) {
          showToast(e.message, "error");
          cb.checked = !cb.checked;
        }
      });
    });
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state">Error: ${esc(e.message)}</div>`;
  }
}

document.getElementById("btn-refresh-ai")?.addEventListener("click", loadActionItems);
document.getElementById("ai-filter-done")?.addEventListener("change", loadActionItems);
document.getElementById("ai-filter-assignee")?.addEventListener("input", _debounce(loadActionItems, 400));

// ── Templates ─────────────────────────────────────────────────────────────

async function loadTemplates() {
  const gridEl = document.getElementById("templates-list");
  if (!gridEl) return;
  gridEl.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const tmpls = await apiFetch("GET", "/templates");
    if (!tmpls.length) {
      gridEl.innerHTML = '<div class="empty-state">No templates yet.</div>';
      return;
    }
    gridEl.innerHTML = tmpls.map(t => `
      <div class="template-card${t.id.startsWith("seed-") ? " template-seed" : ""}" data-tmpl-id="${esc(t.id)}">
        <div class="template-header">
          <span class="template-name">${esc(t.name)}</span>
          ${t.id.startsWith("seed-") ? '<span class="chip chip-sm">Built-in</span>' : `<button class="btn btn-ghost btn-sm tmpl-del-btn" data-del-tmpl="${esc(t.id)}">Delete</button>`}
        </div>
        ${t.description ? `<p class="template-desc">${esc(t.description)}</p>` : ""}
        ${t.prompt_override ? `<pre class="template-prompt">${esc(t.prompt_override.slice(0, 200))}${t.prompt_override.length > 200 ? "…" : ""}</pre>` : '<p class="hint">Uses default analysis prompt.</p>'}
      </div>`).join("");

    gridEl.querySelectorAll("[data-del-tmpl]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm(`Delete template "${btn.closest("[data-tmpl-id]").querySelector(".template-name").textContent}"?`)) return;
        try {
          await apiFetch("DELETE", `/templates/${btn.dataset.delTmpl}`);
          showToast("Template deleted", "success");
          loadTemplates();
        } catch (e) { showToast(e.message, "error"); }
      });
    });
  } catch (e) {
    gridEl.innerHTML = `<div class="empty-state">Error: ${esc(e.message)}</div>`;
  }
}

document.getElementById("btn-new-template")?.addEventListener("click", () => openModal("modal-new-template"));

document.getElementById("btn-create-template")?.addEventListener("click", async () => {
  const name = document.getElementById("new-tmpl-name")?.value.trim();
  if (!name) { showToast("Template name is required", "error"); return; }
  const desc = document.getElementById("new-tmpl-desc")?.value.trim();
  const prompt = document.getElementById("new-tmpl-prompt")?.value.trim();
  try {
    await apiFetch("POST", "/templates", { name, description: desc || null, prompt_override: prompt || null });
    closeModal("modal-new-template");
    showToast("Template created", "success");
    loadTemplates();
  } catch (e) { showToast(e.message, "error"); }
});

// ── Init ───────────────────────────────────────────────────────────────────

connectWS();
loadBots();
