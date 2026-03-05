/* ── MeetingBot Dashboard ──────────────────────────────────────────────────── */

const API = "/api/v1";

// ── Utilities ──────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

let toastTimer;
function showToast(msg, type = "success") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function fmtDuration(start, end) {
  if (!start || !end) return "—";
  const secs = Math.round((new Date(end) - new Date(start)) / 1000);
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}m ${s}s`;
}

function fmtTs(secs) {
  const m = Math.floor(secs / 60).toString().padStart(2, "0");
  const s = Math.floor(secs % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

const PLATFORM_ICONS = {
  zoom: "🔵",
  google_meet: "🟢",
  microsoft_teams: "🟣",
  webex: "🔷",
  whereby: "🟠",
  unknown: "🤖",
};

function platformIcon(p) {
  return PLATFORM_ICONS[p] || "🤖";
}

function statusBadge(status) {
  return `<span class="badge badge-${status}">${status.replace("_", " ")}</span>`;
}

// ── Routing ────────────────────────────────────────────────────────────────

function showPage(id) {
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  document.getElementById("page-" + id).classList.add("active");
  document.querySelectorAll(".nav-item").forEach((n) => {
    n.classList.toggle("active", n.dataset.page === id);
  });
}

document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
  el.addEventListener("click", (e) => {
    if (el.getAttribute("target") === "_blank") return;
    e.preventDefault();
    const page = el.dataset.page;
    if (page === "bots") {
      showPage("bots");
      loadBots();
    } else if (page === "webhooks") {
      showPage("webhooks");
      loadWebhooks();
    }
  });
});

// ── Modals ─────────────────────────────────────────────────────────────────

function openModal(id) {
  document.getElementById(id).classList.remove("hidden");
}
function closeModal(id) {
  document.getElementById(id).classList.add("hidden");
}

document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => closeModal(btn.dataset.close));
});
document.querySelectorAll(".modal-backdrop").forEach((bd) => {
  bd.addEventListener("click", (e) => {
    if (e.target === bd) closeModal(bd.id);
  });
});

// ── Bots ───────────────────────────────────────────────────────────────────

let _bots = [];
let _refreshInterval = null;

async function loadBots(filter = "") {
  const listEl = document.getElementById("bot-list");
  listEl.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const qs = filter ? `?status=${filter}` : "";
    const data = await api("GET", `/bot${qs}`);
    _bots = data.results;
    renderBots(_bots, data.count);
  } catch (e) {
    listEl.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

function renderBots(bots, total) {
  // Stats
  const active = bots.filter((b) => ["joining", "in_call"].includes(b.status)).length;
  const done = bots.filter((b) => b.status === "done").length;
  const err = bots.filter((b) => b.status === "error").length;
  document.getElementById("stat-total").textContent = total ?? bots.length;
  document.getElementById("stat-active").textContent = active;
  document.getElementById("stat-done").textContent = done;
  document.getElementById("stat-error").textContent = err;

  const listEl = document.getElementById("bot-list");
  if (!bots.length) {
    listEl.innerHTML = '<div class="empty">No bots yet. Create one to get started!</div>';
    return;
  }

  listEl.innerHTML = bots
    .map(
      (b) => `
    <div class="bot-row" data-id="${b.id}">
      <div class="bot-platform-icon">${platformIcon(b.meeting_platform)}</div>
      <div class="bot-info">
        <div class="bot-name">${escHtml(b.bot_name)}</div>
        <div class="bot-url">${escHtml(b.meeting_url)}</div>
        <div class="bot-meta">Created ${fmtDate(b.created_at)} · ${b.meeting_platform}</div>
      </div>
      <div class="bot-actions">
        ${statusBadge(b.status)}
        <button class="btn btn-danger" data-delete="${b.id}" title="Delete bot">🗑</button>
      </div>
    </div>`
    )
    .join("");

  listEl.querySelectorAll(".bot-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("[data-delete]")) return;
      showBotDetail(row.dataset.id);
    });
  });

  listEl.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this bot?")) return;
      try {
        await api("DELETE", `/bot/${btn.dataset.delete}`);
        showToast("Bot deleted");
        loadBots();
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  });
}

// Auto-refresh if any bot is in-flight
function scheduleRefresh() {
  clearInterval(_refreshInterval);
  const hasActive = _bots.some((b) =>
    ["ready", "joining", "in_call", "call_ended"].includes(b.status)
  );
  if (hasActive) {
    _refreshInterval = setInterval(() => loadBots(), 4000);
  }
}

// ── Bot Detail ─────────────────────────────────────────────────────────────

async function showBotDetail(botId) {
  showPage("bot-detail");
  const headerEl = document.getElementById("bot-detail-header");
  const contentEl = document.getElementById("bot-detail-content");
  contentEl.innerHTML = '<div class="loading">Loading…</div>';

  let bot;
  try {
    bot = await api("GET", `/bot/${botId}`);
  } catch (e) {
    contentEl.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
    return;
  }

  headerEl.innerHTML = `
    <div style="display:flex;align-items:center;gap:0.75rem">
      <span style="font-size:1.5rem">${platformIcon(bot.meeting_platform)}</span>
      <div>
        <div style="font-weight:700;font-size:1.1rem">${escHtml(bot.bot_name)}</div>
        <div style="color:var(--text-muted);font-size:0.8rem">${escHtml(bot.meeting_url)}</div>
      </div>
      ${statusBadge(bot.status)}
    </div>`;

  // Re-analyse button
  const canAnalyse = bot.transcript && bot.transcript.length > 0;

  contentEl.innerHTML = `
    <div class="detail-meta">
      <div class="meta-item"><div class="meta-label">Platform</div><div class="meta-value">${bot.meeting_platform}</div></div>
      <div class="meta-item"><div class="meta-label">Status</div><div class="meta-value">${bot.status}</div></div>
      <div class="meta-item"><div class="meta-label">Started</div><div class="meta-value">${fmtDate(bot.started_at)}</div></div>
      <div class="meta-item"><div class="meta-label">Duration</div><div class="meta-value">${fmtDuration(bot.started_at, bot.ended_at)}</div></div>
      <div class="meta-item"><div class="meta-label">Created</div><div class="meta-value">${fmtDate(bot.created_at)}</div></div>
      <div class="meta-item"><div class="meta-label">Entries</div><div class="meta-value">${(bot.transcript || []).length}</div></div>
    </div>

    ${bot.error_message ? `<div class="card" style="border-color:var(--error);margin-bottom:1.25rem;color:var(--error)">⚠ ${escHtml(bot.error_message)}</div>` : ""}

    <!-- Transcript -->
    <div class="card" style="margin-bottom:1.25rem">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
        <h3>Transcript</h3>
        ${canAnalyse ? `<button class="btn btn-sm btn-primary" id="btn-reanalyse">✨ (Re-)Analyse with Claude</button>` : ""}
      </div>
      ${renderTranscript(bot.transcript)}
    </div>

    <!-- Analysis -->
    <div class="card" id="analysis-card">
      <h3 style="margin-bottom:1rem">AI Analysis</h3>
      ${renderAnalysis(bot.analysis)}
    </div>`;

  if (canAnalyse) {
    document.getElementById("btn-reanalyse").addEventListener("click", async () => {
      const btn = document.getElementById("btn-reanalyse");
      btn.disabled = true;
      btn.textContent = "Analysing…";
      try {
        const analysis = await api("POST", `/bot/${botId}/analyze`);
        document.getElementById("analysis-card").innerHTML = `<h3 style="margin-bottom:1rem">AI Analysis</h3>${renderAnalysis(analysis)}`;
        showToast("Analysis complete!");
      } catch (e) {
        showToast(e.message, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "✨ (Re-)Analyse with Claude";
      }
    });
  }

  // Poll if bot is still active
  if (["ready", "joining", "in_call", "call_ended"].includes(bot.status)) {
    const poll = setInterval(async () => {
      const fresh = await api("GET", `/bot/${botId}`).catch(() => null);
      if (!fresh) { clearInterval(poll); return; }
      if (!["ready", "joining", "in_call", "call_ended"].includes(fresh.status)) {
        clearInterval(poll);
        showBotDetail(botId);  // reload full detail
      } else {
        // Update status badge
        headerEl.querySelector(".badge").outerHTML = statusBadge(fresh.status);
      }
    }, 3000);
  }
}

function renderTranscript(transcript) {
  if (!transcript || !transcript.length) {
    return '<div class="empty" style="padding:1.5rem">No transcript yet</div>';
  }
  const rows = transcript
    .map(
      (e) => `
    <div class="transcript-entry">
      <span class="t-speaker">${escHtml(e.speaker)}</span>
      <span class="t-ts">${fmtTs(e.timestamp)}</span>
      <span class="t-text">${escHtml(e.text)}</span>
    </div>`
    )
    .join("");
  return `<div class="transcript-list">${rows}</div>`;
}

function renderAnalysis(analysis) {
  if (!analysis) {
    return '<div class="empty" style="padding:1.5rem">No analysis yet — run the bot to generate one</div>';
  }

  const items =
    (analysis.action_items || [])
      .map(
        (a) => `
    <div class="action-item-row">
      <span style="font-size:1rem">☑</span>
      <div class="action-task">${escHtml(a.task)}</div>
      ${a.assignee ? `<div class="action-assignee">@${escHtml(a.assignee)}</div>` : ""}
    </div>`
      )
      .join("") || '<div class="empty" style="padding:0.5rem 0">No action items</div>';

  const sentimentColor = { positive: "#22c55e", neutral: "#f59e0b", negative: "#ef4444" }[analysis.sentiment] || "var(--text-muted)";

  return `
    <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:1rem">
      <span style="font-size:0.8rem;color:var(--text-muted)">Sentiment:</span>
      <span style="font-weight:700;color:${sentimentColor}">${analysis.sentiment || "—"}</span>
    </div>

    <div class="analysis-section">
      <h3>Summary</h3>
      <div class="analysis-summary">${escHtml(analysis.summary || "")}</div>
    </div>

    <div class="analysis-section">
      <h3>Topics</h3>
      <div class="pill-list">
        ${(analysis.topics || []).map((t) => `<span class="pill">${escHtml(t)}</span>`).join("") || "—"}
      </div>
    </div>

    <div class="analysis-section">
      <h3>Key Points</h3>
      <ul style="padding-left:1.25rem;display:flex;flex-direction:column;gap:0.4rem">
        ${(analysis.key_points || []).map((p) => `<li style="font-size:0.875rem">${escHtml(p)}</li>`).join("") || "<li style='color:var(--text-muted)'>—</li>"}
      </ul>
    </div>

    <div class="analysis-section">
      <h3>Action Items</h3>
      ${items}
    </div>

    <div class="analysis-section">
      <h3>Decisions</h3>
      <ul style="padding-left:1.25rem;display:flex;flex-direction:column;gap:0.4rem">
        ${(analysis.decisions || []).map((d) => `<li style="font-size:0.875rem">${escHtml(d)}</li>`).join("") || "<li style='color:var(--text-muted)'>—</li>"}
      </ul>
    </div>

    <div class="analysis-section">
      <h3>Next Steps</h3>
      <ul style="padding-left:1.25rem;display:flex;flex-direction:column;gap:0.4rem">
        ${(analysis.next_steps || []).map((s) => `<li style="font-size:0.875rem">${escHtml(s)}</li>`).join("") || "<li style='color:var(--text-muted)'>—</li>"}
      </ul>
    </div>`;
}

// ── Create Bot ─────────────────────────────────────────────────────────────

document.getElementById("btn-new-bot").addEventListener("click", () => openModal("modal-new-bot"));

document.getElementById("btn-create-bot").addEventListener("click", async () => {
  const url = document.getElementById("new-bot-url").value.trim();
  const name = document.getElementById("new-bot-name").value.trim() || "MeetingBot";
  if (!url) { showToast("Meeting URL is required", "error"); return; }

  const btn = document.getElementById("btn-create-bot");
  btn.disabled = true;
  btn.textContent = "Creating…";

  try {
    const bot = await api("POST", "/bot", { meeting_url: url, bot_name: name });
    closeModal("modal-new-bot");
    document.getElementById("new-bot-url").value = "";
    showToast(`Bot created — ${bot.id}`);
    showBotDetail(bot.id);
    loadBots();
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Create Bot";
  }
});

// ── Back button ────────────────────────────────────────────────────────────

document.getElementById("btn-back-bots").addEventListener("click", () => {
  showPage("bots");
  loadBots();
});

// ── Refresh ────────────────────────────────────────────────────────────────

document.getElementById("btn-refresh").addEventListener("click", () => {
  const filter = document.getElementById("filter-status").value.trim();
  loadBots(filter);
});

document.getElementById("filter-status").addEventListener("input", (e) => {
  const filter = e.target.value.trim();
  loadBots(filter);
});

// ── Webhooks ───────────────────────────────────────────────────────────────

async function loadWebhooks() {
  const listEl = document.getElementById("webhook-list");
  listEl.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const webhooks = await api("GET", "/webhook");
    if (!webhooks.length) {
      listEl.innerHTML = '<div class="empty">No webhooks registered yet</div>';
      return;
    }
    listEl.innerHTML = webhooks
      .map(
        (wh) => `
      <div class="webhook-row">
        <div class="webhook-url">${escHtml(wh.url)}</div>
        <div class="webhook-events">
          ${wh.events.map((e) => `<span class="chip">${escHtml(e)}</span>`).join("")}
        </div>
        <div style="white-space:nowrap;font-size:0.78rem;color:var(--text-muted)">${wh.delivery_attempts} delivered</div>
        <button class="btn btn-danger" data-delete-wh="${wh.id}" title="Delete webhook">🗑</button>
      </div>`
      )
      .join("");

    listEl.querySelectorAll("[data-delete-wh]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this webhook?")) return;
        try {
          await api("DELETE", `/webhook/${btn.dataset.deleteWh}`);
          showToast("Webhook deleted");
          loadWebhooks();
        } catch (e) {
          showToast(e.message, "error");
        }
      });
    });
  } catch (e) {
    listEl.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

document.getElementById("btn-new-webhook").addEventListener("click", () =>
  openModal("modal-new-webhook")
);

document.getElementById("btn-create-webhook").addEventListener("click", async () => {
  const url = document.getElementById("new-wh-url").value.trim();
  const secret = document.getElementById("new-wh-secret").value.trim() || null;
  const select = document.getElementById("new-wh-events");
  const events = Array.from(select.selectedOptions).map((o) => o.value);

  if (!url) { showToast("URL is required", "error"); return; }

  const btn = document.getElementById("btn-create-webhook");
  btn.disabled = true;
  btn.textContent = "Registering…";

  try {
    await api("POST", "/webhook", { url, events, secret });
    closeModal("modal-new-webhook");
    document.getElementById("new-wh-url").value = "";
    showToast("Webhook registered");
    loadWebhooks();
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Register";
  }
});

// ── XSS safety ────────────────────────────────────────────────────────────

function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Init ───────────────────────────────────────────────────────────────────

loadBots();
setInterval(() => {
  const hasActive = _bots.some((b) =>
    ["ready", "joining", "in_call", "call_ended"].includes(b.status)
  );
  if (hasActive) loadBots();
}, 5000);
