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

function fmtUsdc(value, decimals = 2) {
  if (value === null || value === undefined) return "—";
  return `$${value.toFixed(decimals)}`;
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

function renderRealState(realState) {
  const badge = document.getElementById("real-armed-badge");
  badge.textContent = realState.armed ? "REAL" : "REAL (unarmed)";
  badge.classList.toggle("unarmed", !realState.armed);

  document.getElementById("stat-real-balance").textContent = fmtUsdc(realState.balance_usdc);
  document.getElementById("stat-real-gas").textContent = fmtSol(realState.balance_sol, 4);

  const pnlEl = document.getElementById("stat-real-pnl");
  pnlEl.textContent = fmtUsdc(realState.realized_pnl_usdc);
  pnlEl.className = `stat-value ${pnlClass(realState.realized_pnl_usdc)}`;

  const positions = realState.open_real_positions || [];
  document.getElementById("stat-real-open-count").textContent = positions.length;
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

// Two independent questions about every journal entry:
//
//   isReal — did this entry come from the real-money path? kind
//   "did_real" covers every real-trading outcome (armed success,
//   unarmed, blocked, failed) by design — see real_trading.py. The one
//   exception is the gas-reserve refusal, which deliberately logs as
//   kind "refused" (its own record, distinct from a guard rail simply
//   not liking a trade) but still carries meta.min_sol_for_gas, which
//   is what identifies it as real-money-related here.
//
//   isTrade — did this entry represent an actual completed action
//   (opened/closed/topped up), vs. Ledger's reasoning about one? A
//   paper "did" always is. A real "did_real" only is when it actually
//   filled (meta.status === "success") — an unarmed/blocked/failed
//   did_real entry is Ledger explaining why nothing happened, which
//   belongs in Live Thoughts, not Trades.
//
// New kinds default to both false — safer to under-classify into Live
// Thoughts / Paper than to silently miscount a real fill as paper or
// bury an actual trade among reasoning.
function classifyEntry(e) {
  const isReal =
    e.kind === "did_real" ||
    (e.kind === "refused" && e.meta && Object.prototype.hasOwnProperty.call(e.meta, "min_sol_for_gas"));
  const isTrade =
    e.kind === "did" ||
    (e.kind === "did_real" && e.meta && e.meta.status === "success");
  return { isReal, isTrade };
}

// Trade color scheme (Trades panel only) — light blue for a position
// opened or added to, green/red for a close by realized pnl sign. Same
// palette as ledger_bot.py's COLOR_BUY/PROFIT/LOSS and Discord's leading
// 🟦/🟢/🔴 title emoji. Driven by the pnl fields already present in
// journal_meta (pnl_sol for a paper close, realized_pnl_usdc for a real
// sell) rather than parsing message text, so it doesn't depend on title
// wording. An open/topup/real-buy entry carries neither field and falls
// through to "trade-open".
function tradeClass(e) {
  const meta = e.meta || {};
  if (typeof meta.pnl_sol === "number") {
    return meta.pnl_sol >= 0 ? "trade-profit" : "trade-loss";
  }
  if (typeof meta.realized_pnl_usdc === "number") {
    return meta.realized_pnl_usdc >= 0 ? "trade-profit" : "trade-loss";
  }
  return "trade-open";
}

let lastJournalEntries = [];
let activityFilter = "real"; // "real" | "paper" — starts on "real" now that REAL_TRADING_ENABLED is armed

function renderJournalEntries(containerId, entries, emptyText) {
  const body = document.getElementById(containerId);
  if (!entries || entries.length === 0) {
    body.innerHTML = `<div class="empty">${emptyText}</div>`;
    return;
  }

  body.innerHTML = entries.map(e => {
    const tradeCls = classifyEntry(e).isTrade ? ` ${tradeClass(e)}` : "";
    return `
    <div class="journal-entry${tradeCls}">
      <span class="journal-time">${fmtTime(e.timestamp)}</span>
      <span class="journal-kind kind-${escapeHtml(e.kind)}">${escapeHtml(e.kind)}</span>
      <span class="journal-text">${e.token_ticker ? `<strong>${escapeHtml(e.token_ticker)}</strong> — ` : ""}${escapeHtml(e.text)}</span>
    </div>
  `;
  }).join("");
}

function renderJournal(entries) {
  if (entries) lastJournalEntries = entries; // cache so the filter toggle can re-render without a re-fetch
  entries = lastJournalEntries || [];

  const wantReal = activityFilter === "real";
  const filtered = entries.filter(e => classifyEntry(e).isReal === wantReal);
  const trades = filtered.filter(e => classifyEntry(e).isTrade);
  const liveThoughts = filtered.filter(e => !classifyEntry(e).isTrade);

  const noun = wantReal ? "real" : "paper";
  renderJournalEntries("live-thoughts-body", liveThoughts, `No ${noun} activity yet.`);
  renderJournalEntries("trades-body", trades, `No ${noun} trades yet.`);
}

function setActivityFilter(filter) {
  activityFilter = filter;
  document.querySelectorAll("#activity-filter .filter-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.filter === filter);
  });
  renderJournal(); // re-render from the cache — no need to wait for the next poll
}

document.getElementById("activity-filter").addEventListener("click", (event) => {
  const btn = event.target.closest(".filter-btn");
  if (btn) setActivityFilter(btn.dataset.filter);
});

function setConnStatus(ok) {
  const el = document.getElementById("conn-status");
  el.textContent = ok ? "live" : "unreachable";
  el.className = `stat-value ${ok ? "ok" : "err"}`;
}

// ── Poll loop ───────────────────────────────────────────────────────

async function pollOnce() {
  try {
    const [state, realState, theses, journal] = await Promise.all([
      fetchJson("/api/state"),
      fetchJson("/api/real_state"),
      fetchJson("/api/theses"),
      fetchJson("/api/journal?limit=150"),
    ]);
    renderState(state);
    renderRealState(realState);
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
