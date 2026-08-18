// ── Config ──────────────────────────────────────────────────────────
// Empty string: the dashboard is now served by the same Flask process
// as the API (api_server.py), on the same origin, so fetch("/api/...")
// already resolves correctly without a host to specify.
const API_BASE_URL = "";

const POLL_INTERVAL_MS = 10_000;

// ── Helpers ─────────────────────────────────────────────────────────

function fmtSol(value, decimals = 4) {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(decimals)} SOL`;
}

function pnlClass(value) {
  if (value === null || value === undefined) return "pnl-zero";
  if (value > 0) return "pnl-pos";
  if (value < 0) return "pnl-neg";
  return "pnl-zero";
}

function fmtTime(isoString) {
  try {
    const d = new Date(isoString);
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return isoString;
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

async function fetchJson(path) {
  const resp = await fetch(`${API_BASE_URL}${path}`);
  if (!resp.ok) throw new Error(`${path} → HTTP ${resp.status}`);
  return resp.json();
}

// ── Renderers ───────────────────────────────────────────────────────

function renderState(state) {
  document.getElementById("stat-balance").textContent = fmtSol(state.balance_sol);
  const pnlEl = document.getElementById("stat-pnl");
  pnlEl.textContent = fmtSol(state.realized_pnl_sol, 4);
  pnlEl.className = `stat-value ${pnlClass(state.realized_pnl_sol)}`;

  const positions = state.open_positions || [];
  document.getElementById("stat-open-count").textContent = positions.length;

  const body = document.getElementById("positions-body");
  if (positions.length === 0) {
    body.innerHTML = `<div class="empty">No open positions.</div>`;
    return;
  }

  body.innerHTML = positions.map(pos => `
    <div class="card">
      <div class="card-title">
        <span class="ticker">${escapeHtml(pos.ticker)}</span>
        <span class="${pnlClass(pos.pnl_current_sol)}">${fmtSol(pos.pnl_current_sol)}</span>
      </div>
      <div class="card-meta">
        size ${fmtSol(pos.size_sol)} · avg entry ${pos.avg_entry ?? "—"} · ${escapeHtml(pos.mint)}
      </div>
      ${pos.thesis ? `<div class="card-thesis">${escapeHtml(pos.thesis)}</div>` : ""}
    </div>
  `).join("");
}

function renderTheses(theses) {
  const body = document.getElementById("theses-body");
  if (!theses || theses.length === 0) {
    body.innerHTML = `<div class="empty">No active theses.</div>`;
    return;
  }

  body.innerHTML = theses.map(t => `
    <div class="card">
      <div class="card-title">
        <span class="ticker">${escapeHtml(t.token_ticker)}</span>
        <span class="status-badge status-${escapeHtml(t.status)}">${escapeHtml(t.status)}</span>
      </div>
      <div class="card-meta">
        ${t.risk_score !== null && t.risk_score !== undefined ? `risk ${t.risk_score}/10 · ` : ""}
        updated ${fmtTime(t.updated_at)}
      </div>
      <div class="card-thesis">${escapeHtml(t.thesis_text)}</div>
    </div>
  `).join("");
}

function renderJournal(entries) {
  const body = document.getElementById("journal-body");
  if (!entries || entries.length === 0) {
    body.innerHTML = `<div class="empty">No journal entries yet.</div>`;
    return;
  }

  body.innerHTML = entries.map(e => `
    <div class="journal-entry">
      <span class="journal-time">${fmtTime(e.timestamp)}</span>
      <span class="journal-kind kind-${escapeHtml(e.kind)}">${escapeHtml(e.kind)}</span>
      <span class="journal-text">${e.token_ticker ? `<strong>${escapeHtml(e.token_ticker)}</strong> — ` : ""}${escapeHtml(e.text)}</span>
    </div>
  `).join("");
}

function setConnStatus(ok) {
  const el = document.getElementById("conn-status");
  el.textContent = ok ? "live" : "unreachable";
  el.className = `stat-value ${ok ? "ok" : "err"}`;
}

// ── Poll loop ───────────────────────────────────────────────────────

async function pollOnce() {
  try {
    const [state, theses, journal] = await Promise.all([
      fetchJson("/api/state"),
      fetchJson("/api/theses"),
      fetchJson("/api/journal?limit=50"),
    ]);
    renderState(state);
    renderTheses(theses);
    renderJournal(journal);
    setConnStatus(true);
  } catch (err) {
    console.error("Poll failed:", err);
    setConnStatus(false);
  }
}

pollOnce();
setInterval(pollOnce, POLL_INTERVAL_MS);
