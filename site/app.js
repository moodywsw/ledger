// Ledger dashboard v2 — vanilla JS, no build step. Same-origin API.
const API_BASE_URL = "";
const POLL_INTERVAL_MS = 10_000;
const $ = id => document.getElementById(id);

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function num(v, d = 4) { return (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d); }
function sol(v, d = 4) { return v == null ? "—" : `${v > 0 ? "+" : ""}${num(v, d)} SOL`; }
function solPlain(v, d = 3) { return v == null ? "—" : `${num(v, d)} SOL`; }
function usd(v, signed = false) { if (v == null) return "—"; const s = signed && v > 0 ? "+" : ""; return `${s}${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
function pct(v) { return v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`; }
function cls(v) { return v == null || v === 0 ? "zero" : v > 0 ? "pos" : "neg"; }
function price(v) { if (v == null) return "—"; return v < 0.001 ? Number(v).toExponential(2) : Number(v).toPrecision(4); }
function ago(iso) { const t = new Date(iso).getTime(); if (!t) return ""; const s = (Date.now() - t) / 1000;
  if (s < 60) return `${Math.floor(s)}s ago`; if (s < 3600) return `${Math.floor(s / 60)}m ago`; if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return `${Math.floor(s / 86400)}d ago`; }
function setVal(id, text, c) { const el = $(id); el.textContent = text; if (c !== undefined) el.className = `${el.className.replace(/\b(pos|neg|zero)\b/g, "").trim()} ${c}`; }
async function fetchJson(path) { const r = await fetch(`${API_BASE_URL}${path}`); if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`); return r.json(); }
const empty = t => `<div class="empty">${t}</div>`;

// ── Equity chart (inline SVG) ───────────────────────────────────────
function renderEquity(points) {
  const el = $("equity-chart");
  if (!points || points.length < 2) { el.innerHTML = empty("The curve appears after the first closed trades."); return; }
  const W = 1000, H = 200, P = 8, vs = points.map(p => p.v), vals = [0, ...vs];
  const min = Math.min(...vals), max = Math.max(...vals), span = (max - min) || 1;
  const x = i => P + (i / (vals.length - 1)) * (W - 2 * P), y = v => H - P - ((v - min) / span) * (H - 2 * P);
  const line = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const up = vals[vals.length - 1] >= 0, col = up ? "#22d39b" : "#ff5c7a";
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".35"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" stroke="#2a3342" stroke-dasharray="4 6"/>
    <path d="${line}L${x(vals.length - 1)},${H}L${x(0)},${H}Z" fill="url(#g)"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="2.5" vector-effect="non-scaling-stroke"/></svg>`;
}

// ── Renderers ───────────────────────────────────────────────────────
function renderState(state) {
  setVal("stat-balance", solPlain(state.balance_sol, 2));
  const ps = state.open_positions || [];
  $("stat-open-count").textContent = ps.length; $("chip-open").textContent = `${ps.length} open`;
  $("positions-body").innerHTML = ps.length ? ps.map(p => `
    <div class="pcard">
      <div class="top"><span class="tk">${esc(p.ticker)}${p.moonbag ? '<span class="tag moon">🌙 MOONBAG</span>' : ""}</span>
        <span class="pp mono ${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</span></div>
      <div class="rows mono">
        <span>Entry</span><span>$${price(p.avg_entry)}</span>
        <span>Now</span><span>$${price(p.current_price)}</span>
        <span>Size</span><span>${solPlain(p.size_sol, 3)}</span>
        <span>PnL</span><span class="${cls(p.pnl_current_sol)}">${sol(p.pnl_current_sol)}</span>
        <span>Copied</span><span>${esc(p.opened_by || p.source || "—")}</span>
      </div>
    </div>`).join("") : empty("No open positions — waiting for a signal.");
}

function renderRealState(r) {
  const b = $("real-armed-badge"); b.textContent = r.armed ? "REAL MONEY · ARMED" : "REAL · UNARMED"; b.className = `chip ${r.armed ? "real" : "warn"}`;
  setVal("stat-real-balance", usd(r.balance_usdc));
  setVal("stat-real-gas", solPlain(r.balance_sol, 3));
  setVal("stat-real-pnl", usd(r.realized_pnl_usdc, true), cls(r.realized_pnl_usdc));
  setVal("stat-exposure", usd(r.exposure_usdc));
}

let overview = null, activeTheses = [], thesisTab = "own";
function renderOverview(o) {
  overview = o;
  setVal("pnl-today", sol(o.pnl_sol.today, 3), cls(o.pnl_sol.today));
  setVal("pnl-7d", sol(o.pnl_sol.d7, 3), cls(o.pnl_sol.d7));
  setVal("pnl-all", sol(o.pnl_sol.all, 3), cls(o.pnl_sol.all));
  const tot = o.wins + o.losses;
  $("winloss").textContent = tot ? `${o.wins} / ${o.losses} · ${Math.round(o.wins / tot * 100)}%` : "—";
  renderEquity(o.equity);
  $("closed-body").innerHTML = o.closed_trades.length ? o.closed_trades.slice(0, 12).map(t => `
    <div class="row"><div class="l"><div class="t">${esc(t.symbol)} <span class="muted">${t.action === "partial_close" ? "partial" : ""}</span></div>
      <div class="m">${esc(t.reason || "exit")} · ${esc(t.opened_by || "—")} · ${ago(t.at)}</div></div>
      <div class="mono ${cls(t.pnl_sol)}">${sol(t.pnl_sol)}</div></div>`).join("") : empty("No closed trades yet.");
  const tr = [...o.traders].sort((a, b) => (b.active - a.active) || (b.trades - a.trades));
  $("traders-sum").textContent = `${tr.filter(t => t.active).length} active · ${tr.filter(t => !t.active).length} paused`;
  $("chip-wl").textContent = `${tr.filter(t => t.active).length} traders copied`;
  $("traders-body").innerHTML = tr.map(t => `
    <div class="tr ${t.active ? "" : "off"}"><div class="h"><span>${esc(t.handle.replace(/^fomo:/i, ""))}</span><span class="st ${t.active ? "on" : "off"}">${t.active ? "ACTIVE" : "PAUSED"}</span></div>
      <div class="chains">${t.chains.map(c => `<span class="cn">${esc(c)}</span>`).join("")}</div>
      <div class="muted">${t.trades ? `${t.trades} trades · hit ${Math.round(t.hit_rate * 100)}% · <span class="${cls(t.pnl_sol)}">${sol(t.pnl_sol, 3)}</span>` : "No trades yet"}</div>
      <div class="bar"><i style="width:${t.hit_rate == null ? 0 : Math.round(t.hit_rate * 100)}%"></i></div></div>`).join("") || empty("No traders configured.");
  renderTheses();
}

function renderTheses() {
  const body = $("theses-body");
  if (thesisTab === "own") {
    const ts = (overview && overview.own_theses) || [];
    body.innerHTML = ts.length ? ts.map(t => `<div class="thesis"><div class="m">${esc(t.symbol)} · ${esc(t.conviction || "")} conviction · score ${esc(t.score)} · ${esc(t.regime || "")} · ${ago(t.at)}</div><ul>${(t.why || []).map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>`).join("")
      : empty("Ledger hasn't formed its own thesis yet.");
  } else {
    body.innerHTML = activeTheses.length ? activeTheses.map(t => `<div class="thesis"><div class="m">${esc(t.token_ticker)} · ${esc(t.status)}${t.risk_score != null ? ` · risk ${t.risk_score}/10` : ""} · ${ago(t.updated_at)}</div>${esc(t.thesis_text)}</div>`).join("")
      : empty("No active theses.");
  }
}
$("thesis-tabs").addEventListener("click", e => { const b = e.target.closest(".tab"); if (!b) return; thesisTab = b.dataset.t;
  document.querySelectorAll("#thesis-tabs .tab").forEach(x => x.classList.toggle("active", x === b)); renderTheses(); });

// ── Ledger's mind (persona feed) ─────────────────────────────────────
const KIND = { musing: ["💭", "thought"], mood: ["🫀", "mood"], recap: ["📊", "recap"], entry: ["🟢", "entry"], exit_win: ["💰", "exit"],
  exit_loss: ["🩸", "exit"], refusal: ["🚩", "refused"], thesis_own: ["🧭", "thesis"], thesis_kol: ["👀", "kol"] };
function renderMind(f) {
  const pill = $("mood-pill"), body = $("mind-feed");
  if (!f) { pill.textContent = "offline"; body.innerHTML = empty("Ledger's thoughts will appear here."); return; }
  pill.textContent = `${f.mood.emoji || ""} ${f.mood.label || ""}`.trim();
  const ps = f.posts || [];
  body.innerHTML = ps.length ? ps.slice(0, 12).map((p, i) => { const [ic, lb] = KIND[p.kind] || ["💬", p.kind];
    return `<div class="thought ${i === 0 ? "latest" : ""} k-${esc(p.kind)}"><div class="th-meta"><span>${ic} ${esc(p.topic ? p.topic.replace("_", " ") : lb)}</span><span>${ago(new Date(p.ts * 1000))}</span></div><div class="th-text">${esc(p.text)}</div></div>`; }).join("")
    : (f.beliefs || []).length ? f.beliefs.map(b => `<div class="thought"><div class="th-meta"><span>🧭 belief</span></div><div class="th-text">${esc(b)}</div></div>`).join("")
    : empty("Ledger is quiet for now — first thoughts land within a few hours.");
}

// ── Journal (same classification logic as v1) ───────────────────────
function classifyEntry(e) {
  const isReal = e.kind === "did_real" || (e.kind === "refused" && e.meta && Object.prototype.hasOwnProperty.call(e.meta, "min_sol_for_gas"));
  const isTrade = e.kind === "did" || (e.kind === "did_real" && e.meta && e.meta.status === "success");
  return { isReal, isTrade };
}
function tradeClass(e) { const m = e.meta || {};
  if (typeof m.pnl_sol === "number") return m.pnl_sol >= 0 ? "jtrade-profit" : "jtrade-loss";
  if (typeof m.realized_pnl_usdc === "number") return m.realized_pnl_usdc >= 0 ? "jtrade-profit" : "jtrade-loss";
  return "jtrade-open"; }
let lastJournal = [], activityFilter = "real";
function renderEntries(id, es, txt) {
  $(id).innerHTML = es.length ? es.slice(0, 60).map(e => `<div class="row ${classifyEntry(e).isTrade ? tradeClass(e) : ""}"><div class="l">
    <div class="t">${e.token_ticker ? esc(e.token_ticker) : esc(e.kind)}</div><div class="m">${esc(e.text)}</div></div><span class="jt">${ago(e.timestamp)}</span></div>`).join("") : empty(txt);
}
function renderJournal(entries) {
  if (entries) lastJournal = entries;
  const want = activityFilter === "real", f = lastJournal.filter(e => classifyEntry(e).isReal === want);
  renderEntries("trades-body", f.filter(e => classifyEntry(e).isTrade), `No ${activityFilter} trades yet.`);
  renderEntries("live-thoughts-body", f.filter(e => !classifyEntry(e).isTrade), `No ${activityFilter} activity yet.`);
}
$("activity-filter").addEventListener("click", e => { const b = e.target.closest(".filter-btn"); if (!b) return; activityFilter = b.dataset.filter;
  document.querySelectorAll("#activity-filter .tab").forEach(x => x.classList.toggle("active", x === b)); renderJournal(); });

function setConn(ok) { $("conn-dot").className = `dot ${ok ? "ok" : "err"}`; $("conn-status").textContent = ok ? "Live" : "Unreachable";
  if (ok) $("last-update").textContent = `· ${new Date().toLocaleTimeString()}`; }

async function pollOnce() {
  const jobs = {
    state: fetchJson("/api/state").then(renderState),
    real: fetchJson("/api/real_state").then(renderRealState),
    theses: fetchJson("/api/theses").then(t => { activeTheses = t || []; }),
    journal: fetchJson("/api/journal?limit=150").then(renderJournal),
    persona: fetchJson("/api/persona/feed?limit=20").then(renderMind).catch(() => renderMind(null)),
    overview: fetchJson("/api/overview").then(renderOverview).catch(e => console.error(e)),
  };
  const res = await Promise.allSettled(Object.values(jobs));
  renderTheses();
  setConn(res.slice(0, 2).some(r => r.status === "fulfilled"));
}
pollOnce(); setInterval(pollOnce, POLL_INTERVAL_MS);

// ── ON/OFF kill switch (unchanged auth: Bearer admin token, cached in localStorage) ──
let switchEnabled = null;
function renderSwitch(st) {
  switchEnabled = !!st.enabled;
  const s = $("switch-state"); s.textContent = switchEnabled ? "ON" : "OFF"; s.className = `switch-state ${switchEnabled ? "on" : "off"}`;
  $("status-pulse").className = `pulse ${switchEnabled ? "on" : "off"}`;
  $("switch-note").textContent = switchEnabled ? "Ledger is watching traders and can open new positions." : "Paused — no new buys. Open positions are still managed (exits keep running).";
  const b = $("switch-btn"); b.disabled = false; b.className = `switch ${switchEnabled ? "on" : ""}`;
  $("switch-caption").textContent = switchEnabled ? "Tap to pause" : "Tap to resume";
}
async function pollSwitch() { try { renderSwitch(await fetchJson("/api/bot_switch")); } catch (e) { console.error(e); } }
$("switch-btn").addEventListener("click", async () => {
  let token = localStorage.getItem("ledgerAdminToken");
  if (!token) { token = prompt("Admin token (from Railway boot log line [SWITCH] ... token=...):"); if (!token) return; }
  const target = !switchEnabled;
  if (!confirm(target ? "Turn the bot ON (allow new buys)?" : "Turn the bot OFF (no new buys)?")) return;
  const b = $("switch-btn"); b.disabled = true;
  try {
    const r = await fetch(`${API_BASE_URL}/api/bot_switch`, { method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token.trim()}` }, body: JSON.stringify({ enabled: target }) });
    if (r.status === 401) { localStorage.removeItem("ledgerAdminToken"); alert("Wrong admin token."); }
    else if (!r.ok) alert(`Failed: HTTP ${r.status}`);
    else { localStorage.setItem("ledgerAdminToken", token.trim()); renderSwitch(await r.json()); }
  } finally { b.disabled = false; pollSwitch(); }
});
pollSwitch(); setInterval(pollSwitch, POLL_INTERVAL_MS);
